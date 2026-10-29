"""
`local-agent mcp add --sandbox`: a stdio server that opts in is stored with sandbox = true and launches through
docker, with nothing mounted, the [sandbox] network, and its secrets passed by name only.
"""

import pytest

from local_agent import cli
from local_agent.config import load_config
from local_agent.core import events as ev
from local_agent.mcp import client as mcp_client
from local_agent.mcp import config as mcp_config
from local_agent.mcp.client import McpConnections, sdk_server
from local_agent.mcp.config import McpConfigError, McpServer
from local_agent.sandbox.docker import HARDENING
from tests.sandbox.fakes import flag_values


@pytest.fixture
def keychain(monkeypatch):
    stored = {}
    monkeypatch.setattr(mcp_config.keyring, 'set_password', lambda app, key, value: stored.__setitem__(key, value))
    monkeypatch.setattr(mcp_config.keyring, 'get_password', lambda app, key: stored.get(key))
    return stored


def test_add_sandbox_is_stored_in_the_servers_block(home, keychain):
    assert cli.main(['mcp', 'add', 'notes', '--sandbox', '--env', 'NOTES_TOKEN=t0ken-123456', '--',
                     'npx', '-y', 'notes-mcp']) == 0
    assert 'sandbox = true' in home.config_path.read_text()
    [server] = load_config(home).mcp_servers
    assert server.sandbox and server.command == 'npx'
    # without --sandbox, nothing changes
    assert cli.main(['mcp', 'add', 'plain', '--', 'plain-mcp']) == 0
    assert not load_config(home).mcp_servers[1].sandbox


def test_sandbox_is_refused_for_a_remote_server(home, capsys):
    assert cli.main(['mcp', 'add', 'remote', '--sandbox', '--url', 'https://example.com/mcp']) == 1
    assert '--sandbox is for local servers' in capsys.readouterr().err
    home.config_path.write_text('[mcp.servers.remote]\ntransport = "http"\nurl = "https://x.example/mcp"\n'
                                'sandbox = true\n')
    with pytest.raises(Exception, match='only a local'):
        load_config(home)


def test_a_sandboxed_server_launches_in_docker(home, keychain):
    home.config_path.write_text('[sandbox]\nimage = "node:22-slim"\n')
    mcp_config.set_secret('notes', 'env', 'NOTES_TOKEN', 't0ken-123456')
    server = McpServer(name='notes', transport='stdio', command='npx', args=('-y', 'notes-mcp'),
                       env=('NOTES_TOKEN',), sandbox=True)
    params = sdk_server(server).params
    argv = [params.command, *params.args]
    assert argv[:5] == ['docker', 'run', '--rm', '-i', '--init']
    assert flag_values(argv, '--network') == ['none'] and '--mount' not in argv and '-v' not in argv
    assert flag_values(argv, '-e') == ['NOTES_TOKEN'] and not any('t0ken' in item for item in argv)
    assert params.env == {'NOTES_TOKEN': 't0ken-123456'}  # in the docker client's environment only
    assert argv[-4:] == ['node:22-slim', 'npx', '-y', 'notes-mcp']
    assert all(flag in argv for flag in HARDENING)


def test_an_unsandboxed_server_is_unchanged(home):
    params = sdk_server(McpServer(name='plain', transport='stdio', command='plain-mcp')).params
    assert params.command == 'plain-mcp'


def test_a_broken_sandbox_setting_keeps_the_server_from_starting(home):
    home.config_path.write_text('[sandbox]\nnetwork = "host"\n')
    with pytest.raises(McpConfigError, match="notes is sandboxed, but .*network 'host'"):
        sdk_server(McpServer(name='notes', transport='stdio', command='npx', sandbox=True))


class FailingServer:
    async def connect(self):
        raise RuntimeError('npx: not found')


class StartedServer:
    session = None

    async def connect(self):
        return None

    async def list_tools(self):
        return []

    async def cleanup(self):
        return None


async def test_a_sandboxed_server_that_fails_to_start_points_to_the_image(home):
    server = McpServer(name='notes', transport='stdio', command='npx', sandbox=True)
    async with McpConnections([server], factory=lambda s: FailingServer()) as connected:
        assert connected.errors['notes'] == 'RuntimeError' + mcp_client.SANDBOX_HINT
    assert '[sandbox] image' in mcp_client.SANDBOX_HINT


async def test_a_sandboxed_start_is_an_event(home, store):
    server = McpServer(name='notes', transport='stdio', command='npx', sandbox=True)
    async with McpConnections([server], factory=lambda s: StartedServer(), store=store):
        pass
    [event] = store.events_between_runs(types=[ev.SANDBOX_MCP_START])
    assert event.payload == {'server': 'notes', 'command': 'npx'}
