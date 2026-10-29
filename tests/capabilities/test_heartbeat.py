"""C4 the heartbeat: interval and quiet hours on a fake clock, silent runs, read-only tools, the token cap."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from agents.usage import Usage

from local_agent.core import events as ev
from local_agent.core.gate import ToolAccess
from local_agent.daemon import heartbeat, jobs
from tests.capabilities.fakes import enable
from tests.scripted import ScriptedModel, call, say

T0 = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)
WRITES = ('memory_append', 'memory_write', 'files_write', 'files_edit', 'skill_propose', 'schedule_cancel',
          'schedule_create', 'notify_owner', 'skill_adjust', 'delegate', 'browser_open')


class Notifier:
    def __init__(self):
        self.sent = []

    async def deliver(self, text):
        self.sent.append(text)
        return 'test'


def on(home, extra=''):
    home.ensure()
    home.config_path.write_text(home.config_path.read_text().replace('# timezone = "Europe/London"',
                                                                      'timezone = "UTC"'))
    enable(home, f'[heartbeat]\nenabled = true\n{extra}')


def runtime(make_runtime, model):
    rt = make_runtime(model)
    rt.notifier = Notifier()
    return rt


async def test_it_runs_once_per_interval(make_runtime, home):
    on(home, 'every_minutes = 30')
    rt = runtime(make_runtime, ScriptedModel([[say('HEARTBEAT_OK')], [say('HEARTBEAT_OK')]]))
    beat = heartbeat.Heartbeat(rt)
    assert await beat.tick(T0)
    assert await beat.tick(T0 + timedelta(minutes=10)) is None
    assert await beat.tick(T0 + timedelta(minutes=29, seconds=59)) is None
    assert await beat.tick(T0 + timedelta(minutes=30))
    assert len(rt.store.events_between_runs(10, (ev.HEARTBEAT_RUN,))) == 2


async def test_it_keeps_quiet_hours(make_runtime, home):
    on(home, 'quiet_hours = "22:00-07:00"')
    rt = runtime(make_runtime, ScriptedModel([[say('HEARTBEAT_OK')]]))
    beat = heartbeat.Heartbeat(rt)
    for moment in (datetime(2026, 10, 29, 22, 0, tzinfo=UTC), datetime(2026, 10, 30, 3, 0, tzinfo=UTC),
                   datetime(2026, 10, 30, 6, 59, tzinfo=UTC)):
        assert await beat.tick(moment) is None, moment
    assert await beat.tick(datetime(2026, 10, 30, 7, 0, tzinfo=UTC))


def test_quiet_hours_within_a_day_and_none():
    assert heartbeat.in_quiet_hours('12:00-13:00', datetime(2026, 1, 1, 12, 30))
    assert not heartbeat.in_quiet_hours('12:00-13:00', datetime(2026, 1, 1, 13, 0))
    assert not heartbeat.in_quiet_hours('', datetime(2026, 1, 1, 3, 0))


async def test_off_by_default(make_runtime, home):
    rt = runtime(make_runtime, ScriptedModel([]))
    assert rt.config.section('heartbeat')['enabled'] is False
    assert 'heartbeat' not in [job.name for job in jobs.jobs(rt)]
    assert await heartbeat.Heartbeat(rt).tick(T0) is None


async def test_the_daemon_runs_it_when_on(make_runtime, home):
    on(home)
    rt = runtime(make_runtime, ScriptedModel([]))
    [job] = [job for job in jobs.jobs(rt) if job.name == 'heartbeat']
    assert job.every_seconds == heartbeat.TICK_SECONDS


async def test_nothing_new_sends_nothing_and_stays_under_max_tokens(make_runtime, home):
    on(home, 'max_tokens = 5000')
    usage = Usage(requests=1, input_tokens=900, output_tokens=100, total_tokens=1000)
    model = ScriptedModel([[call('1', 'schedule_list', {})], [say('HEARTBEAT_OK')]], usage=usage)
    rt = runtime(make_runtime, model)
    run_id = await heartbeat.Heartbeat(rt).tick(T0)

    assert rt.notifier.sent == []
    [ran] = rt.store.events_between_runs(5, (ev.HEARTBEAT_RUN,))
    [silent] = rt.store.events_between_runs(5, (ev.HEARTBEAT_SILENT,))
    assert ran.payload['run_id'] == silent.payload['run_id'] == run_id and ran.payload['sent'] is False
    assert ran.payload['tokens'] == 2_000 <= 5_000
    start = rt.store.run_events(run_id)[0].payload
    assert start['source'] == 'heartbeat' and start['purpose'] == 'heartbeat'
    assert 'Heartbeat check, Thursday 29 October 2026, 09:00' in start['message']


async def test_it_stops_at_max_tokens(make_runtime, home):
    on(home, 'max_tokens = 1000')
    usage = Usage(requests=1, input_tokens=900, output_tokens=100, total_tokens=1000)
    model = ScriptedModel([[call('1', 'schedule_list', {})], [say('Ran out.')]], usage=usage)
    rt = runtime(make_runtime, model)
    run_id = await heartbeat.Heartbeat(rt).tick(T0)
    assert any(e.type == ev.STOP and e.payload['reason'] == 'token_budget' for e in rt.store.run_events(run_id))


async def test_something_worth_saying_reaches_the_owner_once(make_runtime, home):
    on(home)
    model = ScriptedModel([[say('Your dentist is at 10:00.')], [say('HEARTBEAT_OK')]])
    rt = runtime(make_runtime, model)
    beat = heartbeat.Heartbeat(rt)
    await beat.tick(T0)
    assert rt.notifier.sent == ['Your dentist is at 10:00.']
    await beat.tick(T0 + timedelta(minutes=30))
    assert '- Your dentist is at 10:00.' in model.inputs[1][0]['content']  # it knows what it already said


async def test_the_owners_checklist_is_in_the_message_not_the_prompt(make_runtime, home):
    on(home)
    (home.workspace / 'HEARTBEAT.md').write_text('- Is the build green?')
    model = ScriptedModel([[say('HEARTBEAT_OK')]])
    await heartbeat.run_once(runtime(make_runtime, model), now=T0)
    assert '- Is the build green?' in model.inputs[0][0]['content']
    assert 'Is the build green' not in model.instructions[0] and '09:00' not in model.instructions[0]


async def test_write_tools_are_never_offered(make_runtime, home):
    on(home)
    home.config_path.write_text(home.config_path.read_text() + '\n[browser]\nenabled = true\n'
                                '\n[subagents]\nenabled = true\n')
    model = ScriptedModel([[say('HEARTBEAT_OK')]])
    await heartbeat.run_once(runtime(make_runtime, model), now=T0)
    offered = set(model.tool_names[0])
    assert offered and offered <= heartbeat.READ_ONLY_BUILTINS
    assert not offered & set(WRITES)


async def test_a_write_the_model_asks_for_anyway_is_refused(make_runtime, home):
    """Even a tool the model invents a call to never runs: it isn't there, and the vault is untouched."""
    on(home)
    model = ScriptedModel([[call('1', 'memory_write', {'path': 'profile/x.md', 'text': 'pwned'})]] * 3)
    rt = runtime(make_runtime, model)
    run_id = await heartbeat.run_once(rt, now=T0)
    assert rt.memory.get('profile/x.md') is None and rt.notifier.sent == []
    assert not [e for e in rt.store.run_events(run_id) if e.type == ev.TOOL_RESULT]
    [silent] = rt.store.events_between_runs(5, (ev.HEARTBEAT_SILENT,))
    assert silent.payload['outcome'] == 'failed'  # a failed heartbeat says nothing to the owner


def test_only_read_only_app_tools_are_allowed():
    mcp = SimpleNamespace(access={'GMAIL_FETCH_EMAILS': ToolAccess.READ, 'GMAIL_SEND_EMAIL': ToolAccess.WRITE,
                                  'GMAIL_LIST_ODD': ToolAccess.READ},
                          destructive={'GMAIL_LIST_ODD'})
    allowed = heartbeat.allowed_tools(mcp)
    assert 'GMAIL_FETCH_EMAILS' in allowed and not {'GMAIL_SEND_EMAIL', 'GMAIL_LIST_ODD'} & allowed


async def test_heartbeat_status(make_runtime, home):
    on(home)
    model = ScriptedModel([[say('Your dentist is at 10:00.')]])
    rt = runtime(make_runtime, model)
    await heartbeat.Heartbeat(rt).tick(T0)
    text = heartbeat.status_text(rt, now=T0 + timedelta(minutes=5))
    assert 'Heartbeat: on, every 30 minutes, quiet 22:00-07:00' in text and 'Due now: no' in text
    assert 'Your dentist is at 10:00.' in text


def test_the_cli_has_a_heartbeat_command():
    from local_agent.cli import build_parser

    assert build_parser().parse_args(['heartbeat', 'status']).heartbeat_command == 'status'
