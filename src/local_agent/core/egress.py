"""
Outbound requests to addresses the model chose (reading a web page). The model's URL can come from a page or
an email, so it must not be able to reach anything that trusts the machine it runs on: on a laptop that's
localhost (the agent's own API, dev servers, Ollama), the home or office LAN (the router, printers, NAS), and
link-local addresses (169.254.x.x, which is also the cloud metadata service).

Checks, on every hop:
- http or https only, default ports only, no credentials in the URL
- every address the host resolves to must be public
- redirects are followed by hand, so each new address is checked too
- after connecting, the actual peer address is checked again (a DNS answer can change between the check
  and the connection), before any of the body is read
- the body is read up to a size cap
"""

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

MAX_REDIRECTS = 5
MAX_BYTES = 2_000_000
TIMEOUT_SECONDS = 10
DEFAULT_PORTS = {'http': 80, 'https': 443}


class EgressBlocked(Exception):
    """The URL points somewhere the agent may not go. The message is safe to show the model."""


@dataclass
class Fetched:
    url: str  # after redirects
    status_code: int
    content_type: str
    content: bytes
    truncated: bool  # the page was bigger than MAX_BYTES


def is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address.split('%', 1)[0])  # drop an IPv6 zone id
    if getattr(ip, 'ipv4_mapped', None):
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def _resolve(host: str, port: int) -> set[str]:
    """Every address a host resolves to. An IP address written in the URL is its own answer."""
    try:
        return {str(ipaddress.ip_address(host))}
    except ValueError:
        pass
    try:
        return {info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
    except socket.gaierror:
        raise EgressBlocked(f'{host} could not be found.') from None


def check_url(url: str) -> None:
    """Raises EgressBlocked unless the URL is a plain http(s) URL whose host resolves only to public addresses."""
    parts = urlsplit(url)
    if parts.scheme not in DEFAULT_PORTS:
        raise EgressBlocked(f'Only http and https links can be opened, not {parts.scheme or "this"}.')
    if parts.username or parts.password:
        raise EgressBlocked('Links with a username or password in them are not opened.')
    if not parts.hostname:
        raise EgressBlocked('The link has no host.')
    try:
        port = parts.port
    except ValueError:
        raise EgressBlocked('The link has an invalid port.') from None
    if port not in (None, DEFAULT_PORTS[parts.scheme]):
        raise EgressBlocked('Links to non-standard ports are not opened.')
    addresses = _resolve(parts.hostname, port or DEFAULT_PORTS[parts.scheme])
    if not addresses or not all(is_public(address) for address in addresses):
        raise EgressBlocked(f'{parts.hostname} is a private or internal address, which the agent may not open.')


def _peer_address(response: httpx.Response) -> str:
    """The address the connection actually went to, from httpx's network stream ('' if unknown)."""
    stream = response.extensions.get('network_stream')
    server = stream.get_extra_info('server_addr') if stream is not None else None
    return server[0] if server else ''


def safe_get(url: str, headers=None, transport: httpx.BaseTransport | None = None) -> Fetched:
    """GET a URL the model chose, with the checks above. Raises EgressBlocked or httpx errors.

    transport is for tests (httpx.MockTransport); the real one connects directly.
    """
    # trust_env=False: no proxy from the environment, because the checks are about where we connect
    with httpx.Client(transport=transport, trust_env=False, follow_redirects=False, timeout=TIMEOUT_SECONDS) as client:
        for _ in range(MAX_REDIRECTS + 1):
            check_url(url)
            with client.stream('GET', url, headers=headers) as response:
                peer = _peer_address(response)
                if peer and not is_public(peer):
                    raise EgressBlocked('The link resolved to a private or internal address, which the agent may '
                                        'not open.')
                if response.is_redirect:
                    url = urljoin(url, response.headers.get('location', ''))
                    continue
                response.raise_for_status()
                content, truncated = b'', False
                for chunk in response.iter_bytes(chunk_size=65536):
                    content += chunk
                    if len(content) > MAX_BYTES:
                        content, truncated = content[:MAX_BYTES], True
                        break
                return Fetched(url=url, status_code=response.status_code,
                               content_type=response.headers.get('content-type', ''), content=content,
                               truncated=truncated)
    raise EgressBlocked(f'The link redirected more than {MAX_REDIRECTS} times.')
