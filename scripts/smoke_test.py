"""Exercise the real ARM64 container, including a VLESS protocol exchange."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import ssl
import sys
import tempfile
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
           '-p', '127.0.0.1::8000', '-p', '127.0.0.1:17890:7890', '-p', '127.0.0.1:17892:7892',
           '-v', VOLUME + ':/data', '-e', 'MIHO_HTTP_PROXY_URL=http://127.0.0.1:17890',
           '-e', 'MIHO_HTTPS_PROXY_URL=https://127.0.0.1:17892',
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
    fixture.router.add_get('/subscription', lambda _: web.Response(text='proxies:\n  - {name: 同名节点, type: http, server: 127.0.0.1, port: 9}\nproxy-groups:\n  - name: 测试策略\n    type: select\n    proxies: [DIRECT]\nrules:\n  - MATCH,测试策略\n'))
    second_source = {'fail': False}
    async def provider_two(_):
        if second_source['fail']:
            return web.Response(text='proxies: [')
        return web.Response(text=yaml.safe_dump({'proxy-providers': {'内置': {'type': 'inline', 'payload': [
            {'name': '同名节点', 'type': 'http', 'server': '127.0.0.1', 'port': 9}]}}}, allow_unicode=True))
    fixture.router.add_get('/subscription-two', provider_two)
    runner = web.AppRunner(fixture)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 0)
    await site.start()
    fixture_port = site._server.sockets[0].getsockname()[1]
    # A second fixture checks CONNECT end to end, including TLS inside a TLS proxy.
    fixture_directory = tempfile.TemporaryDirectory()
    fixture_cert = Path(fixture_directory.name) / 'target.crt'
    fixture_key = Path(fixture_directory.name) / 'target.key'
    fixture_gateway = docker('network', 'inspect', 'bridge', '--format', '{{(index .IPAM.Config 0).Gateway}}')
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256',
                    '-nodes', '-days', '1', '-subj', '/CN=host.docker.internal',
                    '-addext', 'subjectAltName=DNS:host.docker.internal,IP:' + fixture_gateway,
                    '-keyout', str(fixture_key), '-out', str(fixture_cert)],
                   check=True, capture_output=True)
    fixture_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    fixture_tls.load_cert_chain(fixture_cert, fixture_key)
    tls_site = web.TCPSite(runner, '0.0.0.0', 0, ssl_context=fixture_tls)
    await tls_site.start()
    tls_port = tls_site._server.sockets[0].getsockname()[1]
    docker('rm', '-f', NAME, check=False)
    docker('volume', 'rm', '-f', VOLUME, check=False)
    base, proxy = start()
    gateway = docker('exec', NAME, 'python', '-c', 'import socket; print(socket.gethostbyname("host.docker.internal"))')
    target = f'http://{gateway}:{fixture_port}/marker'
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as session:
        await ready(session, base)
        assert (await session.get(base + '/manage/info')).status == 401
        assert (await session.get(base + '/api/version')).status == 401
        assert (await session.get(base + '/manage/proxy.crt')).status == 401
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
        proxy_cert = await (await session.get(base + '/manage/proxy.crt')).read()
        proxy_ca = Path(fixture_directory.name) / 'proxy.crt'
        proxy_ca.write_bytes(proxy_cert)
        assert info['http_proxy_uri'].startswith('http://miho:') and info['http_proxy_uri'].endswith('@127.0.0.1:17890')
        assert info['https_proxy_uri'].startswith('https://miho:') and info['https_proxy_uri'].endswith('@127.0.0.1:17892')
        assert info['https_proxy_self_signed']

        async def curl_proxy(uri, url, trust=True):
            arguments = ['curl', '--silent', '--show-error', '--fail', '--max-time', '15', '--noproxy', '',
                         '--proxy', uri, '--cacert', str(fixture_cert)]
            if trust:
                arguments += ['--proxy-cacert', str(proxy_ca)]
            process = await asyncio.create_subprocess_exec(*arguments, url, stdout=asyncio.subprocess.PIPE,
                                                           stderr=asyncio.subprocess.PIPE)
            output, _ = await process.communicate()
            return process.returncode, output

        https_target = f'https://{gateway}:{tls_port}/marker'
        for uri in (info['http_proxy_uri'], info['https_proxy_uri']):
            for url in (target, https_target):
                code, output = await curl_proxy(uri, url)
                assert code == 0 and MARKER.encode() in output, (code, output)
        # TLS verification remains on; an untrusted certificate and bad credentials fail.
        assert (await curl_proxy(info['https_proxy_uri'], https_target, trust=False))[0] != 0
        bad_uri = info['https_proxy_uri'].replace(info['proxy_password'], 'wrong-password')
        assert (await curl_proxy(bad_uri, https_target))[0] != 0
        assert (await session.get(base + '/manage/proxy.key')).status == 404
        print('Exported HTTP/HTTPS links: real HTTP and HTTPS CONNECT traffic, TLS trust and authentication passed.')
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
        source_urls = [f'http://host.docker.internal:{fixture_port}/subscription',
                       f'http://host.docker.internal:{fixture_port}/subscription-two']
        response = await session.post(base + '/manage/config', json={'urls': source_urls})
        assert response.status == 200, await response.text()
        saved = await (await session.get(base + '/manage/config')).json()
        groups = (await (await session.get(base + '/api/proxies')).json())['proxies']
        assert '[提供商1] 同名节点' in groups['[提供商1]']['all']
        assert '[提供商2/内置] 同名节点' in groups['[提供商2]']['all']
        response = await session.post(base + '/manage/refresh', json={})
        assert response.status == 200, await response.text()
        second_source['fail'] = True
        assert (await session.post(base + '/manage/refresh', json={})).status == 400
        assert await (await session.get(base + '/manage/config')).json() == saved
        assert (await (await session.get(base + '/manage/info')).json())['subscription_urls'] == source_urls
        second_source['fail'] = False
        print('Multiple YAML sources: node namespaces, inline providers, refresh and failure preservation passed.')
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
        assert restored['subscription_urls'] == source_urls
        assert await (await session.get(base + '/manage/proxy.crt')).read() == proxy_cert
        assert restored['http_proxy_uri'] == info['http_proxy_uri']
        assert restored['https_proxy_uri'] == info['https_proxy_uri']
        print('Import validation, rejected-config recovery and volume persistence: passed.')
        # Browser verification on a reachable persistent container.
        Path('/tmp/miho-test-url').write_text(base)
        if os.getenv('BROWSER_TEST') == '1':
            from playwright.async_api import async_playwright
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch()
                page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
                page.on('pageerror', lambda error: print('Browser error:', error))
                page.on('response', lambda response: print('HTTP error:', response.status, response.url)
                        if response.status >= 400 and response.url.startswith(base) else None)
                await page.goto(base)
                await page.locator('#password').fill(PASSWORD)
                await page.locator('#login-form button').click()
                await page.locator('#console').wait_for(state='visible')
                assert await page.locator('#http-proxy').input_value() == restored['http_proxy_uri']
                assert await page.locator('#https-proxy').input_value() == restored['https_proxy_uri']
                await page.context.grant_permissions(['clipboard-read', 'clipboard-write'])
                await page.locator('#copy-http').click()
                assert await page.evaluate('navigator.clipboard.readText()') == restored['http_proxy_uri']
                await page.locator('#copy-https').click()
                assert await page.evaluate('navigator.clipboard.readText()') == restored['https_proxy_uri']
                await page.screenshot(path='test-results/proxy-links.png', full_page=True)
                await page.locator('#open-dashboard').click()
                await page.screenshot(path='test-results/console.png', full_page=True)
                frame = page.frame_locator('#dashboard-frame')
                try:
                    await frame.get_by_text('[提供商1]', exact=True).first.wait_for(timeout=60000)
                except Exception:
                    await page.screenshot(path='test-results/console.png', full_page=True)
                    for child in page.frames:
                        print('Frame URL:', child.url)
                        if child.parent_frame:
                            print('Frame text:', (await child.locator('body').inner_text())[:2500])
                    raise
                await page.screenshot(path='test-results/console.png', full_page=True)
                await page.locator('[data-tab="config"]').click()
                assert (await page.locator('#subscription').input_value()).splitlines() == source_urls
                async with page.expect_response(lambda response: response.url == base + '/manage/config' and response.request.method == 'POST') as applied:
                    await page.locator('#url-form button[type="submit"]').click()
                assert (await applied.value).status == 200
                await page.locator('#url-form button[type="submit"]:not([disabled])').wait_for()
                await page.locator('#yaml').fill('proxy-groups:\n  - name: 浏览器策略\n    type: select\n    proxies: [DIRECT]\nrules:\n  - MATCH,浏览器策略\n')
                await page.locator('#yaml-form button').click()
                await page.get_by_text('配置已生效', exact=True).wait_for(timeout=60000)
                await page.locator('[data-tab="dashboard"]').click()
                await frame.get_by_text('浏览器策略', exact=True).first.wait_for(timeout=60000)
                await browser.close()
                print('Browser: login, dashboard automatic connection, multiple provider URLs and configuration import passed.')
    await runner.cleanup()
    fixture_directory.cleanup()


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
