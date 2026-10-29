"""`local-agent mcp tools / allow / disallow / remove` with pinning: what the owner sees and what allow refuses."""

import pytest

from local_agent import cli
from local_agent.config import load_config
from local_agent.mcp import commands, pinning
from local_agent.mcp import config as mcp_config
from local_agent.mcp.client import McpConnections
from local_agent.mcp.config import McpServer
from local_agent.store import SQLiteStore
from tests.security.test_mcp_pinning import FakeServer, baseline_tools, tool


@pytest.fixture
def served(home, monkeypatch):
    """A configured gmail server whose tools the test can change between commands."""
    mcp_config.write_server(home.config_path, McpServer(name='gmail', transport='stdio', command='x',
                                                        always_allow=('send_email',)), 'gmail')
    current = {'tools': baseline_tools(), 'up': True}

    def factory(server):
        if not current['up']:
            raise ConnectionError('down')
        return FakeServer(current['tools'])

    monkeypatch.setattr(commands, 'McpConnections', lambda servers, store=None: McpConnections(
        servers, factory=factory, store=store))
    return current


def output(capsys):
    captured = capsys.readouterr()
    return captured.out + captured.err


def test_tools_shows_re_allow_changed_and_new(home, served, capsys):
    assert cli.main(['mcp', 'tools', 'gmail']) == 0
    lines = {line.split()[0]: line for line in output(capsys).splitlines()}
    assert 're-allow needed' in lines['send_email'] and 'destructive' in lines['delete_email']

    assert cli.main(['mcp', 'allow', 'gmail', 'send_email']) == 0
    served['tools'] = [tool('send_email', 'Changed.'), *baseline_tools()[1:], tool('export_inbox', readOnlyHint=True)]
    assert cli.main(['mcp', 'tools', 'gmail']) == 0
    lines = {line.split()[0]: line for line in output(capsys).splitlines()}
    assert 'changed: asks until you review it (review #' in lines['send_email']
    assert 'new: asks until you review it (review #' in lines['export_inbox']


def test_allow_pins_and_refuses_what_it_cannot_check(home, served, capsys):
    assert cli.main(['mcp', 'allow', 'gmail', 'add_label']) == 0
    store = SQLiteStore(home.db_path)
    try:
        assert pinning.Pins(store).row('gmail', 'add_label')['allowed_hash']
    finally:
        store.close()
    assert 'add_label' in load_config(home).mcp_servers[0].always_allow

    served['tools'] = [*baseline_tools(), tool('export_inbox')]
    assert cli.main(['mcp', 'allow', 'gmail', 'export_inbox']) == 1
    assert 'Review it first' in output(capsys)
    assert cli.main(['mcp', 'allow', 'gmail', 'delete_email']) == 1 and 'destructive' in output(capsys)

    served['up'] = False
    assert cli.main(['mcp', 'allow', 'gmail', 'fetch_emails']) == 1 and 'did not connect' in output(capsys)
    assert 'fetch_emails' not in load_config(home).mcp_servers[0].always_allow


def test_disallow_unpins_and_remove_forgets(home, served, capsys):
    assert cli.main(['mcp', 'allow', 'gmail', 'send_email']) == 0
    assert cli.main(['mcp', 'disallow', 'gmail', 'send_email']) == 0
    store = SQLiteStore(home.db_path)
    try:
        assert pinning.Pins(store).row('gmail', 'send_email')['allowed_hash'] is None
        assert cli.main(['mcp', 'remove', 'gmail']) == 0
        assert pinning.Pins(store).rows('gmail') == []
    finally:
        store.close()
