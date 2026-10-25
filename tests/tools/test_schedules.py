"""
Schedules and reminders: cron and one-off times in the owner's timezone, due(now) on a fake clock (once per
slot, missed slots once, never twice), creating one through the real run() with the y/N approval, and the CLI.
"""

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from local_agent import cli, schedules
from local_agent.channels import terminal
from local_agent.core import gate as gate_module
from tests.scripted import ScriptedModel, ScriptedTerminal, call, run_once, say

LONDON = 'Europe/London'
# Friday 6 November 2026, 10:00 in London (UTC+0 in winter)
NOW = datetime(2026, 11, 6, 10, 0, tzinfo=UTC)


def local(schedule, tz=LONDON):
    return schedule.next_fire_at.astimezone(ZoneInfo(tz))


# --- creating ----------------------------------------------------------------------------------------------

def test_every_weekday_at_8_is_read_in_the_owners_timezone(store):
    schedule = schedules.create(store, 'Send me my morning brief', 'Asia/Jerusalem', NOW, cron='0 8 * * 1-5')
    # 10:00 UTC Friday is 12:00 in Jerusalem: the next weekday 8:00 there is Monday
    assert local(schedule, 'Asia/Jerusalem') == datetime(2026, 11, 9, 8, 0, tzinfo=ZoneInfo('Asia/Jerusalem'))
    assert schedule.next_fire_at == datetime(2026, 11, 9, 6, 0, tzinfo=UTC)


def test_remind_me_friday_is_one_local_time(store):
    schedule = schedules.create(store, 'Remind me to call the bank', LONDON, NOW, at='2026-11-13T09:00')
    assert schedule.cron is None and local(schedule) == datetime(2026, 11, 13, 9, 0, tzinfo=ZoneInfo(LONDON))
    with_offset = schedules.create(store, 'x', LONDON, NOW, at='2026-11-13T09:00+02:00')
    assert with_offset.next_fire_at == datetime(2026, 11, 13, 7, 0, tzinfo=UTC)


@pytest.mark.parametrize('kwargs, problem', [
    ({'cron': 'every day'}, 'not a 5-field cron'),
    ({'cron': '* * * * *'}, 'at least 15 minutes apart'),
    ({'cron': '0 8 * * *', 'at': '2026-11-13T09:00'}, 'not both'),
    ({}, 'not both or neither'),
    ({'at': 'next friday'}, 'not an ISO time'),
    ({'at': '2026-11-06T09:00'}, 'in the past'),
])
def test_bad_schedules_say_what_to_change(store, kwargs, problem):
    with pytest.raises(schedules.ScheduleError, match=problem):
        schedules.create(store, 'Do it', LONDON, NOW, **kwargs)
    with pytest.raises(schedules.ScheduleError, match='needs the message'):
        schedules.create(store, '  ', LONDON, NOW, cron='0 8 * * *')


def test_the_timezone_comes_from_config_then_the_system(monkeypatch):
    assert schedules.local_timezone('Asia/Tokyo') == 'Asia/Tokyo'
    monkeypatch.setenv('TZ', 'America/New_York')
    assert schedules.local_timezone('') == 'America/New_York'
    monkeypatch.delenv('TZ')
    ZoneInfo(schedules.local_timezone(''))  # whatever this machine says, it's a real zone (or UTC)


# --- due(now) ----------------------------------------------------------------------------------------------

def test_a_schedule_fires_once_per_slot_and_moves_on(store):
    schedule = schedules.create(store, 'brief', LONDON, NOW, cron='0 8 * * 1-5')
    monday_8 = datetime(2026, 11, 9, 8, 0, tzinfo=UTC)
    assert schedules.due(store, monday_8 - timedelta(minutes=1)) == []
    [fired] = schedules.due(store, monday_8)
    assert fired.id == schedule.id
    assert schedules.due(store, monday_8 + timedelta(seconds=30)) == []  # the same minute again: nothing
    assert store.get_schedule(schedule.id).next_fire_at == datetime(2026, 11, 10, 8, 0, tzinfo=UTC)


def test_a_laptop_that_slept_fires_once_not_once_per_missed_slot(store):
    schedules.create(store, 'brief', LONDON, NOW, cron='0 8 * * 1-5')
    thursday_noon = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)  # Mon, Tue, Wed and Thu 8:00 all missed
    assert len(schedules.due(store, thursday_noon)) == 1
    assert schedules.due(store, thursday_noon + timedelta(minutes=1)) == []
    assert store.list_schedules()[0].next_fire_at == datetime(2026, 11, 13, 8, 0, tzinfo=UTC)


def test_a_one_off_fires_once_then_is_done(store):
    schedule = schedules.create(store, 'call the bank', LONDON, NOW, at='2026-11-13T09:00')
    assert len(schedules.due(store, datetime(2026, 11, 13, 9, 0, 5, tzinfo=UTC))) == 1
    assert schedules.due(store, datetime(2026, 11, 14, tzinfo=UTC)) == []
    done = store.get_schedule(schedule.id)
    assert not done.active and done.next_fire_at is None and done.last_fired_at is not None


def test_two_callers_cannot_both_fire_it(store):
    schedule = schedules.create(store, 'brief', LONDON, NOW, cron='0 8 * * 1-5')
    expected = store.get_schedule(schedule.id).next_fire_at
    assert store.claim_schedule(schedule.id, expected, expected + timedelta(days=1), fired_at=expected)
    assert not store.claim_schedule(schedule.id, expected, expected + timedelta(days=1), fired_at=expected)


def test_8am_stays_8am_across_daylight_saving(store):
    # London moves to summer time on 29 March 2026: 8:00 local is 8:00 UTC before, 7:00 UTC after
    schedules.create(store, 'brief', LONDON, datetime(2026, 3, 26, 12, 0, tzinfo=UTC), cron='0 8 * * *')
    fired_at = []
    now = datetime(2026, 3, 27, 0, 0, tzinfo=UTC)
    while now < datetime(2026, 3, 31, tzinfo=UTC):
        if schedules.due(store, now):
            fired_at.append(now.astimezone(ZoneInfo(LONDON)).strftime('%d %H:%M'))
        now += timedelta(minutes=1)
    assert fired_at == ['27 08:00', '28 08:00', '29 08:00', '30 08:00']


def test_cancelled_schedules_never_fire(store):
    schedule = schedules.create(store, 'brief', LONDON, NOW, cron='0 8 * * 1-5')
    assert store.cancel_schedule(schedule.id) and not store.cancel_schedule(schedule.id)
    assert schedules.due(store, NOW + timedelta(days=7)) == []


# --- in runs -----------------------------------------------------------------------------------------------

@pytest.fixture
def london(home, monkeypatch):
    home.config_path.write_text(f'[owner]\ntimezone = "{LONDON}"\n')
    monkeypatch.setattr(gate_module, 'APPROVAL_POLL_SECONDS', 0.01)


def create_turns():
    return [[call('1', 'schedule_create', {'message': 'Send me my morning brief', 'cron': '0 8 * * 1-5'})],
            [say('Done: weekdays at 8.')], [say('{"facts": []}')]]


async def test_every_weekday_at_8_through_a_conversation(make_runtime, london):
    model = ScriptedModel(create_turns())
    rt = make_runtime(model)
    owner = ScriptedTerminal('every weekday at 8, send me my morning brief', 'y')
    await terminal.chat(rt, owner)
    assert 'The agent wants to run schedule_create:' in owner.text and 'run this on its own' in owner.text
    [schedule] = rt.store.list_schedules()
    assert (schedule.message, schedule.cron, schedule.timezone) == ('Send me my morning brief', '0 8 * * 1-5', LONDON)
    created = json.loads(model.tool_outputs(1)['call_1'])['created']
    assert created['when'].startswith('every: 0 8 * * 1-5 (next ') and created['when'].endswith(f'08:00 {LONDON})')
    # the daemon fires it: the agent tells the owner so
    assert 'while Local Agent\'s daemon is running' in json.loads(model.tool_outputs(1)['call_1'])['hint']


async def test_no_means_no_schedule(make_runtime, london):
    rt = make_runtime(ScriptedModel(create_turns()))
    await terminal.chat(rt, ScriptedTerminal('every weekday at 8, send me my morning brief', 'n'))
    assert rt.store.list_schedules() == []


async def test_an_unattended_run_cannot_create_schedules(make_runtime, london):
    rt = make_runtime(ScriptedModel(create_turns()))
    await run_once(rt, 'add a schedule', source='schedule')
    assert rt.store.list_schedules() == []


async def test_list_and_cancel_need_no_approval(make_runtime, london, events):
    model = ScriptedModel([[call('1', 'schedule_list', {})], [call('2', 'schedule_cancel', {'id': 1})],
                           [call('3', 'schedule_cancel', {'id': 99})], [say('Cancelled.')]])
    rt = make_runtime(model)
    schedules.create(rt.store, 'brief', LONDON, datetime.now(UTC), cron='0 8 * * 1-5')
    await run_once(rt, 'cancel my brief', callback=events)
    listed = json.loads(model.tool_outputs(1)['call_1'])
    assert listed['timezone'] == LONDON and listed['schedules'][0]['message'] == 'brief'
    assert json.loads(model.tool_outputs(2)['call_2']) == {'cancelled': 1}
    assert 'no active schedule 99' in json.loads(model.tool_outputs(3)['call_3'])['error']
    assert not any(event == 'approval_required' for event, _ in events.recorded)
    assert rt.store.list_schedules() == []


# --- the CLI -----------------------------------------------------------------------------------------------

def test_schedules_list_and_rm(home, capsys):
    from local_agent.store import SQLiteStore
    real = SQLiteStore(home.db_path)
    schedules.create(real, 'Send me my morning brief', LONDON, NOW, cron='0 8 * * 1-5')
    schedules.create(real, 'Call the bank', LONDON, NOW, at='2026-11-13T09:00')
    real.close()

    assert cli.main(['schedules', 'list']) == 0
    listed = capsys.readouterr().out
    assert ('   1  every: 0 8 * * 1-5 (next Mon 2026-11-09 08:00 Europe/London)\n'
            '      Send me my morning brief') in listed
    assert '   2  once: 2026-11-13T09:00' in listed
    assert cli.main(['schedules', 'rm', '1']) == 0 and 'Cancelled schedule 1.' in capsys.readouterr().out
    assert cli.main(['schedules', 'rm', '1']) == 1 and 'No active schedule 1.' in capsys.readouterr().err
    assert cli.main(['schedules', 'list', '--all']) == 0 and '[off]' in capsys.readouterr().out
