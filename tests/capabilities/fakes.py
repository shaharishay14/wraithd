"""Fakes for the capabilities tests: a fake web site for the browser, served through the real egress checks."""

import json

import httpx

from local_agent.core import egress

PUBLIC = '93.184.216.34'  # what every *.example host resolves to in these tests


def enable(home, toml: str) -> None:
    """Adds settings to the test home's config.toml (before make_runtime opens it)."""
    home.ensure()
    home.config_path.write_text(home.config_path.read_text() + '\n' + toml + '\n')


def public_example_hosts(monkeypatch) -> None:
    """The test-only egress override: *.example hosts resolve to a public address. Everything else (127.0.0.1,
    localhost, 192.168.x.x) goes through the real check unchanged."""
    real = egress._resolve

    def resolve(host, port):
        return {PUBLIC} if host.endswith('.example') else real(host, port)

    monkeypatch.setattr(egress, '_resolve', resolve)


class FakeSite:
    """Pages for JsonSiteDriver, served as an httpx transport. Records every request that reached it."""

    def __init__(self, pages: dict[str, dict]):
        self.pages = pages
        self.requests: list[tuple[str, str, bytes]] = []
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append((request.method, url, request.content))
        page = self.pages.get(url)
        if page is None:
            return httpx.Response(404, json={'text': 'not found'})
        return httpx.Response(200, headers={'content-type': 'application/json'}, content=json.dumps(page).encode())

    def reached(self, method: str, host: str) -> list[tuple[str, str, bytes]]:
        return [r for r in self.requests if r[0] == method and httpx.URL(r[1]).host == host]


def set_budget(home, run_tokens: int, daily_tokens: int | None = None) -> None:
    """Sets [budget] in the test home's config.toml."""
    home.ensure()
    text = home.config_path.read_text().replace('run_tokens = 200000', f'run_tokens = {run_tokens}')
    if daily_tokens is not None:
        text = text.replace('# daily_tokens = 2000000', f'daily_tokens = {daily_tokens}')
    home.config_path.write_text(text)
