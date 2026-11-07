"""
/mcp_add from the owner's phone (channels/base.py, mcp/remote_add.py): a confirm card with the exact command, Allow
writes the block and pins the tools on the first connect, Deny and expiry write nothing. Strangers are ignored as
before, a forwarded /mcp_add is refused, and a secret in the command is refused and kept nowhere.
"""

import asyncio
import json
import shlex
import sys
from pathlib import Path

import httpx
import pytest

from tests.daemon.conftest import make_daemon  # noqa: F401 (a fixture)
from tests.daemon.test_telegram import BOT_TOKEN, OWNER, STRANGER, FakeTelegram, paired
from tests.reach.test_channel import MemoryChannel, ignored, pair
from tests.scripted import ScriptedModel, call, say
from wraithd import cli, config
from wraithd.channels import base
from wraithd.config import load_config
from wraithd.core import events as ev
from wraithd.core import gate as gate_module
from wraithd.mcp import pinning, remote_add

FAKE = str(Path(__file__).parent.parent / 'fixtures' / 'fake_mcp.py')
SECRET = 'hunter2-super-secret-value'


@pytest.fixture(autouse=True)
def quick_polls(monkeypatch):
    monkeypatch.setattr(gate_module, 'APPROVAL_POLL_SECONDS', 0.01)


def notes_command(tmp_path) -> str:
    command = [sys.executable, FAKE, '--app', 'notes', '--outbox', str(tmp_path / 'out.jsonl')]
    return f'/mcp_add notes --provides notes -- {shlex.join(command)}'


async def card(channel, before=0):
    for _ in range(500):
        prompts = [s for s in channel.sent[before:] if s[2]]
        if prompts:
            return prompts[-1]
        await asyncio.sleep(0.01)
    raise AssertionError('no card')


def added(rt):
    return [e.payload for e in rt.store.events_between_runs(50, (remote_add.MCP_ADDED,))]


def stored_everywhere(rt) -> str:
    """Everything the owner's machine kept: config, events, approvals, conversations."""
    parts = [rt.home.config_path.read_text() if rt.home.config_path.exists() else '']
    parts += [json.dumps(e.payload) for e in rt.store.events_between_runs(500)]
    parts += [json.dumps(a.arguments) for a in rt.store.list_approvals(status=None)]
    conversation = rt.store.find_conversation('memory', 'owner')
    parts += [m.content for m in rt.store.messages(conversation.id)] if conversation else []
    return '\n'.join(parts)


async def test_owner_allow_adds_the_server_and_pins_its_tools(make_runtime, tmp_path):
    channel = MemoryChannel(make_runtime(ScriptedModel([])))
    await pair(channel)
    command = notes_command(tmp_path)
    await channel.receive(base.Incoming(chat='owner', text=command))
    _, text, choices = await card(channel)
    assert text.startswith('Add MCP server "notes"?')
    assert f'runs: {shlex.join([sys.executable, FAKE, "--app", "notes"])}' in text  # the exact command line
    assert 'provides: notes' in text and choices[0].startswith('a:') and choices[1].startswith('d:')
    assert 'notes' not in [s.name for s in load_config(channel.rt.home).mcp_servers]  # nothing before Allow

    await channel.decide(base.Decision(chat='owner', data=choices[0]))
    await channel.settle()
    [server] = load_config(channel.rt.home).mcp_servers
    assert server.name == 'notes' and server.provides == ('notes',) and server.command == sys.executable
    assert channel.rt.config.mcp_servers == (server,)  # the runtime sees it too
    pins = pinning.Pins(channel.rt.store).rows('notes')
    assert len(pins) == 2 and all(row['reviewed_hash'] == row['seen_hash'] for row in pins)  # the baseline
    reply = channel.sent[-1][1]
    assert reply.startswith('Added notes. 2 tools, pinned') and '  list_notes (read-only)' in reply
    assert added(channel.rt) == [{'server': 'notes', 'transport': 'stdio', 'tools': 2, 'connected': True}]
    assert channel.closed[-1][1] == 'approved'


@pytest.mark.parametrize('how', ['deny', 'expire'])
async def test_deny_or_expiry_writes_nothing(make_runtime, tmp_path, monkeypatch, how):
    if how == 'expire':
        monkeypatch.setattr(gate_module, 'APPROVAL_TIMEOUT_SECONDS', 0.05)
    channel = MemoryChannel(make_runtime(ScriptedModel([])))
    await pair(channel)
    await channel.receive(base.Incoming(chat='owner', text=notes_command(tmp_path)))
    _, _, choices = await card(channel)
    if how == 'deny':
        await channel.decide(base.Decision(chat='owner', data=choices[1]))
    await channel.settle()
    assert load_config(channel.rt.home).mcp_servers == ()
    assert added(channel.rt) == [] and pinning.Pins(channel.rt.store).rows('notes') == []
    assert channel.sent[-1][1] == ('Not added: notes was denied.' if how == 'deny' else
                                   'Not added: notes was not approved in time.')
    assert channel.closed[-1][1] == ('denied' if how == 'deny' else 'expired')


async def test_a_stranger_is_ignored_and_cannot_tap_allow(make_runtime, tmp_path):
    model = ScriptedModel([])
    channel = MemoryChannel(make_runtime(model))
    await pair(channel)
    await channel.receive(base.Incoming(chat='eve', who={'username': 'eve'}, text=notes_command(tmp_path)))
    await channel.settle()
    assert [s for s in channel.sent if s[0] == 'eve'] == [] and model.inputs == []
    assert ignored(channel.rt.store) == [{'chat': 'eve', 'username': 'eve'}]
    assert channel.rt.store.list_approvals(status=None) == []

    await channel.receive(base.Incoming(chat='owner', text=notes_command(tmp_path)))
    _, _, choices = await card(channel)
    await channel.decide(base.Decision(chat='eve', data=choices[0], who={'username': 'eve'}))
    assert channel.acks[-1] == ('eve', 'Only the owner can decide.')
    await channel.decide(base.Decision(chat='owner', data=choices[1]))
    await channel.settle()
    assert load_config(channel.rt.home).mcp_servers == ()


async def test_a_forwarded_mcp_add_is_refused(make_runtime, tmp_path):
    channel = MemoryChannel(make_runtime(ScriptedModel([])))
    await pair(channel)
    await channel.receive(base.Incoming(chat='owner', text=notes_command(tmp_path), forwarded=True))
    await channel.settle()
    assert channel.sent[-1] == ('owner', 'I only add MCP servers from a /mcp_add you type yourself, not from a '
                                         'forwarded message.', [])
    assert channel.rt.store.list_approvals(status=None) == [] and load_config(channel.rt.home).mcp_servers == ()


@pytest.mark.parametrize('arguments', [
    f'--env NOTES_TOKEN={SECRET} -- npx -y notes-mcp',
    f'--url https://notes.example/mcp --header "Authorization: Bearer {SECRET}"',
    f'--url https://notes.example/mcp?api_key={SECRET}',
    f'--url https://me:{SECRET}@notes.example/mcp',
    f'-- npx -y notes-mcp --api-key {SECRET}',
    f'-- npx -y notes-mcp --token={SECRET}',
    '-- npx -y notes-mcp sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123',
    '-- npx -y notes-mcp ghp_abcdefghijklmnopqrstuvwxyz0123456789',
    '-- npx -y notes-mcp 9f8e7d6c5b4a39281706f5e4d3c2b1a0f9e8d7c6',
])
async def test_a_secret_looking_argument_is_refused_and_kept_nowhere(make_runtime, arguments):
    channel = MemoryChannel(make_runtime(ScriptedModel([])))
    deleted = []

    async def delete(chat, ref):
        deleted.append((chat, ref))

    channel.delete = delete
    await pair(channel)
    await channel.receive(base.Incoming(chat='owner', text=f'/mcp_add notes {arguments}', ref=41))
    await channel.settle()
    reply = channel.sent[-1][1]
    assert reply.startswith('Not added: that looks like it has a secret in it.')
    assert 'env file' in reply and 'op://' in reply
    assert deleted == [('owner', 41)]  # Telegram keeps messages: the bot deletes it
    assert load_config(channel.rt.home).mcp_servers == () and channel.rt.store.list_approvals(status=None) == []
    kept = stored_everywhere(channel.rt)
    for word in (SECRET, 'sk-ant-api03', 'ghp_abcdef', '9f8e7d6c5b4a'):
        assert word not in kept


async def test_names_and_references_are_fine_and_never_values(make_runtime):
    channel = MemoryChannel(make_runtime(ScriptedModel([])))
    await pair(channel)
    await channel.receive(base.Incoming(chat='owner', text='/mcp_add gmail --provides email --env GMAIL_TOKEN '
                                                           '--env API_KEY=op://wraithd/Gmail/key -- npx -y gmail-mcp'))
    _, text, _ = await card(channel)
    assert 'package: gmail-mcp' in text
    assert 'GMAIL_TOKEN: from WRAITHD_MCP_GMAIL_GMAIL_TOKEN' in text and 'API_KEY: from 1Password op://' in text


@pytest.mark.parametrize('text, problem', [
    ('/mcp_add', 'Usage: /mcp_add NAME'),
    ('/mcp_add Notes -- npx x', 'Server names are lowercase'),
    ('/mcp_add notes', 'Give either a command after --'),
    ('/mcp_add notes --frobnicate -- npx x', 'Unrecognized arguments: --frobnicate'),
    ('/mcp_add notes --url https://x.example --sandbox', '--sandbox is for local servers'),
])
async def test_a_bad_command_says_what_to_fix(make_runtime, text, problem):
    channel = MemoryChannel(make_runtime(ScriptedModel([])))
    await pair(channel)
    await channel.receive(base.Incoming(chat='owner', text=text))
    await channel.settle()
    assert problem in channel.sent[-1][1] and channel.rt.store.list_approvals(status=None) == []


async def test_an_existing_server_is_not_replaced_from_the_phone(make_runtime, tmp_path):
    channel = MemoryChannel(make_runtime(ScriptedModel([])))
    await pair(channel)
    await channel.receive(base.Incoming(chat='owner', text=notes_command(tmp_path)))
    _, _, choices = await card(channel)
    await channel.decide(base.Decision(chat='owner', data=choices[0]))
    await channel.settle()
    await channel.receive(base.Incoming(chat='owner', text='/mcp_add notes -- npx -y something-else'))
    await channel.settle()
    assert channel.sent[-1][1].startswith('There is already a server named notes.')
    assert load_config(channel.rt.home).mcp_servers[0].command == sys.executable


def test_a_phone_keyboards_dashes_and_quotes_still_parse():
    server = remote_add.parse('/mcp_add notes —provides notes — npx -y “notes-mcp”')
    assert server.provides == ('notes',) and server.command == 'npx' and server.args == ('-y', 'notes-mcp')


# --- through Telegram, with the daemon -------------------------------------------------------------------------

async def test_telegram_allow_adds_pins_and_reconnects_the_daemon(make_daemon, isolated, tmp_path):  # noqa: F811
    config.set_key('telegram', BOT_TOKEN)
    fake = FakeTelegram()
    daemon, _ = await make_daemon(ScriptedModel([]), telegram_transport=httpx.MockTransport(fake.handler))
    await paired(daemon, fake)
    assert daemon.mcp.tools == []
    fake.message(OWNER, notes_command(tmp_path))
    await daemon.telegram.poll_once(timeout=0)
    [prompt] = [p for p in fake.sent(OWNER) if 'reply_markup' in p]
    assert prompt['text'].startswith('Add MCP server "notes"?')
    allow = prompt['reply_markup']['inline_keyboard'][0][0]
    assert allow['text'] == 'Allow'
    fake.press(OWNER, allow['callback_data'])
    await daemon.telegram.poll_once(timeout=0)
    await daemon.telegram.settle()
    assert fake.sent(OWNER)[-1]['text'].startswith('Added notes. 2 tools')
    # the daemon's open connections now have it, for every channel and the scheduler
    assert {server for server, _ in daemon.mcp.origin.values()} == {'notes'}
    assert daemon.telegram.mcp is daemon.mcp and daemon.scheduler.mcp is daemon.mcp


async def test_telegram_forwarded_and_stranger_messages_add_nothing(make_daemon, isolated, tmp_path):  # noqa: F811
    config.set_key('telegram', BOT_TOKEN)
    fake = FakeTelegram()
    daemon, _ = await make_daemon(ScriptedModel([]), telegram_transport=httpx.MockTransport(fake.handler))
    await paired(daemon, fake)
    fake.message(STRANGER, notes_command(tmp_path), username='eve')
    fake.message(OWNER, notes_command(tmp_path))
    fake.updates[-1]['message']['forward_origin'] = {'type': 'user', 'date': 0, 'sender_user': {'id': 7}}
    fake.message(OWNER, f'/mcp_add notes --env T={SECRET} -- npx x')
    secret_message = fake.updates[-1]['message']['message_id']
    await daemon.telegram.poll_once(timeout=0)
    await daemon.telegram.settle()
    assert fake.sent(STRANGER) == []
    texts = [p['text'] for p in fake.sent(OWNER)]
    assert texts[-2].startswith('I only add MCP servers from a /mcp_add you type yourself')
    assert texts[-1].startswith('Not added: that looks like it has a secret in it.')
    assert ('deleteMessage', {'chat_id': OWNER, 'message_id': secret_message}) in fake.calls
    assert daemon.rt.store.list_approvals(status=None) == [] and load_config(daemon.rt.home).mcp_servers == ()
    assert ignored(daemon.rt.store) == [{'chat': STRANGER, 'username': 'eve'}]
    assert daemon.rt.store.events_between_runs(50, (remote_add.MCP_ADDED,)) == []


class GatedModel(ScriptedModel):
    """A scripted model whose first turn waits until the test opens the gate (a run still in progress)."""

    def __init__(self, turns):
        super().__init__(turns)
        self.waiting, self.gate = asyncio.Event(), asyncio.Event()

    async def get_response(self, *args, **kwargs):
        if not self.inputs:
            self.waiting.set()
            await self.gate.wait()
        return await super().get_response(*args, **kwargs)


async def until(condition, what):
    for _ in range(1000):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f'never: {what}')


async def test_a_run_in_progress_keeps_its_tools_across_a_reconnect(make_daemon, isolated, home, tmp_path,  # noqa: F811
                                                                     capsys):
    assert cli.main(['mcp', 'add', 'notes', '--', sys.executable, FAKE, '--app', 'notes']) == 0
    config.set_key('telegram', BOT_TOKEN)
    fake = FakeTelegram()
    model = GatedModel([[call('1', 'NOTES_LIST_NOTES', {})], [say('You have groceries and trip ideas.')]])
    daemon, _ = await make_daemon(model, telegram_transport=httpx.MockTransport(fake.handler))
    await paired(daemon, fake)
    old_sessions, old = daemon._sessions, daemon.mcp
    fake.message(OWNER, 'what notes do I have?')
    await daemon.telegram.poll_once(timeout=0)
    await asyncio.wait_for(model.waiting.wait(), 10)  # the run has the old set, and is waiting on the model

    command = [sys.executable, FAKE, '--app', 'notes']
    fake.message(OWNER, f'/mcp_add notes2 -- {shlex.join(command)}')
    await daemon.telegram.poll_once(timeout=0)
    [prompt] = [p for p in fake.sent(OWNER) if 'reply_markup' in p]
    fake.press(OWNER, prompt['reply_markup']['inline_keyboard'][0][0]['callback_data'])
    await daemon.telegram.poll_once(timeout=0)
    await until(lambda: any(p['text'].startswith('Added notes2') for p in fake.sent(OWNER)), 'added')
    await until(lambda: daemon.mcp is not old, 'switched')
    assert {s for s, _ in daemon.mcp.origin.values()} == {'notes', 'notes2'}
    assert daemon.telegram.mcp is daemon.mcp and daemon.scheduler.mcp is daemon.mcp
    await asyncio.sleep(0.2)
    assert not old_sessions.task.done()  # still held by the run

    model.gate.set()  # the run goes on: its tool call goes to the old set's session
    await asyncio.wait_for(daemon.telegram.settle(), 30)  # a closed session would hang the call
    assert fake.sent(OWNER)[-1]['text'] == 'You have groceries and trip ideas.'
    assert 'groceries' in json.dumps(model.inputs[1], default=str)  # the tool's real result, not an error
    await asyncio.wait_for(old_sessions.task, 10)  # and then the old set closes


async def test_mcp_added_is_a_listed_event_type(make_daemon, isolated, capsys):  # noqa: F811
    daemon, client = await make_daemon(ScriptedModel([]))
    ev.record_event(daemon.rt.store, ev.MCP_ADDED, server='notes', transport='stdio', tools=2, connected=True)
    response = await client.get('/api/events', params={'type': 'mcp_added'})
    assert response.status_code == 200
    assert [e['details'] for e in response.json()] == [{'server': 'notes', 'transport': 'stdio', 'tools': 2,
                                                        'connected': True}]
    capsys.readouterr()
    assert cli.main(['events', '--type', 'mcp_added']) == 0
    assert 'mcp_added' in capsys.readouterr().out
