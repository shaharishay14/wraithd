"""
The local API's guards, in one middleware that runs before any route: any website the owner visits can make
their browser send requests to 127.0.0.1, so

- Host must be this daemon's own address (127.0.0.1 or localhost, on its port): a DNS-rebinding name fails
- Origin, when a browser sends one, must be the daemon's own origin (or the one configured dev origin)
- remote access: each name in [remote] allowed_hosts (the machine's tailnet name, which `tailscale serve` puts in
  front of the API) is one more Host, and https://<name> one more Origin. Nothing else changes: the server still
  binds to 127.0.0.1 and the token is still required (docs/remote-access.md)
- every /api call except /api/health needs the token, as a Bearer header; static UI files don't
- no CORS headers, ever, and preflight requests are refused

The token lives in ~/.local-agent/api-token (mode 600) and reaches the UI once, in a URL fragment.
"""

import hmac
import json
import os
import re
import secrets
from pathlib import Path

TOKEN_FILE = 'api-token'
PUBLIC_PATHS = {'/api/health'}
HTTPS_PORT = 443  # what tailscale serve listens on
# a DNS name (box.tail1234.ts.net), optionally with a port: no scheme, path, wildcard or user
_LABEL = r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?'
REMOTE_HOST = re.compile(rf'{_LABEL}(?:\.{_LABEL})+(?::[0-9]{{1,5}})?')


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


def check_remote(values: dict) -> None:
    """The [remote] section: allowed_hosts is a list of host names, each spelled out (no '*', scheme or path)."""
    from local_agent.config import ConfigError

    for host in values['allowed_hosts']:
        if not isinstance(host, str) or not REMOTE_HOST.fullmatch(host.strip().lower()):
            raise ConfigError(f'[remote] allowed_hosts: {host!r} is not a host name. Use the machine\'s tailnet name '
                              'as `tailscale status` shows it, like "box.tail1234.ts.net" (no https://, no *).')


def remote_hosts(config) -> list[str]:
    """The host names [remote] allowed_hosts lets in, lowercased. Empty: the API is reachable from this machine only."""
    return [host.strip().lower() for host in config.section('remote')['allowed_hosts']]


def _split(host: str) -> tuple[str, int]:
    name, _, port = host.partition(':')
    return name, int(port) if port else HTTPS_PORT


def allowed_hosts(port: int, remote=()) -> set[str]:
    own = {f'127.0.0.1:{port}', f'localhost:{port}'}
    for host in remote:  # what tailscale serve passes on: the name, with its port only when it isn't 443
        name, remote_port = _split(host)
        own |= {f'{name}:{remote_port}'} | ({name} if remote_port == HTTPS_PORT else set())
    return own


def allowed_origins(port: int, dev_origin: str = '', remote=()) -> set[str]:
    own = {f'http://127.0.0.1:{port}', f'http://localhost:{port}'}
    for host in remote:  # https only: that's what tailscale serve gives
        name, remote_port = _split(host)
        own.add(f'https://{name}' if remote_port == HTTPS_PORT else f'https://{name}:{remote_port}')
    return own | {dev_origin.rstrip('/')} if dev_origin else own


async def _refuse(send, status: int, code: str, message: str) -> None:
    body = json.dumps({'error': {'code': code, 'message': message}}).encode()
    await send({'type': 'http.response.start', 'status': status,
                'headers': [(b'content-type', b'application/json'), (b'content-length', str(len(body)).encode())]})
    await send({'type': 'http.response.body', 'body': body})


class Guard:
    """ASGI middleware with the checks above. Lifespan and other non-HTTP scopes pass through."""

    def __init__(self, app, token: str, port: int, dev_origin: str = '', remote=()):
        self.app, self.token = app, token
        self.hosts, self.origins = allowed_hosts(port, remote), allowed_origins(port, dev_origin, remote)

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = {name.decode('latin-1').lower(): value.decode('latin-1') for name, value in scope['headers']}
        if headers.get('host', '').lower() not in self.hosts:
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
