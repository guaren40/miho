import json
import os
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch
from aiohttp import web, WSMsgType
from aiohttp.test_utils import TestClient, TestServer
import yaml
from app.state import State
from app.core import normalize
from app.server import create_app


class StateTests(unittest.TestCase):
    def test_credentials_and_sessions_persist(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'ADMIN_PASSWORD': 'first-password'}):
            state = State(directory)
            self.assertTrue(state.verify_password('first-password'))
            self.assertFalse(state.verify_password('wrong'))
            token = state.session()
            self.assertTrue(state.verify_session(token))
            self.assertFalse(state.verify_session(token + 'x'))
            os.environ['ADMIN_PASSWORD'] = 'changed-environment'
            restored = State(directory)
            self.assertEqual(state.data, restored.data)
            self.assertTrue(restored.verify_session(token))
            self.assertNotIn('first-password', restored.path.read_text())
            self.assertEqual(restored.path.stat().st_mode & 0o777, 0o600)

    def test_zeabur_password(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'ZEABUR': '1', 'PASSWORD': 'zeabur-password', 'ADMIN_PASSWORD': ''}):
            self.assertTrue(State(directory).verify_password('zeabur-password'))

    def test_import_cannot_replace_managed_listeners(self):
        data = {'proxy_user': 'test', 'proxy_password': 'secret', 'api_secret': 'token', 'vless_uuid': 'test-uuid'}
        config = yaml.safe_load(normalize('mixed-port: 1234\nport: 80\nauthentication: []\nskip-auth-prefixes: [0.0.0.0/0]\nexternal-controller: 0.0.0.0:80\nexternal-controller-unix: /tmp/public\ntun: {enable: true}\nlisteners: []\n', data))
        self.assertEqual(config['mixed-port'], 7890)
        self.assertEqual(config['authentication'], ['test:secret'])
        self.assertEqual(config['skip-auth-prefixes'], [])
        self.assertEqual(config['external-controller'], '127.0.0.1:9090')
        self.assertFalse(config['tun']['enable'])
        self.assertNotIn('external-controller-unix', config)
        self.assertNotIn('port', config)
        self.assertEqual(config['listeners'][0]['users'][0]['uuid'], 'test-uuid')
        self.assertEqual(config['listeners'][0]['listen'], '127.0.0.1')
        self.assertTrue(config['listeners'][0]['allow-insecure'])
        self.assertEqual(config['rules'], ['MATCH,节点选择'])

    def test_reject_non_yaml_subscription(self):
        with self.assertRaises(ValueError):
            normalize('dm1lc3M6Ly9hYmNk', {})
        with self.assertRaises(ValueError):
            normalize('rules: [', {})


class FakeCore:
    process = type('Process', (), {'returncode': None})()
    text = ''
    def __init__(self, state): self.state = state
    async def start(self, session): pass
    async def stop(self): pass
    async def monitor(self, session):
        import asyncio
        await asyncio.Event().wait()
    def source_text(self): return self.text
    async def apply(self, text, session, subscription_url=None, subscription_urls=None):
        self.text = text
        if subscription_urls is not None:
            self.state.data['subscription_urls'] = list(subscription_urls)
            self.state.data['subscription_url'] = subscription_urls[0] if subscription_urls else ''
            self.state.save()


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'ADMIN_PASSWORD': 'password', 'PUBLIC_URL': '', 'ZEABUR_WEB_URL': ''})
        self.env.start()
        self.state = State(self.temp.name)
        upstream = web.Application()
        async def version(request):
            if request.headers.get('Authorization') != 'Bearer ' + self.state.data['api_secret']:
                return web.Response(status=401)
            return web.json_response({'version': 'fixture'})
        async def traffic(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.send_json({'up': 42, 'down': 24})
            async for _ in ws: pass
            return ws
        upstream.router.add_get('/version', version)
        upstream.router.add_get('/traffic', traffic)
        async def proxy_name(request):
            return web.json_response({'name': request.match_info['name']})
        upstream.router.add_get('/proxies/{name}', proxy_name)
        self.upstream = TestServer(upstream, port=9090)
        await self.upstream.start_server()
        self.core = FakeCore(self.state)
        self.client = TestClient(TestServer(create_app(self.state, self.core)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.upstream.close()
        self.env.stop()
        self.temp.cleanup()

    async def login(self):
        response = await self.client.post('/manage/login', json={'password': 'password'})
        self.assertEqual(response.status, 200)
        self.assertTrue(response.cookies['miho_session']['httponly'])

    async def test_auth_and_same_origin(self):
        self.assertEqual((await self.client.get('/manage/info')).status, 401)
        self.assertEqual((await self.client.get('/api/version')).status, 401)
        self.assertEqual((await self.client.post('/manage/login', json={'password': 'wrong'})).status, 401)
        await self.login()
        self.assertEqual((await self.client.get('/api/version')).status, 200)
        self.assertEqual((await self.client.post('/manage/logout', headers={'Origin': 'https://evil.example'}, json={})).status, 403)
        self.assertEqual((await self.client.patch('/api/configs', json={'secret': ''})).status, 400)
        self.assertEqual((await self.client.post('/api/upgrade')).status, 403)

    async def test_dashboard_auto_connect_and_export(self):
        await self.login()
        response = await self.client.get('/dashboard', allow_redirects=False)
        self.assertEqual(response.status, 302)
        location = response.headers['Location']
        query = parse_qs(urlsplit(location).fragment.partition('?')[2])
        self.assertEqual(query['secondaryPath'], ['/api'])
        self.assertIn('disableTunMode=1', location)
        self.assertNotIn(self.state.data['api_secret'], location)
        response = await self.client.get('/manage/client.yaml')
        config = yaml.safe_load(await response.text())
        self.assertEqual(config['proxies'][0]['uuid'], self.state.data['vless_uuid'])
        self.assertEqual(config['proxies'][0]['ws-opts']['path'], '/tunnel')

    async def test_controller_websocket(self):
        await self.login()
        async with self.client.ws_connect('/api/traffic') as ws:
            message = await ws.receive()
            self.assertEqual(message.type, WSMsgType.TEXT)
            self.assertEqual(json.loads(message.data)['up'], 42)

    async def test_api_token_for_native_controllers(self):
        response = await self.client.get('/api/version', headers={'Authorization': 'Bearer ' + self.state.data['api_secret']})
        self.assertEqual(response.status, 200)

    async def test_encoded_node_names_and_managed_endpoint_paths(self):
        await self.login()
        response = await self.client.get('/api/proxies/Region%2FNode')
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())['name'], 'Region/Node')
        self.assertEqual((await self.client.patch('/api/configs/', json={'secret': ''})).status, 400)
        self.assertEqual((await self.client.patch('/api//configs', json={'secret': ''})).status, 400)
