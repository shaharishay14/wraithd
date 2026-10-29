"""The browser's parts: the egress router, where an action goes, the session's lifetime, and (opt-in) real Chromium."""

import asyncio
import os

import httpx
import pytest

from local_agent.tools import browser
from local_agent.tools.browser import Blocked, BrowserSession, EgressRouter, Element, Fulfill, PassThrough, Snapshot
from tests.capabilities.fakes import FakeSite, public_example_hosts


@pytest.fixture
def site(monkeypatch):
    public_example_hosts(monkeypatch)
    return FakeSite({'https://shop.example/': {'text': 'hi'}})


async def test_the_router_fetches_public_pages_and_blocks_the_rest(site):
    router = EgressRouter(site.transport)
    assert isinstance(await router.decide('GET', 'https://shop.example/'), Fulfill)
    assert isinstance(await router.decide('GET', 'data:text/plain,hi'), PassThrough)
    for url in ('http://127.0.0.1:7878/api/status', 'http://192.168.0.1/', 'file:///etc/passwd',
                'https://user:pw@shop.example/'):
        assert isinstance(await router.decide('GET', url), Blocked), url
    assert isinstance(await router.decide('GET', 'wss://shop.example/socket', resource_type='websocket'), Blocked)
    assert len(router.blocked) == 5 and [m for m, _, _ in site.requests] == ['GET']


async def test_a_post_is_not_redirected_for_the_browser(monkeypatch):
    public_example_hosts(monkeypatch)

    def handle(request):
        return httpx.Response(303, headers={'location': '/done'})

    decision = await EgressRouter(httpx.MockTransport(handle)).decide('POST', 'https://shop.example/pay', {}, b'x')
    # The browser asks for the new location itself, and that request is checked again
    assert decision.status == 303 and decision.headers == {'location': 'https://shop.example/done'}


async def test_no_cookies_or_credentials_leave_the_browser(monkeypatch):
    public_example_hosts(monkeypatch)
    seen = []

    def handle(request):
        seen.append({key.lower() for key in request.headers})
        return httpx.Response(200, text='ok')

    await EgressRouter(httpx.MockTransport(handle)).decide(
        'GET', 'https://shop.example/', {'Cookie': 'x', 'User-Agent': 'ua', 'Authorization': 'Bearer t'})
    assert 'user-agent' in seen[0] and not seen[0] & {'cookie', 'authorization'}


def test_where_an_action_goes():
    page = Snapshot('https://shop.example/cart', 'Cart', '')
    link = Element(1, 'a', 'Help', href='https://help.example/faq')
    pay = Element(2, 'button', 'Pay', type='submit', form_action='https://pay.example/charge')
    field = Element(3, 'input', 'Card', type='text', form_action='https://pay.example/charge')
    assert browser.site_of(browser.target_of(page, link, browser.CLICK)) == 'help.example'
    assert browser.site_of(browser.target_of(page, pay, browser.CLICK)) == 'pay.example'  # a click submits
    assert browser.site_of(browser.target_of(page, pay, browser.SUBMIT)) == 'pay.example'
    assert browser.site_of(browser.target_of(page, field, browser.TYPE)) == 'shop.example'  # the page sees it
    assert browser.site_of('https://Shop.Example/x') == browser.site_of('shop.example') == 'shop.example'


async def test_an_idle_browser_closes_itself():
    closed = []

    class Driver(browser.JsonSiteDriver):
        async def close(self):
            closed.append(True)

    session = BrowserSession(Driver, EgressRouter(), idle_seconds=0.01)
    await session.get()
    await asyncio.sleep(0.05)
    assert closed == [True] and session.driver is None


REAL_BROWSER = pytest.mark.skipif(not os.environ.get('LOCAL_AGENT_REAL_BROWSER'),
                                  reason='real Chromium: set LOCAL_AGENT_REAL_BROWSER=1')


@REAL_BROWSER
async def test_real_chromium_routes_every_request_through_the_guard(monkeypatch):  # pragma: no cover
    """Needs `uv sync --extra browser` and `playwright install chromium`. A data: page whose script fetches the
    daemon's port: the fetch is aborted by the router, and the page still loads."""
    pytest.importorskip('playwright')
    router = EgressRouter()
    driver = browser.PlaywrightDriver(timeout_seconds=10)
    await driver.start(router)
    try:
        await driver.open('data:text/html,<p>hi</p><script>fetch("http://127.0.0.1:7878/api/status")</script>')
        await asyncio.sleep(0.5)
        assert (await driver.snapshot()).text.strip() == 'hi'
        assert any('127.0.0.1' in blocked['url'] for blocked in router.blocked)
    finally:
        await driver.close()
