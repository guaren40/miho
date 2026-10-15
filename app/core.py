import asyncio
import copy
import os
from pathlib import Path
import aiohttp
import yaml
from .state import atomic_write

MAX_CONFIG = 5 * 1024 * 1024


def normalize(text, state):
    if len(text.encode()) > MAX_CONFIG:
        raise ValueError('配置超过 5 MiB')
    try:
        config = yaml.safe_load(text) if text.strip() else {}
    except yaml.YAMLError as error:
        raise ValueError('YAML 格式错误') from error
    if not isinstance(config, dict):
        raise ValueError('需要 Clash / Mihomo YAML 配置，暂不接受 Base64 或节点链接订阅')
    config = copy.deepcopy(config)
    # These listeners and credentials belong to the gateway, including on imports.
    for key in ('port', 'socks-port', 'redir-port', 'tproxy-port', 'external-controller-tls',
                'external-controller-unix', 'external-controller-pipe', 'external-doh-server',
                'external-ui-url', 'external-ui', 'external-ui-name', 'listeners', 'tunnels'):
        config.pop(key, None)
    config.update({
        'mixed-port': 7890, 'allow-lan': True, 'bind-address': '*',
        'authentication': [state['proxy_user'] + ':' + state['proxy_password']],
        'skip-auth-prefixes': [], 'external-controller': '127.0.0.1:9090',
        'secret': state['api_secret'], 'mode': config.get('mode', 'rule'),
        'tun': {'enable': False},
        'external-controller-cors': {'allow-origins': [], 'allow-private-network': False},
        'listeners': [{'name': 'miho-ws', 'type': 'vless', 'listen': '127.0.0.1',
                       'port': 7891, 'users': [{'username': 'miho', 'uuid': state['vless_uuid']}],
                       # TLS terminates at Zeabur / the reverse proxy. This listener is loopback only.
                       'allow-insecure': True, 'ws-path': '/tunnel'}],
    })
    profile = config.get('profile') or {}
    if not isinstance(profile, dict):
        raise ValueError('profile 必须是对象')
    profile['store-selected'] = True
    config['profile'] = profile
    dns = config.get('dns')
    if isinstance(dns, dict) and dns.get('listen'):
        dns['listen'] = '127.0.0.1:1053'
    if not config.get('proxy-groups'):
        proxies = config.get('proxies') or []
        if not isinstance(proxies, list):
            raise ValueError('proxies 必须是数组')
        names = [item['name'] for item in proxies if isinstance(item, dict) and item.get('name')]
        providers = config.get('proxy-providers') or {}
        group = {'name': '节点选择', 'type': 'select', 'proxies': names + ['DIRECT']}
        if providers:
            group['use'] = list(providers)
        config['proxy-groups'] = [group]
    if not config.get('rules'):
        config['rules'] = ['MATCH,' + config['proxy-groups'][0]['name']]
    return yaml.safe_dump(config, allow_unicode=True, sort_keys=False)


class Core:
    def __init__(self, state):
        self.state = state
        self.directory = state.directory
        self.source = self.directory / 'user.yaml'
        self.runtime = self.directory / 'runtime.yaml'
        self.process = None
        self.reader = None
        self.lock = asyncio.Lock()
        self.stopping = False

    def source_text(self):
        return self.source.read_text() if self.source.exists() else ''

    async def validate(self, runtime):
        path = self.directory / 'validate.yaml'
        atomic_write(path, runtime)
        try:
            process = await asyncio.create_subprocess_exec(
                '/usr/local/bin/mihomo', '-t', '-d', str(self.directory), '-f', str(path),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            try:
                output, _ = await asyncio.wait_for(process.communicate(), 45)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                raise ValueError('配置检查超时，请检查订阅中的远程规则或数据源')
            if process.returncode:
                # Core diagnostics may contain user URLs or credentials. Keep them private.
                raise ValueError('Mihomo 配置检查失败，请检查节点、策略组和规则引用')
        finally:
            path.unlink(missing_ok=True)

    async def start(self, session):
        atomic_write(self.runtime, normalize(self.source_text(), self.state.data))
        await self.launch(session)

    async def launch(self, session):
        self.process = await asyncio.create_subprocess_exec(
            '/usr/local/bin/mihomo', '-d', str(self.directory), '-f', str(self.runtime),
            stdout=None if os.getenv('MIHO_DEBUG') == '1' else asyncio.subprocess.DEVNULL,
            stderr=None if os.getenv('MIHO_DEBUG') == '1' else asyncio.subprocess.DEVNULL)
        for _ in range(150):
            if self.process.returncode is not None:
                raise RuntimeError('Mihomo exited while starting; check the saved configuration')
            try:
                async with session.get('http://127.0.0.1:9090/version',
                                       headers={'Authorization': 'Bearer ' + self.state.data['api_secret']}) as response:
                    if response.status == 200:
                        async with session.ws_connect('http://127.0.0.1:7891/tunnel'):
                            print('Mihomo ready on ARM64.', flush=True)
                            return
            except (OSError, aiohttp.ClientError, asyncio.TimeoutError):
                pass
            await asyncio.sleep(0.2)
        await self.stop()
        raise RuntimeError('Mihomo startup timed out')

    async def stop(self):
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 8)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()

    async def apply(self, text, session, subscription_url=None):
        async with self.lock:
            runtime = normalize(text, self.state.data)
            await self.validate(runtime)
            old_runtime = self.runtime.read_text()
            await self.stop()
            atomic_write(self.runtime, runtime)
            try:
                await self.launch(session)
            except Exception:
                await self.stop()
                atomic_write(self.runtime, old_runtime)
                await self.launch(session)
                raise ValueError('新配置启动失败，已恢复原配置')
            atomic_write(self.source, text)
            if subscription_url is not None:
                self.state.data['subscription_url'] = subscription_url
                self.state.save()

    async def monitor(self, session):
        while True:
            await asyncio.sleep(5)
            async with self.lock:
                if self.process and self.process.returncode is not None:
                    print('Mihomo stopped; restarting with the last saved configuration.', flush=True)
                    try:
                        await self.launch(session)
                    except Exception as error:
                        print(str(error), flush=True)
