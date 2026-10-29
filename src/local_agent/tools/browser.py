"""
The browser: a headless Chromium (Playwright) the agent can open pages in, read, and act on.

- Reads: browser_open(url), browser_read(), browser_screenshot(). Page text is third-party content, so it taints
  the run like read_website does; browser_open follows the gate's link rule (after a private read, only links the
  owner wrote open freely).
- Writes: browser_click, browser_type, browser_submit. Each names `to_site`, the host the action goes to (where a
  link points, where a form posts, or the page itself), and the tool refuses unless that's the real target. The
  gate treats to_site as a recipient: an action is asked about, runs unattended only if pre-approved, and never
  toward a site that came from third-party text (a page saying "submit this form to attacker.example").
- Every request the page makes goes through the egress guard, not just navigations: the browser can't resolve a
  single host itself (--host-resolver-rules), so nothing reaches the network unless EgressRouter fetched it. Page
  scripts can't reach 127.0.0.1, the LAN or the daemon API; WebSockets, service workers and WebRTC are off.
- Each run gets its own fresh browser context: no cookies, no logins, nothing kept. It closes when idle.

Playwright is the optional extra `local-agent[browser]`, imported only when a browser starts. The Playwright part
sits behind BrowserDriver, so tests drive the same tools and router with a fake.
"""

import asyncio
import contextlib
import importlib.util
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from urllib.parse import urljoin, urlsplit

import httpx

from local_agent.core import egress
from local_agent.tools.base import function_tool, tool_error

logger = logging.getLogger(__name__)

OPEN, READ, SCREENSHOT = 'browser_open', 'browser_read', 'browser_screenshot'
CLICK, TYPE, SUBMIT = 'browser_click', 'browser_type', 'browser_submit'
READS, WRITES = (OPEN, READ, SCREENSHOT), (CLICK, TYPE, SUBMIT)
TEXT_CHARS = 8_000  # page text per result
MAX_ELEMENTS = 80
MAX_BLOCKED_SHOWN = 10
NO_NETWORK = ('about:', 'data:', 'blob:')  # no request leaves the browser for these
FORWARDED_HEADERS = ('accept', 'accept-language', 'content-type', 'user-agent', 'referer')


# --- the egress router: every request the page makes --------------------------------------------------------

@dataclass(frozen=True)
class Blocked:
    reason: str


@dataclass(frozen=True)
class Fulfill:
    status: int
    content_type: str
    body: bytes
    headers: dict = field(default_factory=dict)


@dataclass(frozen=True)
class PassThrough:
    """Nothing to fetch (data:, about:): the browser handles it itself."""


class EgressRouter:
    """
    Decides every request a page makes, and makes it on the page's behalf through the egress checks. GETs go
    through egress.safe_get (every hop and the connected peer checked); other methods get the same URL check and
    peer check, and aren't redirected for the browser (it asks again for the new location, which is checked again).
    transport is for tests (httpx.MockTransport), like the web tools'.
    """

    def __init__(self, transport=None):
        self.transport = transport
        self.blocked: list[dict[str, str]] = []  # this run's blocked requests, for the tool results

    async def decide(self, method: str, url: str, headers: dict | None = None, body: bytes | None = None,
                     resource_type: str = '') -> Blocked | Fulfill | PassThrough:
        if url.startswith(NO_NETWORK):
            return PassThrough()
        if resource_type == 'websocket' or urlsplit(url).scheme in ('ws', 'wss'):
            return self._block(url, 'WebSockets are not opened.')
        sent = {key: value for key, value in (headers or {}).items() if key.lower() in FORWARDED_HEADERS}
        try:
            if method.upper() == 'GET':
                fetched = await asyncio.to_thread(egress.safe_get, url, sent, self.transport)
                return Fulfill(fetched.status_code, fetched.content_type, fetched.content)
            return await asyncio.to_thread(self._send, method.upper(), url, sent, body)
        except egress.EgressBlocked as e:
            return self._block(url, str(e))
        except httpx.HTTPStatusError as e:
            return Fulfill(e.response.status_code, e.response.headers.get('content-type', ''), b'')
        except httpx.HTTPError as e:
            return self._block(url, f'The request failed ({type(e).__name__}).')

    def _send(self, method: str, url: str, headers: dict, body: bytes | None) -> Fulfill:
        egress.check_url(url)
        with httpx.Client(transport=self.transport, trust_env=False, follow_redirects=False,
                          timeout=egress.TIMEOUT_SECONDS) as client, \
                client.stream(method, url, headers=headers, content=body) as response:
            peer = egress._peer_address(response)
            if peer and not egress.is_public(peer):
                raise egress.EgressBlocked('The site resolved to a private or internal address.')
            content = b''
            for chunk in response.iter_bytes(chunk_size=65536):
                content += chunk
                if len(content) > egress.MAX_BYTES:
                    content = content[:egress.MAX_BYTES]
                    break
            location = response.headers.get('location')
            return Fulfill(response.status_code, response.headers.get('content-type', ''), content,
                           {'location': urljoin(url, location)} if location else {})

    def _block(self, url: str, reason: str) -> Blocked:
        logger.info(f'Browser request blocked: {url} ({reason})')
        self.blocked.append({'url': url[:200], 'why': reason})
        return Blocked(reason)


# --- the driver: what the tools need from a browser ---------------------------------------------------------

class BrowserBlocked(Exception):
    """The page itself couldn't be opened: the message is safe to show the model."""


@dataclass(frozen=True)
class Element:
    ref: int
    tag: str  # a, button, input...
    label: str
    type: str = ''
    href: str = ''  # where a link goes
    form_action: str = ''  # where its form posts, for elements inside a form


@dataclass(frozen=True)
class Snapshot:
    url: str
    title: str
    text: str
    elements: tuple[Element, ...] = ()


class BrowserDriver(Protocol):
    async def start(self, router: EgressRouter) -> None: ...
    async def open(self, url: str) -> None: ...  # raises BrowserBlocked
    async def snapshot(self) -> Snapshot: ...
    async def screenshot(self) -> bytes: ...
    async def click(self, ref: int) -> None: ...
    async def type(self, ref: int, text: str) -> None: ...
    async def submit(self, ref: int) -> None: ...
    async def close(self) -> None: ...


# Chromium flags: no DNS of its own (every request is fetched by the router), no WebRTC to reach the LAN by IP
CHROMIUM_ARGS = (
    '--host-resolver-rules=MAP * ~NOTFOUND',
    '--force-webrtc-ip-handling-policy=disable_non_proxied_udp',
    '--webrtc-ip-handling-policy=disable_non_proxied_udp',
    '--disable-background-networking',
    '--no-first-run',
)

ELEMENTS_JS = """(limit) => {
  const out = []; let n = 0;
  for (const el of document.querySelectorAll(
      'a[href], button, input, select, textarea, [role=button], [role=link], [onclick]')) {
    const box = el.getBoundingClientRect();
    if (!(box.width || box.height) || getComputedStyle(el).visibility === 'hidden') continue;
    n += 1; el.setAttribute('data-la-ref', String(n));
    const form = el.form || el.closest('form');
    out.push({ref: n, tag: el.tagName.toLowerCase(), type: el.type || '', href: el.href || '',
              form_action: form ? (form.action || location.href) : '',
              label: (el.innerText || el.value || el.placeholder || el.getAttribute('aria-label') || el.name || '')
                     .trim().slice(0, 80)});
    if (n >= limit) break;
  }
  return out;
}"""


class PlaywrightDriver:
    """Headless Chromium in a fresh context. Every request goes through the router; the browser resolves nothing."""

    def __init__(self, timeout_seconds: float = 20):
        self.timeout_ms = int(timeout_seconds * 1000)
        self._playwright = self._browser = self._context = self.page = None

    async def start(self, router: EgressRouter) -> None:
        from playwright.async_api import async_playwright  # the optional extra: only when a browser starts

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=True, args=list(CHROMIUM_ARGS))
        self._context = await self._browser.new_context(service_workers='block', accept_downloads=False)
        self._context.set_default_timeout(self.timeout_ms)

        async def handle(route, request):
            decision = await router.decide(request.method, request.url, await request.all_headers(),
                                           request.post_data_buffer, request.resource_type)
            if isinstance(decision, Blocked):
                await route.abort('blockedbyclient')
            elif isinstance(decision, Fulfill):
                await route.fulfill(status=decision.status, body=decision.body,
                                    headers={**decision.headers, 'content-type': decision.content_type or
                                             'application/octet-stream'})
            else:
                await route.continue_()

        async def no_sockets(socket):
            await socket.close()

        await self._context.route('**/*', handle)
        await self._context.route_web_socket('**/*', no_sockets)
        self.page = await self._context.new_page()

    async def open(self, url: str) -> None:
        try:
            await self.page.goto(url, wait_until='load')
        except Exception as e:
            raise BrowserBlocked(f'The page could not be opened ({type(e).__name__}).') from None

    async def snapshot(self) -> Snapshot:
        elements = await self.page.evaluate(ELEMENTS_JS, MAX_ELEMENTS)
        text = await self.page.inner_text('body') if await self.page.query_selector('body') else ''
        return Snapshot(self.page.url, await self.page.title(), text,
                        tuple(Element(**element) for element in elements))

    async def screenshot(self) -> bytes:
        return await self.page.screenshot(full_page=False)

    def _element(self, ref: int):
        return self.page.locator(f'[data-la-ref="{int(ref)}"]')

    async def click(self, ref: int) -> None:
        await self._element(ref).click()
        await self.page.wait_for_load_state()

    async def type(self, ref: int, text: str) -> None:
        await self._element(ref).fill(text)

    async def submit(self, ref: int) -> None:
        await self._element(ref).evaluate(
            'el => { const f = el.form || el.closest("form"); if (f) f.requestSubmit(); else el.click(); }')
        await self.page.wait_for_load_state()

    async def close(self) -> None:
        for closing in (self._context, self._browser):
            if closing is not None:
                with contextlib.suppress(Exception):
                    await closing.close()
        if self._playwright is not None:
            with contextlib.suppress(Exception):
                await self._playwright.stop()
        self._playwright = self._browser = self._context = self.page = None


class JsonSiteDriver:
    """
    A browser without Chromium, for tests and evals: each page is a JSON document fetched through the router like any
    page, {"title", "text", "elements": [{"ref", "tag", "label", "type", "href", "form_action"}], "requests":
    [{"method", "url"}]}. "requests" stand for what the page's scripts fetch when it loads: each one goes through the
    router too. Serve the documents with an httpx.MockTransport as rt.web_transport, and set
    rt.browser_driver = JsonSiteDriver. Nothing else changes: same tools, same router, same egress checks.
    """

    def __init__(self):
        self.router: EgressRouter | None = None
        self.page = Snapshot('about:blank', '', '')
        self.typed: dict[int, str] = {}
        self.script_requests: list[tuple[str, str, bool]] = []  # (method, url, allowed), for tests

    async def start(self, router: EgressRouter) -> None:
        self.router = router

    async def _load(self, method: str, url: str, body: bytes | None = None) -> None:
        decision = await self.router.decide(method, url, {'accept': 'text/html'}, body, 'document')
        if isinstance(decision, Blocked):
            raise BrowserBlocked(decision.reason)
        if isinstance(decision, Fulfill) and 300 <= decision.status < 400 and decision.headers.get('location'):
            return await self._load('GET', decision.headers['location'])
        body = decision.body if isinstance(decision, Fulfill) else b''
        try:
            data = json.loads(body or b'{}')
        except ValueError:
            data = {'text': body.decode(errors='replace')}  # not a JSON page: its text as is
        self.page = Snapshot(url, str(data.get('title', '')), str(data.get('text', '')),
                             tuple(Element(**element) for element in data.get('elements', [])))
        self.typed = {}
        for request in data.get('requests', []):
            target = urljoin(url, request['url'])
            outcome = await self.router.decide(request.get('method', 'GET'), target, {}, None, 'fetch')
            self.script_requests.append((request.get('method', 'GET'), target, not isinstance(outcome, Blocked)))

    async def open(self, url: str) -> None:
        await self._load('GET', url)

    async def snapshot(self) -> Snapshot:
        return self.page

    async def screenshot(self) -> bytes:
        return b'\x89PNG\r\n\x1a\n'

    def _element(self, ref: int) -> Element:
        return next(e for e in self.page.elements if e.ref == int(ref))

    async def click(self, ref: int) -> None:
        element = self._element(ref)
        if element.href:
            await self._load('GET', urljoin(self.page.url, element.href))
        elif element.form_action:
            await self.submit(ref)

    async def type(self, ref: int, text: str) -> None:
        self.typed[int(ref)] = text

    async def submit(self, ref: int) -> None:
        element = self._element(ref)
        body = json.dumps({str(key): value for key, value in self.typed.items()}).encode()
        await self._load('POST', urljoin(self.page.url, element.form_action or self.page.url), body)

    async def close(self) -> None:
        self.router = None


def playwright_installed() -> bool:
    return importlib.util.find_spec('playwright') is not None


# --- one run's browser -----------------------------------------------------------------------------------------

class BrowserSession:
    """One run's browser: started on first use, closed after idle_seconds without an action (and at exit)."""

    def __init__(self, make_driver, router: EgressRouter, idle_seconds: float = 60):
        self.make_driver, self.router, self.idle_seconds = make_driver, router, idle_seconds
        self.driver: BrowserDriver | None = None
        self._idle: asyncio.TimerHandle | None = None
        self._lock = asyncio.Lock()

    async def get(self) -> BrowserDriver:
        async with self._lock:
            if self.driver is None:
                driver = self.make_driver()
                await driver.start(self.router)
                self.driver = driver
            self._touch()
            return self.driver

    def _touch(self) -> None:
        if self._idle is not None:
            self._idle.cancel()
        loop = asyncio.get_running_loop()
        self._idle = loop.call_later(self.idle_seconds, lambda: loop.create_task(self.close()))

    async def close(self) -> None:
        if self._idle is not None:
            self._idle.cancel()
            self._idle = None
        driver, self.driver = self.driver, None
        if driver is not None:
            await driver.close()


def site_of(value: str) -> str:
    """The host a to_site argument names: 'shop.example', or a URL whose host it is. Lowercase."""
    value = (value or '').strip().lower()
    host = urlsplit(value if '//' in value else f'//{value}').hostname or ''
    return host.rstrip('.')


def target_of(snapshot: Snapshot, element: Element, action: str) -> str:
    """The URL an action goes to: where a link points or a form posts, else the page itself (its scripts)."""
    if action == CLICK and element.href and not element.href.lower().startswith('javascript:'):
        return element.href
    if action == SUBMIT or (action == CLICK and element.form_action and element.type in ('submit', '')
                            and element.tag in ('button', 'input')):
        return element.form_action or snapshot.url
    return snapshot.url


def browser_tools(session: BrowserSession, shots_dir: Path, run_id: str) -> list:
    shots = {'n': 0}

    def result(snapshot: Snapshot, **extra) -> dict:
        text = snapshot.text if len(snapshot.text) <= TEXT_CHARS else snapshot.text[:TEXT_CHARS] + ' [...]'
        found = {'url': snapshot.url, 'title': snapshot.title, 'text': text,
                 'elements': [f'[{e.ref}] {e.tag}{f" ({e.type})" if e.type else ""} {e.label!r}'
                              + (f' -> {e.href}' if e.href else '') for e in snapshot.elements],
                 **extra}
        if session.router.blocked:
            found['blocked_requests'] = session.router.blocked[-MAX_BLOCKED_SHOWN:]
        return found

    async def open_page(url: str) -> dict:
        url = (url or '').strip()
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        try:
            egress.check_url(url)
            driver = await session.get()
            await driver.open(url)
            return result(await driver.snapshot())
        except (egress.EgressBlocked, BrowserBlocked) as e:
            return tool_error(str(e), 'Tell the user the page could not be opened.')

    async def read_page() -> dict:
        if session.driver is None:
            return tool_error('No page is open.', f'Open one with {OPEN} first.')
        return result(await (await session.get()).snapshot())

    async def screenshot() -> dict:
        if session.driver is None:
            return tool_error('No page is open.', f'Open one with {OPEN} first.')
        driver = await session.get()
        shots['n'] += 1
        path = shots_dir / f'{run_id[:8]}-{shots["n"]}.png'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(await driver.screenshot())
        return result(await driver.snapshot(), screenshot=str(path),
                      note='The screenshot is saved for the user; you get the page text.')

    def act(action: str):
        async def run_action(ref=None, to_site: str = '', text: str = '') -> dict:
            if session.driver is None:
                return tool_error('No page is open.', f'Open one with {OPEN} first.')
            driver = await session.get()
            snapshot = await driver.snapshot()
            element = next((e for e in snapshot.elements if str(e.ref) == str(ref)), None)
            if element is None:
                return tool_error(f'There is no element [{ref}] on this page.', f'Call {READ} for the current list.')
            target = site_of(target_of(snapshot, element, action))
            if not to_site or site_of(to_site) != target:
                return tool_error(f'This action goes to {target}, not {to_site or "(no site given)"}.',
                                  f'Call it again with to_site "{target}" if the user wants that.')
            try:
                egress.check_url(target_of(snapshot, element, action))
            except egress.EgressBlocked as e:
                return tool_error(str(e), 'Tell the user this action can\'t be done.')
            if action == CLICK:
                await driver.click(element.ref)
            elif action == TYPE:
                await driver.type(element.ref, str(text))
            else:
                await driver.submit(element.ref)
            return result(await driver.snapshot(), done=f'{action.removeprefix("browser_")} [{element.ref}]')
        return run_action

    ref = {'type': 'integer', 'description': f'The element number from {READ} or {OPEN}, e.g. 3'}
    site = {'type': 'string', 'description': 'The site this action goes to (the link\'s or form\'s host, else the '
                                             'page\'s), e.g. "shop.example"'}
    return [
        function_tool(OPEN, 'Open a web page in a browser (runs its scripts), and get its text and the numbered '
                            'elements you can act on. For plain reading, read_website is faster.',
                      {'properties': {'url': {'type': 'string', 'description': 'The page, e.g. https://example.com'}},
                       'required': ['url']}, open_page),
        function_tool(READ, 'The open page\'s text and numbered elements again (after it changed).',
                      {'properties': {}}, read_page),
        function_tool(SCREENSHOT, 'Save a screenshot of the open page for the user, and get its text.',
                      {'properties': {}}, screenshot),
        function_tool(CLICK, 'Click an element on the open page. The user approves it.',
                      {'properties': {'ref': ref, 'to_site': site}, 'required': ['ref', 'to_site']}, act(CLICK)),
        function_tool(TYPE, 'Type text into a field on the open page. The user approves it.',
                      {'properties': {'ref': ref, 'text': {'type': 'string', 'description': 'What to type'},
                                      'to_site': site}, 'required': ['ref', 'text', 'to_site']}, act(TYPE)),
        function_tool(SUBMIT, 'Submit the form an element belongs to. The user approves it.',
                      {'properties': {'ref': ref, 'to_site': site}, 'required': ['ref', 'to_site']}, act(SUBMIT)),
    ]


def run_tools(rt, log, owner: bool, stack=None) -> list:
    """This run's browser tools: for the owner, when [browser] is on and Playwright is installed (or a test driver
    is set on the Runtime as rt.browser_driver). The browser closes when the run ends (stack), or when idle."""
    settings = rt.config.section('browser')
    make_driver = getattr(rt, 'browser_driver', None)
    if not owner or not settings['enabled'] or (make_driver is None and not playwright_installed()):
        return []
    if make_driver is None:
        def make_driver():
            return PlaywrightDriver(settings['timeout_seconds'])
    session = BrowserSession(make_driver, EgressRouter(rt.web_transport), settings['idle_seconds'])
    if stack is not None:
        stack.push_async_callback(session.close)
    return browser_tools(session, rt.home.files_dir / 'browser', log.run_id)
