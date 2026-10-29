"""
Attacks on remote access ([remote] allowed_hosts behind tailscale serve): turning it on lets the owner's tailnet name
in, and nothing more. A rebinding name, a page on another tailnet machine, or a device on the tailnet without the
token all still fail.
"""

import httpx
import pytest

from local_agent.daemon.app import Daemon
from tests.scripted import ScriptedModel

TAILNET = 'box.tail1234.ts.net'


@pytest.fixture
def daemon(make_runtime, home):
    home.config_path.write_text(f'[remote]\nallowed_hosts = ["{TAILNET}"]\n')
    return Daemon(make_runtime(ScriptedModel([])))


def client(daemon, token=None):
    headers = {'Authorization': f'Bearer {token}'} if token else {}
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=daemon.asgi()), base_url=f'https://{TAILNET}',
                             headers=headers)


async def test_dns_rebinding_still_fails_with_remote_access_on(daemon):
    # evil.example now resolves to 127.0.0.1 (or the tailnet address): the browser sends its own Host
    for host in ('evil.example', 'evil.example:443', f'{TAILNET}.evil.example', 'ts.net'):
        response = await client(daemon, daemon.token).get('/api/status', headers={'Host': host})
        assert response.json()['error']['code'] == 'bad_host', host


async def test_a_page_on_another_tailnet_machine_cant_approve(daemon):
    # the owner's browser opens a page on another tailnet machine; it posts to the agent with the browser
    response = await client(daemon, daemon.token).post(
        '/api/approvals/x', json={'decision': 'approve'},
        headers={'Host': TAILNET, 'Origin': 'https://nas.tail1234.ts.net'})
    assert response.status_code == 403 and response.json()['error']['code'] == 'bad_origin'


async def test_a_tailnet_device_without_the_token_gets_nothing(daemon):
    bare = client(daemon)
    for path in ('/api/status', '/api/conversations', '/api/approvals', '/api/events'):
        response = await bare.get(path, headers={'Host': TAILNET})
        assert response.status_code == 401, path
