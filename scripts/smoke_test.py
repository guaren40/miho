"""Exercise the real ARM64 container, including a VLESS protocol exchange."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
import aiohttp
from aiohttp import web
import yaml

IMAGE = os.getenv('TEST_IMAGE', 'miho:test')
NAME = 'miho-smoke'
VOLUME = 'miho-smoke-data'
PASSWORD = 'ci-password-not-for-production'
MARKER = 'miho-real-proxy-ok'


def docker(*arguments, check=True):
    result = subprocess.run(['docker', *arguments], capture_output=True, text=True, check=check)
    return result.stdout.strip()


def start(password=PASSWORD):
    docker('run', '-d', '--name', NAME, '--add-host', 'host.docker.internal:host-gateway',
           '-p', '127.0.0.1::8000', '-p', '127.0.0.1::7890', '-v', VOLUME + ':/data',
           '-e', 'ZEABUR=1', '-e', 'MIHO_DEBUG=1', '-e', 'PASSWORD=' + password, IMAGE)
    web_port = docker('port', NAME, '8000/tcp').rsplit(':', 1)[1]
    proxy_port = docker('port', NAME, '7890/tcp').rsplit(':', 1)[1]
    return 'http://127.0.0.1:' + web_port, 'http://127.0.0.1:' + proxy_port


async def ready(session, base):
    for _ in range(180):
        try:
            async with session.get(base + '/healthz', timeout=aiohttp.ClientTimeout(total=2)) as response:
                if response.status == 200:
                    return
        except (OSError, aiohttp.ClientError, asyncio.TimeoutError):
            pass
        await asyncio.sleep(1)
    raise AssertionError('Container failed to become healthy')


async def test():
    architecture = docker('image', 'inspect', IMAGE, '--format', '{{.Os}}/{{.Architecture}}')
    assert architecture == 'linux/arm64', architecture
    assert docker('run', '--rm', '--entrypoint', 'uname', IMAGE, '-m') == 'aarch64'
    fixture = web.Application()
    fixture.router.add_get('/marker', lambda _: web.Response(text=MARKER))
    fixture.router.add_get('/subscription', lambda _: web.Response(text='proxy-groups:\n  - name: 测试策略\n    type: select\n    proxies: [DIRECT]\nrules:\n  - MATCH,测试策略\n'))
    runner = web.AppRunner(fixture)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 0)
    await site.start()
    fixture_port = site._server.sockets[0].getsockname()[1]
    docker('rm', '-f', NAME, check=False)
    docker('volume', 'rm', '-f', VOLUME, check=False)
    base, proxy = start()
    gateway = docker('exec', NAME, 'python', '-c', 'import socket; print(socket.gethostbyname("host.docker.internal"))')
    target = f'http://{gateway}:{fixture_port}/marker'
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as session:
        await ready(session, base)
        assert (await session.get(base + '/manage/info')).status == 401
        assert (await session.get(base + '/api/version')).status == 401
        assert (await session.post(base + '/manage/login', json={'password': 'wrong'})).status == 401
        assert (await session.post(base + '/manage/login', json={'password': PASSWORD})).status == 200
        info = await (await session.get(base + '/manage/info')).json()
        assert (await session.get(base + '/api/version')).status == 200
        assert (await session.get(base + '/ui/index.html')).status == 200
        async with session.ws_connect(base + '/api/traffic') as ws:
            assert 'up' in json.loads((await ws.receive(timeout=10)).data)
        export = await (await session.get(base + '/manage/client.yaml')).text()
        node = yaml.safe_load(export)['proxies'][0]
        assert node['network'] == 'ws' and node['ws-opts']['path'] == '/tunnel'
        docker('exec', NAME, 'python', '-c', "from pathlib import Path; import sys; Path('/data/client-test.yaml').write_text(sys.argv[1])", export)
        docker('exec', NAME, 'mihomo', '-t', '-d', '/data', '-f', '/data/client-test.yaml')
        # A complete VLESS request: v0, UUID, no addons, TCP, port, domain, payload.
        host = gateway.encode()
        header = b'\x00' + uuid.UUID(node['uuid']).bytes + b'\x00\x01' + fixture_port.to_bytes(2, 'big') + b'\x02' + bytes([len(host)]) + host
        request = f'GET /marker HTTP/1.1\r\nHost: host.docker.internal:{fixture_port}\r\nConnection: close\r\n\r\n'.encode()
        async with session.ws_connect(base + '/tunnel') as ws:
            await ws.send_bytes(header + request)
            received = bytearray()
            for _ in range(30):
                message = await ws.receive(timeout=10)
                if message.type == aiohttp.WSMsgType.BINARY:
                    received.extend(message.data)
                    if MARKER.encode() in received:
                        break
                elif message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                    break
            assert received[:2] == b'\x00\x00' and MARKER.encode() in received, repr(received)
        print('VLESS over public WebSocket: real TCP traffic passed.')
        # Invalid UUID must never reach the destination.
        async with session.ws_connect(base + '/tunnel') as ws:
            await ws.send_bytes(b'\x00' + uuid.uuid4().bytes + header[17:] + request)
            try:
                message = await ws.receive(timeout=4)
                assert message.type != aiohttp.WSMsgType.BINARY or MARKER.encode() not in message.data
            except asyncio.TimeoutError:
                pass
        assert (await session.get(target, proxy=proxy)).status == 407
        async with session.get(target, proxy=proxy, proxy_auth=aiohttp.BasicAuth(info['proxy_user'], info['proxy_password'])) as response:
            assert MARKER in await response.text()
        print('HTTP proxy authentication and traffic: passed.')
        # Real core validates URL imports and protects the previous configuration.
        response = await session.post(base + '/manage/config', json={'url': f'http://host.docker.internal:{fixture_port}/subscription'})
        assert response.status == 200, await response.text()
        saved = await (await session.get(base + '/manage/config')).json()
        assert '测试策略' in saved['text']
        response = await session.post(base + '/manage/config', json={'text': 'rules: [invalid-rule]'} )
        assert response.status == 400, await response.text()
        assert await (await session.get(base + '/manage/config')).json() == saved
        assert (await session.get(base + '/healthz')).status == 200
        assert (await session.patch(base + '/api/configs', json={'mixed-port': 0})).status == 400
        runtime = yaml.safe_load(docker('exec', NAME, 'cat', '/data/runtime.yaml'))
        assert runtime['authentication'] and runtime['skip-auth-prefixes'] == []
        docker('rm', '-f', NAME)
        base, _ = start('changed-env-must-not-reset-state')
        await ready(session, base)
        assert (await session.post(base + '/manage/login', json={'password': PASSWORD})).status == 200
        restored = await (await session.get(base + '/manage/info')).json()
        for key in ('vless_uri', 'proxy_password', 'api_token', 'subscription_url'):
            if key == 'vless_uri':
                assert node['uuid'] in restored[key]
            elif key == 'subscription_url':
                assert restored[key].endswith('/subscription')
            else:
                assert restored[key] == info[key]
        assert await (await session.get(base + '/manage/config')).json() == saved
        print('Import validation, rejected-config recovery and volume persistence: passed.')
        # Browser verification on a reachable persistent container.
        Path('/tmp/miho-test-url').write_text(base)
        if os.getenv('BROWSER_TEST') == '1':
            from playwright.async_api import async_playwright
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch()
                page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
                await page.goto(base)
                await page.locator('#password').fill(PASSWORD)
                await page.locator('#login-form button').click()
                await page.locator('#console').wait_for(state='visible')
                await page.locator('#open-dashboard').click()
                await page.screenshot(path='test-results/console.png', full_page=True)
                frame = page.frame_locator('#dashboard-frame')
                await frame.get_by_text('测试策略', exact=True).first.wait_for(timeout=60000)
                await page.screenshot(path='test-results/console.png', full_page=True)
                await page.locator('[data-tab="config"]').click()
                await page.locator('#yaml').fill('proxy-groups:\n  - name: 浏览器策略\n    type: select\n    proxies: [DIRECT]\nrules:\n  - MATCH,浏览器策略\n')
                await page.locator('#yaml-form button').click()
                await page.get_by_text('配置已生效', exact=True).wait_for(timeout=60000)
                await page.locator('[data-tab="dashboard"]').click()
                await frame.get_by_text('浏览器策略', exact=True).first.wait_for(timeout=60000)
                await browser.close()
                print('Browser: login, dashboard automatic connection and configuration import passed.')
    await runner.cleanup()


if __name__ == '__main__':
    Path('test-results').mkdir(exist_ok=True)
    try:
        asyncio.run(test())
    except BaseException:
        print(docker('logs', NAME, check=False), file=sys.stderr)
        raise
    finally:
        docker('rm', '-f', NAME, check=False)
        docker('volume', 'rm', '-f', VOLUME, check=False)
