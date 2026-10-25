"""
The local API's guards: every website the owner visits can make their browser call 127.0.0.1, so a request needs
the token, the daemon's own Host (no DNS rebinding), and, from a browser, the daemon's own Origin. No CORS.
"""

import os
import stat

import httpx
import pytest

from local_agent.daemon.app import HOST, Daemon
from local_agent.daemon.security import load_or_create_token
from tests.daemon.conftest import BASE
from tests.scripted import ScriptedModel


@pytest.fixture
async def guarded(make_daemon):
    daemon, client = await make_daemon(ScriptedModel([]))
    bare = httpx.AsyncClient(transport=httpx.ASGITransport(app=daemon.asgi()), base_url=BASE)
    return daemon, client, bare


async def test_no_token_or_a_wrong_one_is_401(guarded):
    daemon, _, bare = guarded
    for headers in ({}, {'Authorization': 'Bearer wrong'}, {'Authorization': daemon.token},
                    {'Authorization': f'Basic {daemon.token}'}):
        response = await bare.get('/api/status', headers=headers)
        assert response.status_code == 401, headers
        assert response.json() == {'error': {'code': 'unauthorized',
                                             'message': 'Open the link printed by `local-agent daemon`.'}}
    # the token in a query string doesn't count either
    assert (await bare.get(f'/api/status?token={daemon.token}')).status_code == 401


async def test_the_right_token_works_and_health_needs_none(guarded):
    _, client, bare = guarded
    assert (await client.get('/api/status')).status_code == 200
    assert (await bare.get('/api/health')).json()['ok'] is True


@pytest.mark.parametrize('origin', ['https://evil.example', 'http://127.0.0.1:8000', 'http://localhost',
                                    'null', 'http://127.0.0.1.evil.example:7878'])
async def test_a_foreign_origin_is_403_even_with_the_token(guarded, origin):
    _, client, _ = guarded
    response = await client.post('/api/chat', json={'message': 'hi'}, headers={'Origin': origin})
    assert response.status_code == 403 and response.json()['error']['code'] == 'bad_origin'
    assert (await client.get('/api/health', headers={'Origin': origin})).status_code == 403


async def test_the_daemons_own_origin_passes(guarded):
    _, client, _ = guarded
    for origin in (BASE, 'http://localhost:7878'):
        assert (await client.get('/api/status', headers={'Origin': origin})).status_code == 200


@pytest.mark.parametrize('host', ['evil.example', 'rebind.evil.example:7878', '127.0.0.1:9999', '192.168.1.5:7878',
                                  'localhost.evil.example:7878'])
async def test_a_foreign_host_is_403_dns_rebinding(guarded, host):
    _, client, _ = guarded
    response = await client.get('/api/status', headers={'Host': host})
    assert response.status_code == 403 and response.json()['error']['code'] == 'bad_host'
    assert (await client.get('/', headers={'Host': host})).status_code == 403  # the UI too


async def test_no_cors_and_no_preflight(guarded):
    _, client, _ = guarded
    response = await client.get('/api/status', headers={'Origin': BASE})
    assert not any(name.lower().startswith('access-control') for name in response.headers)
    preflight = await client.options('/api/chat', headers={'Origin': BASE, 'Access-Control-Request-Method': 'POST'})
    assert preflight.status_code == 403 and 'access-control-allow-origin' not in preflight.headers


async def test_the_server_binds_to_127_0_0_1_only(guarded):
    daemon, _, _ = guarded
    config = daemon.uvicorn_config()
    assert HOST == '127.0.0.1' and config.host == '127.0.0.1' and config.port == 7878


def test_the_token_file_is_private_and_stable(home):
    first = load_or_create_token(home.root)
    assert len(first) >= 40 and stat.S_IMODE(os.stat(home.root / 'api-token').st_mode) == 0o600
    assert load_or_create_token(home.root) == first


async def test_a_dev_origin_must_be_set_on_purpose(make_runtime, home):
    home.config_path.write_text('[daemon]\ndev_origin = "http://localhost:5173"\n')
    rt = make_runtime(ScriptedModel([]))
    daemon = Daemon(rt)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=daemon.asgi()), base_url=BASE,
                               headers={'Authorization': f'Bearer {daemon.token}'})
    assert (await client.get('/api/status', headers={'Origin': 'http://localhost:5173'})).status_code == 200
    assert (await client.get('/api/status', headers={'Origin': 'http://localhost:5174'})).status_code == 403
