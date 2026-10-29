"""The API's always-allow pins the tool as the server showed it, like `mcp allow`, and refuses an unreviewed one."""

import sys

from local_agent import cli
from local_agent.mcp import pinning
from tests.daemon.test_api import FAKE
from tests.scripted import ScriptedModel


async def test_always_allow_pins_and_unpins(make_daemon, home, capsys):
    cli.main(['mcp', 'add', 'gmail', '--', sys.executable, FAKE, '--app', 'gmail'])
    daemon, client = await make_daemon(ScriptedModel([]))
    pins = pinning.Pins(daemon.rt.store)
    assert pins.row('gmail', 'send_email')['allowed_hash'] is None

    assert (await client.put('/api/mcp/servers/gmail/always-allow/send_email')).status_code == 200
    assert pins.row('gmail', 'send_email')['allowed_hash'] == pins.row('gmail', 'send_email')['reviewed_hash']

    assert (await client.delete('/api/mcp/servers/gmail/always-allow/send_email')).status_code == 200
    assert pins.row('gmail', 'send_email')['allowed_hash'] is None


async def test_an_unreviewed_tool_cannot_be_always_allowed(make_daemon, home, capsys):
    cli.main(['mcp', 'add', 'gmail', '--', sys.executable, FAKE, '--app', 'gmail'])
    daemon, client = await make_daemon(ScriptedModel([]))
    pinning.Pins(daemon.rt.store).set('gmail', 'send_email', reviewed_hash='changed-since')
    refused = await client.put('/api/mcp/servers/gmail/always-allow/send_email')
    assert refused.status_code == 409 and 'review list' in refused.json()['error']['message']
    assert 'GMAIL_SEND_EMAIL' not in daemon.mcp.always_allowed
    assert 'always_allow = []' in home.config_path.read_text()
