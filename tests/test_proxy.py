import os
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest.mock import patch
import yaml
from app.core import normalize
from app.proxy import certificate_host, prepare_proxy_tls, proxy_endpoint, proxy_uri

ENV = {'MIHO_HTTP_PROXY_URL': '', 'MIHO_HTTPS_PROXY_URL': '',
       'MIHO_PROXY_TLS_CERT': '', 'MIHO_PROXY_TLS_KEY': ''}


class ProxyTests(unittest.TestCase):
    def test_links_use_explicit_endpoint_and_encode_credentials(self):
        state = {'proxy_user': 'a:b', 'proxy_password': 'p@ss /#?'}
        with patch.dict(os.environ, ENV):
            self.assertEqual(proxy_uri('http', state), '')
            self.assertEqual(proxy_uri('https', state), '')
        with patch.dict(os.environ, {**ENV, 'MIHO_HTTP_PROXY_URL': 'http://example.com:30001/',
                                    'MIHO_HTTPS_PROXY_URL': 'https://[2001:db8::1]:30002'}):
            self.assertEqual(proxy_uri('http', state), 'http://a%3Ab:p%40ss%20%2F%23%3F@example.com:30001')
            self.assertEqual(proxy_uri('https', state), 'https://a%3Ab:p%40ss%20%2F%23%3F@[2001:db8::1]:30002')
            self.assertEqual(certificate_host(), '2001:db8::1')

    def test_invalid_endpoints_are_rejected(self):
        for value in ['https://example.com:7890', 'http://example.com', 'http://user:pass@example.com:7890',
                      'http://@example.com:7890', 'http://example.com:7890/api', 'http://example.com:7890?q=1',
                      'http://example.com:0', 'http://example.com:65536', 'http://bad host:7890']:
            with self.subTest(value=value), patch.dict(os.environ, {**ENV, 'MIHO_HTTP_PROXY_URL': value}):
                with self.assertRaises(ValueError):
                    proxy_endpoint('http')

    def test_import_cannot_override_https_listener(self):
        state = {'proxy_user': 'miho', 'proxy_password': 'password', 'api_secret': 'token', 'vless_uuid': 'uuid'}
        tls = {'certificate': '/data/proxy.crt', 'private-key': '/data/proxy.key'}
        config = yaml.safe_load(normalize('listeners: [{type: http, port: 7892, users: []}]', state, tls))
        listener = config['listeners'][1]
        self.assertEqual(listener['type'], 'http')
        self.assertEqual(listener['port'], 7892)
        self.assertEqual(listener['users'], [{'username': 'miho', 'password': 'password'}])
        self.assertEqual(listener['certificate'], tls['certificate'])
        self.assertEqual(listener['private-key'], tls['private-key'])


class CertificateTests(unittest.IsolatedAsyncioTestCase):
    async def test_certificate_is_persistent_and_matches_proxy_host(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {**ENV, 'MIHO_HTTPS_PROXY_URL': 'https://proxy.example.com:30002'}):
            tls = await prepare_proxy_tls(directory)
            first = Path(tls['certificate']).read_bytes()
            self.assertTrue(tls['self_signed'])
            self.assertEqual(Path(tls['private-key']).stat().st_mode & 0o777, 0o600)
            self.assertEqual(Path((await prepare_proxy_tls(directory))['certificate']).read_bytes(), first)
            context = ssl.create_default_context(cafile=tls['certificate'])
            server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server.load_cert_chain(tls['certificate'], tls['private-key'])
            import asyncio
            async def close(reader, writer):
                writer.close()
                await writer.wait_closed()
            listener = await asyncio.start_server(close, '127.0.0.1', 0, ssl=server)
            async with listener:
                _, writer = await asyncio.open_connection('127.0.0.1', listener.sockets[0].getsockname()[1],
                                                         ssl=context, server_hostname='proxy.example.com')
                writer.close()
                await writer.wait_closed()

    async def test_custom_certificate_pair_is_required(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {**ENV, 'MIHO_PROXY_TLS_CERT': '/missing.crt'}):
            with self.assertRaises(ValueError):
                await prepare_proxy_tls(directory)
