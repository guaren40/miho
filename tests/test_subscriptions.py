import json
import os
import tempfile
import unittest
from unittest.mock import patch
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import yaml
from app.state import State
from app.server import create_app
from app.subscriptions import parse_urls, merge_subscriptions
from test_app import FakeCore

NODE = {'name': '同名节点', 'type': 'http', 'server': '127.0.0.1', 'port': 9001}


class SubscriptionTests(unittest.TestCase):
    def test_source_deduplication_and_validation(self):
        self.assertEqual(parse_urls(' https://a.example/a\nhttps://b.example/b\nhttps://a.example/a\n'),
                         ['https://a.example/a', 'https://b.example/b'])
        for value in ([123], None, 'file:///etc/passwd', 'https://user:pass@example.com/a',
                      [f'https://example.com/{i}' for i in range(21)]):
            with self.assertRaises(ValueError):
                parse_urls(value)

    def test_merge_conflicting_node_names_and_dialer_references(self):
        first = yaml.safe_dump({'proxies': [NODE, {**NODE, 'name': '出口', 'dialer-proxy': '同名节点'}],
                                'rules': ['MATCH,REJECT']}, allow_unicode=True)
        merged = yaml.safe_load(merge_subscriptions(['https://a.example', 'https://b.example'],
                                                   [first, yaml.safe_dump({'proxies': [NODE]})]))
        names = [node['name'] for node in merged['proxies']]
        self.assertEqual(len(set(names)), 3)
        self.assertEqual(merged['proxies'][1]['dialer-proxy'], merged['proxies'][0]['name'])
        self.assertEqual(merged['rules'], ['MATCH,节点选择'])
        self.assertEqual(len(merged['proxy-groups']), 3)

    def test_provider_cache_paths_and_dynamic_node_names_do_not_collide(self):
        source = yaml.safe_dump({'proxy-providers': {'pool': {'type': 'http', 'url': 'https://nodes.example/yaml',
                                 'path': './same-cache.yaml', 'override': {'additional-prefix': '原前缀-'}}}})
        merged = yaml.safe_load(merge_subscriptions(['https://a.example', 'https://b.example'], [source, source]))
        providers = list(merged['proxy-providers'].values())
        self.assertNotEqual(providers[0]['path'], providers[1]['path'])
        self.assertNotEqual(providers[0]['override']['additional-prefix'], providers[1]['override']['additional-prefix'])
        self.assertTrue(providers[0]['override']['additional-prefix'].endswith('原前缀-'))
        self.assertEqual(merge_subscriptions(['https://a.example', 'https://b.example'], [source, source]),
                         yaml.safe_dump(merged, allow_unicode=True, sort_keys=False))

    def test_single_source_keeps_complete_config_and_bad_multi_source_is_rejected(self):
        original = 'mode: global\nrules:\n  - MATCH,REJECT\n'
        self.assertEqual(merge_subscriptions(['https://a.example'], [original]), original)
        for bad in ('proxies: [', 'not yaml', 'proxies: []', 'proxies: [123]'):
            with self.assertRaises(ValueError):
                merge_subscriptions(['https://a.example', 'https://b.example'], [yaml.safe_dump({'proxies': [NODE]}), bad])

    def test_legacy_state_retains_password_and_exposes_single_saved_source(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'ADMIN_PASSWORD': 'existing-password'}):
            state = State(directory)
            state.data.pop('subscription_urls')
            state.data['subscription_url'] = 'https://old.example/clash.yaml'
            state.save()
            restored = State(directory)
            self.assertEqual(restored.subscription_urls, ['https://old.example/clash.yaml'])
            self.assertTrue(restored.verify_password('existing-password'))


class ImportAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'ADMIN_PASSWORD': 'password', 'PUBLIC_URL': '', 'ZEABUR_WEB_URL': ''})
        self.env.start()
        self.state = State(self.temp.name)
        self.core = FakeCore(self.state)
        self.payloads = {'one': yaml.safe_dump({'proxies': [NODE], 'mode': 'global'}),
                         'two': yaml.safe_dump({'proxies': [NODE]})}
        self.hits = []
        fixture = web.Application()
        async def subscription(request):
            name = request.match_info['name']
            self.hits.append(name)
            if name not in self.payloads:
                return web.Response(status=503)
            return web.Response(text=self.payloads[name])
        fixture.router.add_get('/{name}', subscription)
        self.fixture = TestServer(fixture)
        await self.fixture.start_server()
        self.urls = [str(self.fixture.make_url('/one')), str(self.fixture.make_url('/two'))]
        self.client = TestClient(TestServer(create_app(self.state, self.core)))
        await self.client.start_server()
        self.assertEqual((await self.client.post('/manage/login', json={'password': 'password'})).status, 200)

    async def asyncTearDown(self):
        await self.client.close()
        await self.fixture.close()
        self.env.stop()
        self.temp.cleanup()

    async def test_import_refresh_persistence_and_failure_preservation(self):
        response = await self.client.post('/manage/config', json={'urls': self.urls + [self.urls[0]]})
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual((await response.json())['sources'], 2)
        self.assertEqual(self.state.subscription_urls, self.urls)
        self.assertEqual(State(self.temp.name).subscription_urls, self.urls)
        info = await (await self.client.get('/manage/info')).json()
        self.assertEqual(info['subscription_urls'], self.urls)
        self.payloads['one'] = yaml.safe_dump({'proxies': [{**NODE, 'port': 9002}]})
        self.hits.clear()
        self.assertEqual((await self.client.post('/manage/refresh', json={})).status, 200)
        self.assertCountEqual(self.hits, ['one', 'two'])
        self.assertEqual(yaml.safe_load(self.core.text)['proxies'][0]['port'], 9002)
        previous = self.core.text
        saved = self.state.path.read_bytes()
        self.payloads['two'] = 'proxies: ['
        self.assertEqual((await self.client.post('/manage/refresh', json={})).status, 400)
        self.assertEqual(self.core.text, previous)
        self.assertEqual(self.state.path.read_bytes(), saved)
        missing = str(self.fixture.make_url('/missing'))
        self.assertEqual((await self.client.post('/manage/config', json={'urls': [self.urls[0], missing]})).status, 400)
        self.assertEqual(self.state.subscription_urls, self.urls)
        self.assertEqual(self.core.text, previous)

    async def test_legacy_single_url_and_manual_yaml(self):
        response = await self.client.post('/manage/config', json={'url': self.urls[0]})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.core.text, self.payloads['one'])
        self.assertEqual(self.state.subscription_urls, [self.urls[0]])
        self.assertEqual((await self.client.post('/manage/config', json={'urls': []})).status, 400)
        self.assertEqual((await self.client.post('/manage/config', json={'text': ''})).status, 200)
        self.assertEqual(self.state.subscription_urls, [])
