import httpx
import pytest

from local_agent.daemon.app import Daemon

BASE = 'http://127.0.0.1:7878'


@pytest.fixture
async def make_daemon(make_runtime):
    """A started daemon on a scripted runtime, and an API client for it (ASGI in-process, no socket)."""
    started = []

    async def make(model, strong=None, telegram_transport=None, clock=None):
        rt = make_runtime(model, strong)
        daemon = Daemon(rt, telegram_transport=telegram_transport, **({'clock': clock} if clock else {}))
        await daemon.start()
        started.append(daemon)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=daemon.asgi()), base_url=BASE,
                                   headers={'Authorization': f'Bearer {daemon.token}'})
        return daemon, client

    yield make
    for daemon in started:
        await daemon.stop()
