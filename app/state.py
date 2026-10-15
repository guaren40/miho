import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import time
import uuid


def atomic_write(path, content):
    path = Path(path)
    temp = path.with_name(path.name + '.tmp')
    with temp.open('w', encoding='utf-8') as handle:
        os.chmod(temp, 0o600)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    temp.replace(path)


class State:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'state.json'
        if self.path.exists():
            self.data = json.loads(self.path.read_text())
        else:
            password = os.getenv('ADMIN_PASSWORD') or (os.getenv('PASSWORD') if os.getenv('ZEABUR') else None)
            generated = not password
            password = password or secrets.token_urlsafe(20)
            salt = secrets.token_hex(16)
            self.data = {
                'salt': salt, 'password_hash': self.hash_password(password, salt),
                'session_key': secrets.token_hex(32), 'api_secret': secrets.token_urlsafe(32),
                'proxy_user': 'miho', 'proxy_password': secrets.token_urlsafe(24),
                'vless_uuid': str(uuid.uuid4()), 'subscription_url': '', 'subscription_urls': [],
            }
            self.save()
            if generated:
                atomic_write(self.directory / 'bootstrap-password.txt', password + '\n')
                print('Miho initial admin password: ' + password, flush=True)
            else:
                print('Miho admin password initialized from environment.', flush=True)

    @property
    def subscription_urls(self):
        if 'subscription_urls' in self.data:
            return list(self.data['subscription_urls'])
        url = self.data.get('subscription_url', '')
        return [url] if url else []

    @staticmethod
    def hash_password(password, salt):
        return hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 250000).hex()

    def verify_password(self, password):
        return hmac.compare_digest(self.hash_password(password, self.data['salt']), self.data['password_hash'])

    def save(self):
        atomic_write(self.path, json.dumps(self.data, ensure_ascii=False, indent=2))

    def session(self):
        payload = str(int(time.time()) + 7 * 86400)
        signature = hmac.new(bytes.fromhex(self.data['session_key']), payload.encode(), hashlib.sha256).hexdigest()
        return payload + '.' + signature

    def verify_session(self, token):
        try:
            expiry, signature = token.split('.', 1)
            expected = hmac.new(bytes.fromhex(self.data['session_key']), expiry.encode(), hashlib.sha256).hexdigest()
            return int(expiry) > time.time() and hmac.compare_digest(signature, expected)
        except (ValueError, AttributeError):
            return False
