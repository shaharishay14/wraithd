"""
The local API's guards, in one middleware that runs before any route: any website the owner visits can make
their browser send requests to 127.0.0.1, so

- Host must be this daemon's own address (127.0.0.1 or localhost, on its port): a DNS-rebinding name fails
- Origin, when a browser sends one, must be the daemon's own origin (or the one configured dev origin)
- every /api call except /api/health needs the token, as a Bearer header; static UI files don't
- no CORS headers, ever, and preflight requests are refused

The token lives in ~/.local-agent/api-token (mode 600) and reaches the UI once, in a URL fragment.
"""

import hmac
import json
import os
import secrets
from pathlib import Path

TOKEN_FILE = 'api-token'
PUBLIC_PATHS = {'/api/health'}


def load_or_create_token(root: Path) -> str:
    """The daemon's token, created on first start. Only the owner's account can read the file."""
    path = Path(root) / TOKEN_FILE
    if path.exists():
        token = path.read_text().strip()
        if len(token) >= 32:
            return token
    token = secrets.token_urlsafe(32)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w') as out:
        out.write(token + '\n')
    path.chmod(0o600)
    return token


def allowed_hosts(port: int) -> set[str]:
    return {f'127.0.0.1:{port}', f'localhost:{port}'}


def allowed_origins(port: int, dev_origin: str = '') -> set[str]:
    own = {f'http://127.0.0.1:{port}', f'http://localhost:{port}'}
    return own | {dev_origin.rstrip('/')} if dev_origin else own


async def _refuse(send, status: int, code: str, message: str) -> None:
    body = json.dumps({'error': {'code': code, 'message': message}}).encode()
    await send({'type': 'http.response.start', 'status': status,
                'headers': [(b'content-type', b'application/json'), (b'content-length', str(len(body)).encode())]})
    await send({'type': 'http.response.body', 'body': body})


class Guard:
    """ASGI middleware with the checks above. Lifespan and other non-HTTP scopes pass through."""

    def __init__(self, app, token: str, port: int, dev_origin: str = ''):
        self.app, self.token = app, token
        self.hosts, self.origins = allowed_hosts(port), allowed_origins(port, dev_origin)

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = {name.decode('latin-1').lower(): value.decode('latin-1') for name, value in scope['headers']}
        if headers.get('host', '') not in self.hosts:
            return await _refuse(send, 403, 'bad_host', 'This address is not allowed.')
        origin = headers.get('origin')
        if origin is not None and origin.rstrip('/') not in self.origins:
            return await _refuse(send, 403, 'bad_origin', 'Requests from other sites are not allowed.')
        if scope['method'] == 'OPTIONS':
            return await _refuse(send, 403, 'bad_origin', 'Cross-origin requests are not allowed.')
        path = scope['path']
        if path.startswith('/api/') and path not in PUBLIC_PATHS:
            scheme, _, given = headers.get('authorization', '').partition(' ')
            if scheme.lower() != 'bearer' or not hmac.compare_digest(given.strip().encode(), self.token.encode()):
                return await _refuse(send, 401, 'unauthorized', 'Open the link printed by `local-agent daemon`.')
        return await self.app(scope, receive, send)
