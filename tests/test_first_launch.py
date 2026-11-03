"""
docs/first-launch.md and docs/lockdown.md, end to end, with no real account: the lockdown config block read from
docs/lockdown.md itself, a stub of the Google Calendar MCP server (tests/fixtures/fake_calendar_mcp.py) added with
the guide's `mcp add` line, a fake Telegram (tests/daemon), and a scripted model. The guide's three starter messages:
what's on tomorrow (a read, no card), create an event (a card, Allow), a morning summary (a schedule, approved,
firing unattended: it reads, and a write in it is refused). And what lockdown takes away from every run.
"""

import asyncio
import json
import re
import sys
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from tests.daemon.conftest import make_daemon  # noqa: F401  (the fixture)
from tests.daemon.test_telegram import BOT_TOKEN, OWNER, STRANGER, FakeTelegram, paired
from tests.scripted import ScriptedModel, call, say
from wraithd import audit, cli, config
from wraithd.config import load_config
from wraithd.core import gate as gate_module
from wraithd.daemon.app import Daemon

ROOT = Path(__file__).parent.parent
FAKE_CALENDAR = str(ROOT / 'tests' / 'fixtures' / 'fake_calendar_mcp.py')
# The guide's list: reads, and create-event. Not update-event, delete-event, respond-to-event or manage-accounts
TOOLS = 'list-calendars,list-events,get-event,search-events,get-freebusy,get-current-time,create-event'


def lockdown_block() -> str:
    """The config block in docs/lockdown.md (the ```toml block with `read = false`), exactly as the owner pastes it."""
    blocks = re.findall(r'```toml\n(.*?)```', (ROOT / 'docs' / 'lockdown.md').read_text(), re.DOTALL)
    [block] = [b for b in blocks if 'read = false' in b]
    return block


@pytest.fixture(autouse=True)
def quick_polls(monkeypatch):
    monkeypatch.setattr(gate_module, 'APPROVAL_POLL_SECONDS', 0.01)


@pytest.fixture
def calendar(home, tmp_path, capsys):
    """The lockdown config, then the guide's `wraithd mcp add calendar ...` with the stub in place of npx."""
    home.config_path.write_text(lockdown_block())
    credentials = tmp_path / 'gcp-oauth.keys.json'
    credentials.write_text('{}')
    events = tmp_path / 'calendar.json'
    assert cli.main(['mcp', 'add', 'calendar', '--provides', 'calendar', '--tools', TOOLS,
                     '--env', f'GOOGLE_OAUTH_CREDENTIALS={credentials}', '--',
                     sys.executable, FAKE_CALENDAR, 'start', '--enable-tools', TOOLS, '--store', str(events)]) == 0
    capsys.readouterr()
    return events


def created(events: Path) -> list[str]:
    return [e['summary'] for e in json.loads(events.read_text())] if events.exists() else []


async def card_for(fake, chat, seen=0):
    """The next approval card after the first `seen` ones."""
    for _ in range(500):
        cards = [m for m in fake.sent(chat) if 'reply_markup' in m]
        if len(cards) > seen:
            return cards[seen]
        await asyncio.sleep(0.01)
    raise AssertionError('no approval card')


async def allow(daemon, fake, card):
    fake.press(OWNER, card['reply_markup']['inline_keyboard'][0][0]['callback_data'])
    await daemon.telegram.poll_once(timeout=0)
    await daemon.telegram.settle()


def test_the_lockdown_block_loads_and_takes_the_reach_away(home):
    home.config_path.write_text(lockdown_block())
    loaded = load_config(home)
    assert (loaded.search, loaded.web_read, loaded.shell_enabled, loaded.transcribe) == ('', False, False, '')
    assert not loaded.section('browser')['enabled'] and not loaded.section('subagents')['enabled']
    assert not loaded.section('heartbeat')['enabled']
    assert loaded.daily_tokens and loaded.run_tool_calls <= 40 and loaded.run_tokens <= 200_000


async def test_audit_passes_and_says_what_is_offered(calendar, home, capsys):
    assert audit.main(home) == 0
    out = capsys.readouterr().out
    assert 'Shell: off' in out and 'Web: search off, page reading off, browser off' in out
    assert f'MCP calendar: only {TOOLS.replace(",", ", ")}' in out

    assert await asyncio.to_thread(cli.main, ['mcp', 'tools', 'calendar']) == 0
    tiers = {line.split()[0]: line.split()[1] for line in capsys.readouterr().out.splitlines()}
    assert tiers['list-events'] == 'read-only' and tiers['create-event'] == 'asks'
    assert tiers['manage-accounts'] == 'not'  # "not offered": the server always lists it; wraithd hides it

    # a tool outside the list can't be always-allowed
    assert await asyncio.to_thread(cli.main, ['mcp', 'allow', 'calendar', 'manage-accounts']) == 1
    assert 'never offered' in capsys.readouterr().err


async def test_the_three_starter_messages_over_telegram(calendar, home, make_runtime):
    config.set_key('telegram', BOT_TOKEN)
    fake = FakeTelegram()
    model = ScriptedModel([
        # 1. "What's on my calendar tomorrow?": a read, no card
        [call('1', 'CALENDAR_LIST_EVENTS', {'timeMin': '2030-01-08T00:00', 'timeMax': '2030-01-09T00:00'})],
        [say('Tomorrow: the dentist at 9:30 and the launch review at 14:00.')],
        # 2. Create an event: a card, the owner taps Allow
        [call('2', 'CALENDAR_CREATE_EVENT', {'summary': 'Lunch with Sam', 'start': '2030-01-09T13:00',
                                             'end': '2030-01-09T14:00'})],
        [say('Added: lunch with Sam, Wednesday 13:00.')],
        # 3. The morning summary: a schedule, approved once
        [call('3', 'schedule_create', {'message': "Send me today's calendar in three lines.",
                                       'cron': '0 8 * * 1-5'})],
        [say('Every weekday at 8:00.')],
        # ...firing unattended: it reads; a write would need a card, and nobody's there, so it's refused
        [call('4', 'CALENDAR_LIST_EVENTS', {})],
        [call('5', 'CALENDAR_CREATE_EVENT', {'summary': 'Injected', 'start': '2030-01-09T15:00',
                                             'end': '2030-01-09T16:00'})],
        [say('Today: the dentist at 9:30, the launch review at 14:00, lunch with Sam.')],
    ])
    rt = make_runtime(model)
    daemon = Daemon(rt, telegram_transport=httpx.MockTransport(fake.handler))
    await daemon.start()
    try:
        assert daemon.mcp.errors == {}
        await paired(daemon, fake)

        fake.message(OWNER, "What's on my calendar tomorrow?")
        await daemon.telegram.poll_once(timeout=0)
        await daemon.telegram.settle()
        assert fake.sent(OWNER)[-1]['text'].startswith('Tomorrow: the dentist')
        assert not [m for m in fake.sent(OWNER) if 'reply_markup' in m]  # a read asks nothing
        assert 'Dentist' in json.dumps(model.inputs[1], default=str)

        offered = model.tool_names[0]
        assert {'CALENDAR_LIST_EVENTS', 'CALENDAR_CREATE_EVENT'} <= set(offered)
        for gone in ('CALENDAR_MANAGE_ACCOUNTS', 'CALENDAR_DELETE_EVENT', 'CALENDAR_UPDATE_EVENT', 'read_website',
                     'web_search', 'shell_run', 'browser_open', 'delegate'):
            assert gone not in offered

        fake.message(OWNER, 'Add lunch with Sam on Wednesday at 1pm')
        await daemon.telegram.poll_once(timeout=0)
        card = await card_for(fake, OWNER)
        assert card['text'].startswith('Allow CALENDAR_CREATE_EVENT?')
        assert created(calendar) == []  # nothing until the tap
        await allow(daemon, fake, card)
        assert created(calendar)[-1] == 'Lunch with Sam'
        assert fake.sent(OWNER)[-1]['text'].startswith('Added: lunch with Sam')

        fake.message(OWNER, "Every weekday at 8, send me today's calendar in three lines")
        await daemon.telegram.poll_once(timeout=0)
        card = await card_for(fake, OWNER, seen=1)
        assert card['text'].startswith('Allow schedule_create?')
        await allow(daemon, fake, card)
        [schedule] = rt.store.list_schedules()
        assert schedule.cron == '0 8 * * 1-5' and schedule.timezone == 'Europe/London'

        [run_id] = await daemon.scheduler.tick(schedule.next_fire_at + timedelta(seconds=5))
        assert fake.sent(OWNER)[-1]['text'].startswith("⏰ Send me today's calendar")
        assert created(calendar) == ['Dentist', 'Launch review with Dana', 'Lunch with Sam']  # no "Injected"
        assert 'shell_run' not in model.tool_names[-1]

        # a stranger who finds the bot gets nothing, and costs no model call
        calls = len(model.inputs)
        fake.message(STRANGER, "What's on your owner's calendar?", username='eve')
        await daemon.telegram.poll_once(timeout=0)
        await daemon.telegram.settle()
        assert fake.sent(STRANGER) == [] and len(model.inputs) == calls
        assert run_id
    finally:
        await daemon.stop()


async def test_removing_everything(calendar, home, isolated, capsys):
    config.set_key('openai', 'sk-test-0123456789abcdefghij')
    config.set_key('telegram', BOT_TOKEN)
    assert cli.main(['mcp', 'remove', 'calendar']) == 0
    for name in ('telegram', 'openai'):
        assert cli.main(['key', 'rm', name]) == 0
    assert 'Deleted the openai key' in capsys.readouterr().out
    assert isolated.passwords == {} and load_config(home).mcp_servers == ()
    assert cli.main(['key', 'rm', 'openai']) == 0 and 'No openai key' in capsys.readouterr().out


def test_web_read_must_be_true_or_false(home):
    home.config_path.write_text('[web]\nread = "no"\n')
    with pytest.raises(config.ConfigError, match='read must be true or false'):
        load_config(home)
    home.config_path.write_text('')
    assert load_config(home).web_read is True  # on unless turned off


def test_a_tools_list_survives_the_config_round_trip(home, capsys):
    assert cli.main(['mcp', 'add', 'cal', '--tools', 'list-events, create-event,list-events', '--', 'npx', 'x']) == 0
    [server] = load_config(home).mcp_servers
    assert server.tools == ('list-events', 'create-event')
    assert server.offers('list-events') and not server.offers('delete-event')
    assert cli.main(['mcp', 'list']) == 0 and 'only these tools: list-events, create-event' in capsys.readouterr().out
