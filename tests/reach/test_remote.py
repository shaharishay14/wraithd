"""
Remote access: [remote] allowed_hosts adds the machine's tailnet name (what `tailscale serve` passes on) to the
Host and Origin checks. The token is still required, the server still binds to 127.0.0.1, and nothing else gets in.
"""

import httpx
import pytest

from local_agent.config import ConfigError, load_config
from local_agent.daemon.app import HOST, Daemon
from local_agent.daemon.security import allowed_hosts, allowed_origins, remote_hosts
from tests.scripted import ScriptedModel

TAILNET = 'box.tail1234.ts.net'


@pytest.fixture
def remote(make_runtime, home):
    """remote(hosts) -> (daemon, client with the token, client without), the hosts in [remote] allowed_hosts."""
    def make(hosts=(TAILNET,)):
        home.config_path.write_text('[remote]\nallowed_hosts = [' + ', '.join(f'"{h}"' for h in hosts) + ']\n')
        daemon = Daemon(make_runtime(ScriptedModel([])))
        app = httpx.ASGITransport(app=daemon.asgi())
        base = f'https://{TAILNET}'
        return (daemon, httpx.AsyncClient(transport=app, base_url=base,
                                          headers={'Authorization': f'Bearer {daemon.token}'}),
                httpx.AsyncClient(transport=app, base_url=base))
    return make


async def test_a_configured_tailnet_host_is_accepted(remote):
    _, client, _ = remote()
    for host in (TAILNET, f'{TAILNET}:443', TAILNET.upper()):
        response = await client.get('/api/status', headers={'Host': host, 'Origin': f'https://{TAILNET}'})
        assert response.status_code == 200, host
    assert (await client.get('/api/status', headers={'Host': TAILNET})).status_code == 200  # no Origin: curl


async def test_an_unconfigured_host_is_refused(remote):
    _, client, _ = remote()
    for host in ('other.tail1234.ts.net', f'sub.{TAILNET}', f'{TAILNET}.evil.example', f'{TAILNET}:8443',
                 'evil.example'):
        response = await client.get('/api/status', headers={'Host': host})
        assert response.status_code == 403 and response.json()['error']['code'] == 'bad_host', host


async def test_without_remote_config_the_tailnet_name_is_refused(make_runtime, home):
    daemon = Daemon(make_runtime(ScriptedModel([])))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=daemon.asgi()), base_url=f'https://{TAILNET}',
                               headers={'Authorization': f'Bearer {daemon.token}'})
    assert (await client.get('/api/status')).json()['error']['code'] == 'bad_host'
    assert daemon.remote_urls == []


async def test_the_token_is_still_required(remote):
    daemon, _, bare = remote()
    assert (await bare.get('/api/status')).status_code == 401
    assert (await bare.get('/api/status', headers={'Authorization': 'Bearer wrong'})).status_code == 401
    assert (await bare.get('/api/health')).status_code == 200  # as on 127.0.0.1


@pytest.mark.parametrize('origin', ['https://evil.example', f'http://{TAILNET}', f'https://sub.{TAILNET}',
                                    f'https://{TAILNET}.evil.example', f'https://{TAILNET}:8443', 'null',
                                    'http://127.0.0.1:7878.evil.example'])
async def test_a_cross_site_origin_is_still_refused(remote, origin):
    _, client, _ = remote()
    response = await client.post('/api/chat', json={'message': 'hi'}, headers={'Host': TAILNET, 'Origin': origin})
    assert response.status_code == 403 and response.json()['error']['code'] == 'bad_origin'


async def test_local_access_is_unchanged_and_the_server_stays_on_loopback(remote):
    daemon, client, _ = remote()
    response = await client.get('/api/status', headers={'Host': '127.0.0.1:7878', 'Origin': 'http://127.0.0.1:7878'})
    assert response.status_code == 200
    assert HOST == '127.0.0.1' and daemon.uvicorn_config().host == '127.0.0.1'
    assert daemon.remote_urls == [f'https://{TAILNET}/#token={daemon.token}']


def test_a_port_in_the_name_is_kept_exactly():
    assert allowed_hosts(7878, ['box.ts.net:8443']) == {'127.0.0.1:7878', 'localhost:7878', 'box.ts.net:8443'}
    assert 'https://box.ts.net:8443' in allowed_origins(7878, '', ['box.ts.net:8443'])
    assert 'https://box.ts.net' not in allowed_origins(7878, '', ['box.ts.net:8443'])


@pytest.mark.parametrize('entry', ['*', '*.ts.net', 'https://box.ts.net', 'box.ts.net/api', 'user@box.ts.net', '',
                                   'box', ' ', 'box..ts.net', 7878])
def test_allowed_hosts_must_be_spelled_out(home, entry):
    value = f'"{entry}"' if isinstance(entry, str) else entry
    home.config_path.write_text(f'[remote]\nallowed_hosts = [{value}]\n')
    with pytest.raises(ConfigError, match='allowed_hosts'):
        load_config(home)


def test_remote_hosts_reads_the_config(home):
    assert remote_hosts(load_config(home)) == []
    home.config_path.write_text('[remote]\nallowed_hosts = ["Box.Tail1234.TS.net"]\n')
    assert remote_hosts(load_config(home)) == ['box.tail1234.ts.net']
