"""HTTP(S) proxy link exports and persistent HTTPS listener certificates."""
import asyncio
import ipaddress
import os
from pathlib import Path
import re
import ssl
from urllib.parse import quote, urlsplit
from .state import atomic_write


def proxy_endpoint(scheme):
    key = f'MIHO_{scheme.upper()}_PROXY_URL'
    value = os.getenv(key, '').strip()
    if not value:
        return ''
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == scheme and parsed.hostname and parsed.port
                 and parsed.username is None and parsed.password is None
                 and parsed.path in ('', '/') and not parsed.query and not parsed.fragment
                 and not any(c.isspace() for c in value))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(f'{key} 需要 {scheme}://主机:公网映射端口，不要填写凭据或路径')
    return f'{scheme}://{parsed.netloc}'


def proxy_uri(scheme, state):
    endpoint = proxy_endpoint(scheme)
    if not endpoint:
        return ''
    parsed = urlsplit(endpoint)
    credentials = quote(state['proxy_user'], safe='') + ':' + quote(state['proxy_password'], safe='')
    return f'{parsed.scheme}://{credentials}@{parsed.netloc}'


def certificate_host():
    # Never derive the proxy hostname from an untrusted HTTP Host header.
    host = urlsplit(proxy_endpoint('https') or 'https://localhost').hostname
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        host = host.encode('idna').decode('ascii')
        if len(host) > 253 or not re.fullmatch(r'[A-Za-z0-9.-]+', host):
            raise ValueError('HTTPS 代理主机名无效')
        return host


def public_certificate(path):
    # Custom PEM bundles sometimes contain both the chain and the private key.
    certificates = re.findall(rb'-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----',
                              Path(path).read_bytes(), re.DOTALL)
    if not certificates:
        raise ValueError('代理证书中没有公开证书')
    return b'\n'.join(certificates) + b'\n'


async def prepare_proxy_tls(directory):
    proxy_endpoint('http')
    proxy_endpoint('https')
    certificate = os.getenv('MIHO_PROXY_TLS_CERT', '')
    private_key = os.getenv('MIHO_PROXY_TLS_KEY', '')
    if bool(certificate) != bool(private_key):
        raise ValueError('MIHO_PROXY_TLS_CERT 和 MIHO_PROXY_TLS_KEY 必须同时设置')
    generated = not certificate
    if generated:
        directory = Path(directory)
        certificate = str(directory / 'proxy.crt')
        private_key = str(directory / 'proxy.key')
        host = certificate_host()
        metadata = directory / 'proxy-host.txt'
        if (not Path(certificate).exists() or not Path(private_key).exists()
                or not metadata.exists() or metadata.read_text().strip() != host):
            names = ['DNS:localhost', 'IP:127.0.0.1', 'IP:::1']
            try:
                ipaddress.ip_address(host)
                names.append('IP:' + host)
            except ValueError:
                names.append('DNS:' + host)
            cert_temp, key_temp = certificate + '.new', private_key + '.new'
            process = await asyncio.create_subprocess_exec(
                'openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256',
                '-nodes', '-sha256', '-days', '3650', '-subj', '/CN=Miho HTTPS Proxy',
                '-addext', 'subjectAltName=' + ','.join(dict.fromkeys(names)),
                '-addext', 'basicConstraints=critical,CA:FALSE',
                '-addext', 'extendedKeyUsage=serverAuth',
                '-keyout', key_temp, '-out', cert_temp,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            try:
                await asyncio.wait_for(process.wait(), 15)
                if process.returncode:
                    raise RuntimeError('HTTPS 代理证书生成失败')
                for path in (cert_temp, key_temp):
                    os.chmod(path, 0o600)
                Path(key_temp).replace(private_key)
                Path(cert_temp).replace(certificate)
                atomic_write(metadata, host + '\n')
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
                Path(cert_temp).unlink(missing_ok=True)
                Path(key_temp).unlink(missing_ok=True)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, private_key)
    return {'certificate': str(Path(certificate).resolve()),
            'private-key': str(Path(private_key).resolve()), 'self_signed': generated}
