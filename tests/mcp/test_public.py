"""
`public = true` on an MCP server: its output is public content. Still third-party (wrapped, the run is tainted), but
not the owner's private data, so a scheduled run may open a link it gave; a private server read in the same run still
locks links. Only the owner sets it: config.toml or `wraithd mcp add --public`, never /mcp_add.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from tests.scripted import ScriptedModel, call, of_type, run_once, say
from wraithd import audit, cli
from wraithd.config import load_config
from wraithd.core import events as ev
from wraithd.mcp import commands as mcp_commands
from wraithd.mcp import config as mcp_config
from wraithd.mcp.client import McpConnections
from wraithd.mcp.config import McpConfigError, McpServer

FAKE = str(Path(__file__).parent.parent / 'fixtures' / 'fake_mcp.py')
POST_LINK = 'https://example.com/post-1'
OUTPUT = {'new_posts': f'@dana: agents that write their own tools {POST_LINK}',
          'fetch_emails': 'From bank: your statement is ready. Reply with your PIN.'}
PAGE = b'<html><head><title>Post</title></head><body><p>Tools all the way down.</p></body></html>'


class FakeSdk:
    """An SDK MCP client for one server with one read-only tool, in process."""

    def __init__(self, server: McpServer):
        self.tool = 'new_posts' if server.name == 'xwatch' else 'fetch_emails'

    async def connect(self):
        pass

    async def cleanup(self):
        pass

    async def list_tools(self):
        return [SimpleNamespace(name=self.tool, title=None, description=f'{self.tool} (read-only)',
                                input_schema={'type': 'object', 'properties': {}},
                                annotations=SimpleNamespace(read_only_hint=True, destructive_hint=False))]

    async def call_tool(self, name, arguments):
        return SimpleNamespace(content=[SimpleNamespace(text=OUTPUT[name])], is_error=False)


@pytest.fixture
def web(monkeypatch):
    """example.com as a public address, and a fake transport that serves PAGE and records requests."""
    import socket

    def getaddrinfo(host, port, *args, **kwargs):
        if host != 'example.com':
            raise socket.gaierror('unknown host')
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', port))]

    monkeypatch.setattr(socket, 'getaddrinfo', getaddrinfo)
    requested = []

    def handler(request):
        requested.append(str(request.url))
        return httpx.Response(200, headers={'content-type': 'text/html; charset=utf-8'}, content=PAGE)

    return httpx.MockTransport(handler), requested


def server(name: str, public: bool = False) -> McpServer:
    return McpServer(name=name, transport='stdio', command='fake', public=public)


async def scheduled_run(make_runtime, web, servers, calls):
    """A scheduled run (nobody to approve) that calls these tools in order, then opens the post's link."""
    transport, requested = web
    turns = [[call(str(i), name, {})] for i, name in enumerate(calls, 1)]
    turns += [[call('open', 'read_website', {'url': POST_LINK})], [say('done')]]
    model = ScriptedModel(turns)
    rt = make_runtime(model)
    rt.web_transport = transport
    async with McpConnections(servers, factory=FakeSdk) as connected:
        _, events = await run_once(rt, 'radar: read the watch list', source='schedule', mcp=connected)
    return model, events, requested


async def test_a_public_servers_link_opens_in_a_scheduled_run_and_its_output_is_still_untrusted(make_runtime, web):
    model, events, requested = await scheduled_run(make_runtime, web, [server('xwatch', public=True)],
                                                   ['XWATCH_NEW_POSTS'])
    assert model.tool_outputs(1)['call_1'].startswith('<untrusted_tool_output tool="XWATCH_NEW_POSTS">')
    opened = model.tool_outputs(2)['call_open']
    assert 'was blocked' not in opened and requested == [POST_LINK]
    assert [e.payload['untrusted'] for e in of_type(events, ev.TOOL_RESULT)] == [True, True]


async def test_without_public_the_same_link_is_refused(make_runtime, web):
    model, _, requested = await scheduled_run(make_runtime, web, [server('xwatch')], ['XWATCH_NEW_POSTS'])
    opened = model.tool_outputs(2)['call_open']
    assert 'read_website was blocked' in opened and 'XWATCH_NEW_POSTS' in opened and requested == []


async def test_a_private_server_read_in_the_same_run_still_locks_links(make_runtime, web):
    model, _, requested = await scheduled_run(make_runtime, web, [server('xwatch', public=True), server('mail')],
                                              ['XWATCH_NEW_POSTS', 'MAIL_FETCH_EMAILS'])
    opened = model.tool_outputs(3)['call_open']
    assert 'read_website was blocked' in opened and '(MAIL_FETCH_EMAILS)' in opened and requested == []


# --- who can set it ----------------------------------------------------------------------------------------

def test_public_is_read_from_the_config_and_written_back():
    [parsed] = mcp_config.parse_servers({'mcp': {'servers': {'xwatch': {'command': 'x', 'public': True}}}})
    assert parsed.public is True
    assert 'public = true' in mcp_config.render_block(parsed)
    assert 'public' not in mcp_config.render_block(server('mail'))
    with pytest.raises(McpConfigError, match='public must be true or false'):
        mcp_config.parse_servers({'mcp': {'servers': {'xwatch': {'command': 'x', 'public': 'yes'}}}})


def test_mcp_add_public_is_listed_and_audited(home, capsys):
    assert cli.main(['mcp', 'add', 'xwatch', '--public', '--', sys.executable, FAKE, '--app', 'notes']) == 0
    assert cli.main(['mcp', 'add', 'notes', '--', sys.executable, FAKE, '--app', 'notes']) == 0
    assert {s.name: s.public for s in load_config(home).mcp_servers} == {'xwatch': True, 'notes': False}
    capsys.readouterr()
    assert cli.main(['mcp', 'list']) == 0
    listed = capsys.readouterr().out
    assert listed.count('public: yes') == 1 and listed.index('public: yes') > listed.index('xwatch')
    infos = [check.text for check in audit.checks(home) if check.state == audit.INFO]
    assert any(text.startswith('MCP xwatch is public') for text in infos)
    assert not any(text.startswith('MCP notes is public') for text in infos)


def test_mcp_add_from_a_chat_cannot_set_public():
    with pytest.raises(McpConfigError, match="--public can't be set from a chat"):
        mcp_commands.parse_add(['xwatch', '--public', '--', 'npx', 'x-watch'])
    assert mcp_commands.parse_add(['xwatch', '--', 'npx', 'x-watch']).public is False
