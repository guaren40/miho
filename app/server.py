import asyncio
from collections import defaultdict, deque
import contextlib
import hmac
import json
import os
from pathlib import Path
import time
from urllib.parse import urlencode, urlsplit
import aiohttp
from aiohttp import web
import yaml
from yarl import URL
from .core import MAX_CONFIG

STATIC = Path(__file__).resolve().parent.parent / 'static'
UI = Path(os.getenv('ZASHBOARD_DIR', '/opt/zashboard'))
HOP = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization', 'te',
       'trailer', 'transfer-encoding', 'upgrade', 'host', 'cookie', 'authorization', 'content-length'}


def public_url(request):
    configured = os.getenv('PUBLIC_URL') or os.getenv('ZEABUR_WEB_URL', '')
    if configured:
        if not configured.startswith(('http://', 'https://')):
            configured = 'https://' + configured
        parsed = urlsplit(configured)
        if parsed.scheme in ('http', 'https') and parsed.hostname and not parsed.username:
            return parsed.scheme + '://' + parsed.netloc
    scheme = request.headers.get('X-Forwarded-Proto', request.scheme).split(',')[0].strip()
    if scheme not in ('http', 'https'):
        scheme = 'https'
    return scheme + '://' + request.host


def client_node(request, state):
    parsed = urlsplit(public_url(request))
    return {'name': 'Miho', 'type': 'vless', 'server': parsed.hostname,
            'port': parsed.port or (443 if parsed.scheme == 'https' else 80),
            'uuid': state.data['vless_uuid'], 'udp': True, 'tls': parsed.scheme == 'https',
            'servername': parsed.hostname, 'network': 'ws',
            'ws-opts': {'path': '/tunnel', 'headers': {'Host': parsed.netloc}}}


async def bridge(request, upstream):
    downstream = web.WebSocketResponse(max_msg_size=8 * 1024 * 1024, heartbeat=30)
    await downstream.prepare(request)

    async def relay(source, target):
        async for message in source:
            if message.type == aiohttp.WSMsgType.TEXT:
                await target.send_str(message.data)
            elif message.type == aiohttp.WSMsgType.BINARY:
                await target.send_bytes(message.data)
            elif message.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                break
    tasks = [asyncio.create_task(relay(downstream, upstream)), asyncio.create_task(relay(upstream, downstream))]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await upstream.close()
        await downstream.close()
    return downstream


def create_app(state, core):
    app = web.Application(client_max_size=MAX_CONFIG + 65536)
    attempts = defaultdict(deque)

    def authorized(request):
        token = request.headers.get('Authorization', '')
        return state.verify_session(request.cookies.get('miho_session', '')) or (
            token.startswith('Bearer ') and hmac.compare_digest(token[7:], state.data['api_secret']))

    @web.middleware
    async def gate(request, handler):
        protected = request.path.startswith(('/manage/', '/api/', '/ui/')) or request.path in ('/dashboard', '/ui')
        if protected and request.path != '/manage/login' and not authorized(request):
            return web.json_response({'error': '请先登录'}, status=401)
        origin = request.headers.get('Origin')
        if request.path != '/tunnel' and origin and (request.method not in ('GET', 'HEAD', 'OPTIONS') or request.headers.get('Upgrade', '').lower() == 'websocket'):
            if origin.rstrip('/') != public_url(request):
                return web.json_response({'error': '跨站请求已拒绝'}, status=403)
        try:
            response = await handler(request)
        except (ValueError, yaml.YAMLError) as error:
            response = web.json_response({'error': str(error)}, status=400)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            response = web.json_response({'error': '连接失败，请检查目标地址或稍后重试'}, status=502)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        if protected:
            response.headers['Cache-Control'] = 'no-store'
        return response

    app.middlewares.append(gate)

    async def login(request):
        # Bound memory while applying a per-client limit before expensive PBKDF2.
        now = time.monotonic()
        if len(attempts) > 4096:
            for key in list(attempts):
                if not attempts[key] or attempts[key][-1] < now - 60:
                    attempts.pop(key, None)
        address = request.remote or 'unknown'
        bucket = attempts[address]
        while bucket and bucket[0] < now - 60:
            bucket.popleft()
        if len(bucket) >= 10:
            return web.json_response({'error': '尝试过多，请一分钟后重试'}, status=429)
        bucket.append(now)
        data = await request.json()
        password = data.get('password', '') if isinstance(data, dict) else ''
        if not isinstance(password, str) or len(password) > 1024 or not await asyncio.to_thread(state.verify_password, password):
            return web.json_response({'error': '密码错误'}, status=401)
        response = web.json_response({'ok': True})
        response.set_cookie('miho_session', state.session(), httponly=True, samesite='Strict',
                            secure=public_url(request).startswith('https:'), max_age=7 * 86400, path='/')
        return response

    async def logout(request):
        response = web.json_response({'ok': True})
        response.del_cookie('miho_session', path='/')
        return response

    async def health(request):
        if core.process is None or core.process.returncode is not None:
            return web.json_response({'status': 'starting'}, status=503)
        try:
            async with app['client'].get('http://127.0.0.1:9090/version', headers=core_headers(),
                                         timeout=aiohttp.ClientTimeout(total=2)) as response:
                return web.json_response({'status': 'ok' if response.status == 200 else 'starting'},
                                         status=200 if response.status == 200 else 503)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return web.json_response({'status': 'starting'}, status=503)

    def core_headers():
        return {'Authorization': 'Bearer ' + state.data['api_secret']}

    async def info(request):
        node = client_node(request, state)
        query = urlencode({'encryption': 'none', 'type': 'ws', 'host': node['ws-opts']['headers']['Host'],
                           'path': '/tunnel', 'security': 'tls' if node['tls'] else 'none', 'sni': node['server']})
        server = '[' + node['server'] + ']' if ':' in node['server'] else node['server']
        uri = f"vless://{node['uuid']}@{server}:{node['port']}?{query}#Miho"
        versions = json.loads((STATIC.parent / 'versions.json').read_text())
        return web.json_response({'versions': versions, 'subscription_url': state.data['subscription_url'],
                                  'public_url': public_url(request), 'vless_uri': uri,
                                  'proxy_user': state.data['proxy_user'], 'proxy_password': state.data['proxy_password'],
                                  'api_token': state.data['api_secret'], 'status': 'ready' if core.process and core.process.returncode is None else 'starting'})

    async def download_subscription(url):
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username:
            raise ValueError('订阅地址必须是 HTTP(S) URL')
        async with app['client'].get(url, headers={'User-Agent': 'clash.meta/Miho'},
                                     timeout=aiohttp.ClientTimeout(total=30)) as response:
            if response.status != 200:
                raise ValueError('订阅下载失败，HTTP ' + str(response.status))
            content = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                content.extend(chunk)
                if len(content) > MAX_CONFIG:
                    raise ValueError('订阅超过 5 MiB')
            try:
                return content.decode('utf-8-sig')
            except UnicodeDecodeError as error:
                raise ValueError('订阅不是 UTF-8 YAML') from error

    async def config(request):
        if request.method == 'GET':
            return web.json_response({'text': core.source_text()})
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError('请求需要 JSON 对象')
        url = body.get('url', '').strip()
        text = await download_subscription(url) if url else body.get('text', '')
        if not isinstance(text, str):
            raise ValueError('配置必须是文本')
        await core.apply(text, app['client'], subscription_url=url)
        return web.json_response({'ok': True})

    async def refresh(request):
        url = state.data['subscription_url']
        if not url:
            raise ValueError('尚未保存订阅地址')
        await core.apply(await download_subscription(url), app['client'])
        return web.json_response({'ok': True})

    async def client_config(request):
        config = {'mixed-port': 7890, 'allow-lan': False, 'mode': 'rule', 'log-level': 'info',
                  'proxies': [client_node(request, state)],
                  'proxy-groups': [{'name': '节点选择', 'type': 'select', 'proxies': ['Miho', 'DIRECT']}],
                  'rules': ['MATCH,节点选择']}
        return web.Response(text=yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
                            content_type='application/yaml', headers={'Content-Disposition': 'attachment; filename="miho-client.yaml"'})

    async def dashboard(request):
        parsed = urlsplit(public_url(request))
        query = urlencode({'protocol': parsed.scheme, 'hostname': parsed.hostname,
                           'port': parsed.port or (443 if parsed.scheme == 'https' else 80),
                           'secondaryPath': '/api', 'disableUpgradeCore': '1', 'disableTunMode': '1', 'label': 'Miho'})
        raise web.HTTPFound('/ui/#/setup?' + query)

    async def ui(request):
        relative = request.match_info.get('path', '') or 'index.html'
        path = (UI / relative).resolve()
        if not path.is_relative_to(UI.resolve()) or not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    async def api(request):
        path = request.match_info['path']
        if any(part in ('.', '..') for part in path.split('/')) or '\\' in path:
            raise ValueError('无效的控制器路径')
        # Canonicalize for access checks, but preserve encoded slashes in node names.
        checked_path = '/'.join(part for part in path.split('/') if part)
        if checked_path.split('/')[0] in ('upgrade', 'restart'):
            return web.json_response({'message': '请通过重新部署 ARM64 镜像更新或重启'}, status=403)
        if checked_path == 'configs' and request.method == 'PUT':
            body = await request.json()
            if body.get('path'):
                raise ValueError('请在 Miho 配置页导入配置，不支持任意文件路径')
            text = body.get('payload', core.source_text())
            if not isinstance(text, str):
                raise ValueError('payload 必须是 YAML 文本')
            await core.apply(text, app['client'])
            return web.Response(status=204)
        if checked_path == 'configs' and request.method == 'PATCH':
            body = await request.json()
            if not isinstance(body, dict) or set(body) - {'mode', 'log-level', 'ipv6', 'unified-delay', 'tcp-concurrent'}:
                raise ValueError('端口、认证与监听地址由 Miho 管理；请在配置页修改其他设置')
        raw_path = request.rel_url.raw_path
        if not raw_path.startswith('/api/'):
            raise ValueError('无效的控制器路径')
        target = URL('http://127.0.0.1:9090/' + raw_path[len('/api/'):], encoded=True)
        query = [(key, value) for key, value in request.query.items() if key != 'token']
        headers = {key: value for key, value in request.headers.items() if key.lower() not in HOP}
        headers.update(core_headers())
        if request.headers.get('Upgrade', '').lower() == 'websocket':
            async with app['client'].ws_connect(target, params=query, headers=core_headers(),
                                                 max_msg_size=8 * 1024 * 1024) as upstream:
                return await bridge(request, upstream)
        async with app['client'].request(request.method, target, params=query, headers=headers,
                                          data=await request.read(), auto_decompress=False, allow_redirects=False) as upstream:
            response = web.StreamResponse(status=upstream.status,
                                          headers={key: value for key, value in upstream.headers.items() if key.lower() not in HOP and key.lower() != 'set-cookie'})
            response.headers['Cache-Control'] = 'no-store'
            response.headers['X-Content-Type-Options'] = 'nosniff'
            await response.prepare(request)
            async for chunk in upstream.content.iter_chunked(65536):
                await response.write(chunk)
            await response.write_eof()
            return response

    async def tunnel(request):
        if request.headers.get('Upgrade', '').lower() != 'websocket':
            raise web.HTTPBadRequest(text='WebSocket required')
        async with app['client'].ws_connect('http://127.0.0.1:7891/tunnel',
                                             max_msg_size=8 * 1024 * 1024) as upstream:
            return await bridge(request, upstream)

    async def root(request):
        return web.FileResponse(STATIC / 'index.html')

    async def assets(request):
        name = request.match_info['name']
        if name not in ('app.js', 'style.css'):
            raise web.HTTPNotFound()
        return web.FileResponse(STATIC / name)

    async def lifecycle(application):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
            application['client'] = session
            await core.start(session)
            monitor = asyncio.create_task(core.monitor(session))
            try:
                yield
            finally:
                monitor.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await monitor
                await core.stop()

    app.cleanup_ctx.append(lifecycle)
    app.router.add_get('/', root)
    app.router.add_get('/assets/{name}', assets)
    app.router.add_get('/healthz', health)
    app.router.add_post('/manage/login', login)
    app.router.add_post('/manage/logout', logout)
    app.router.add_get('/manage/info', info)
    app.router.add_route('*', '/manage/config', config)
    app.router.add_post('/manage/refresh', refresh)
    app.router.add_get('/manage/client.yaml', client_config)
    app.router.add_get('/dashboard', dashboard)
    app.router.add_get('/ui', dashboard)
    app.router.add_get('/ui/{path:.*}', ui)
    app.router.add_route('*', '/api/{path:.*}', api)
    app.router.add_get('/tunnel', tunnel)
    return app
