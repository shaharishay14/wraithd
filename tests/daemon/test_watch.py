"""Feed watches (daemon/watch.py): the feed is fetched in code, the first poll is a baseline, quiet polls make no
model call, new items run the agent once on only those items (tainted input), NO_REPLY sends nothing, errors retry.
Offline: the feed is an httpx MockTransport behind the real egress checks, the model is scripted."""

import json
import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from tests.capabilities.fakes import enable, public_example_hosts
from tests.scripted import ScriptedModel, say
from wraithd import config as config_module
from wraithd.config import ConfigError, Section, load_config, load_sections
from wraithd.core import events as ev
from wraithd.daemon import jobs, watch

URL = 'https://feed.example/api/agent-feed'
T0 = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
WATCH = f'''[watch.slabbed]
url = "{URL}"
items = "new_cards"
key = "card_url"
every_minutes = 15
message = "use slabbed-x, mode watch"
'''


def card(slug, mrr=1000):
    return {'startup': slug.title(), 'mrr': mrr, 'x_handle': f'@{slug}', 'card_url': f'https://slabbedmrr.com/c/{slug}',
            'created_at': '2026-11-08T11:00:00Z'}


def feed(cards=(), milestones=()):
    """The SlabbedMRR agent feed's shape (src/lib/feed.ts)."""
    return {'edition': '2026-11-08', 'stats': {'startups': 42}, 'new_cards': list(cards),
            'milestones': list(milestones),
            'leaderboard': [{'startup': 'Supersave', 'card_url': 'https://slabbedmrr.com/c/supersave'}]}


class Feed:
    """The feed server: answers what `body` holds now (a dict, or an httpx.Response), and counts requests."""

    def __init__(self, body):
        self.body, self.requests = body, []

    def handler(self, request):
        self.requests.append(request)
        if isinstance(self.body, httpx.Response):
            return self.body
        return httpx.Response(200, json=self.body)

    @property
    def transport(self):
        return httpx.MockTransport(self.handler)


class Notifier:
    def __init__(self):
        self.sent = []

    async def deliver(self, text):
        self.sent.append(text)
        return 'test'


@pytest.fixture
def setup(make_runtime, home, monkeypatch):
    """setup(model, toml=WATCH, body=feed()) -> (rt, watcher, feed server)."""
    public_example_hosts(monkeypatch)

    def make(model, toml=WATCH, body=None):
        home.config_path.write_text(home.config_path.read_text().replace('# timezone = "Europe/London"',
                                                                          'timezone = "UTC"'))
        enable(home, toml)
        rt = make_runtime(model)
        rt.notifier = Notifier()
        server = Feed(feed() if body is None else body)
        return rt, watch.Watcher(rt, clock=lambda: T0, transport=server.transport), server

    return make


def the_watch(rt, name='slabbed'):
    return next(found for found in watch.watches(rt.config) if found.name == name)


def checked(rt):
    return [event.payload for event in rt.store.events_between_runs(50, (ev.WATCH_CHECKED,))]


async def test_the_first_poll_is_a_baseline_and_runs_nothing(setup):
    model = ScriptedModel([])
    rt, watcher, server = setup(model, body=feed([card('alpha'), card('beta')]))
    assert await watcher.poll(the_watch(rt), T0) is None
    assert model.inputs == [] and rt.notifier.sent == []
    assert len(server.requests) == 1
    state = watch.load_state(rt.store, 'slabbed')
    assert state['baselined'] and len(state['seen']) == 2
    [event] = checked(rt)
    assert event['status'] == 'baseline' and event['items'] == 2 and event['new'] == 0


async def test_a_quiet_poll_makes_no_model_call(setup):
    model = ScriptedModel([])
    rt, watcher, server = setup(model, body=feed([card('alpha')]))
    await watcher.poll(the_watch(rt), T0)
    for minutes in (15, 30, 45):
        assert await watcher.poll(the_watch(rt), T0 + timedelta(minutes=minutes)) is None
    assert model.inputs == [] and rt.notifier.sent == []  # 0 model tokens: only the HTTP GETs
    assert len(server.requests) == 4
    assert [event['status'] for event in checked(rt)] == ['ok', 'baseline']  # the same 'ok' isn't recorded again


async def test_a_new_card_runs_once_with_only_the_new_item(setup):
    model = ScriptedModel([[say('Drafted a post for Gamma.')]])
    rt, watcher, server = setup(model, body=feed([card('alpha'), card('beta')]))
    await watcher.poll(the_watch(rt), T0)
    server.body = feed([card('gamma', 5000), card('alpha'), card('beta')])
    run_id = await watcher.poll(the_watch(rt), T0 + timedelta(minutes=15))
    assert run_id and len(model.inputs) == 1
    sent = json.dumps(model.inputs[0], default=str)
    assert 'use slabbed-x, mode watch' in sent and 'https://slabbedmrr.com/c/gamma' in sent
    assert '/c/alpha' not in sent and '/c/beta' not in sent and 'supersave' not in sent
    assert rt.notifier.sent == ['👀 slabbed\n\nDrafted a post for Gamma.']
    [fired] = [event.payload for event in rt.store.events_between_runs(10, (ev.WATCH_FIRED,))]
    assert fired['name'] == 'slabbed' and fired['new'] == 1 and fired['run_id'] == run_id
    # Seen now: the next poll is quiet
    assert await watcher.poll(the_watch(rt), T0 + timedelta(minutes=30)) is None
    assert len(model.inputs) == 1


async def test_the_new_items_are_tainted_third_party_input(setup):
    model = ScriptedModel([[say('ok')]])
    rt, watcher, server = setup(model)
    await watcher.poll(the_watch(rt), T0)
    server.body = feed([card('gamma')])
    run_id = await watcher.poll(the_watch(rt), T0 + timedelta(minutes=15))
    [start] = [event for event in rt.store.run_events(run_id) if event.type == ev.RUN_START]
    assert start.payload['source'] == 'watch'
    assert start.payload['untrusted_inputs'] == ['watch:slabbed']


async def test_no_reply_sends_nothing_and_marks_the_items_seen(setup):
    model = ScriptedModel([[say('NO_REPLY')]])
    rt, watcher, server = setup(model)
    await watcher.poll(the_watch(rt), T0)
    server.body = feed([card('gamma')])
    assert await watcher.poll(the_watch(rt), T0 + timedelta(minutes=15))
    assert rt.notifier.sent == []
    assert await watcher.poll(the_watch(rt), T0 + timedelta(minutes=30)) is None
    assert len(model.inputs) == 1


async def test_a_fetch_error_logs_watch_error_and_retries_next_poll(setup):
    model = ScriptedModel([])
    rt, watcher, server = setup(model, body=httpx.Response(404, text='not found'))
    assert await watcher.poll(the_watch(rt), T0) is None
    [error] = [event.payload for event in rt.store.events_between_runs(10, (ev.WATCH_ERROR,))]
    assert error['name'] == 'slabbed' and '404' in error['reason']
    state = watch.load_state(rt.store, 'slabbed')
    assert not state['baselined'] and state['last_status'] == 'error' and '404' in state['last_error']
    assert not watcher.due(the_watch(rt), state, T0 + timedelta(minutes=14))  # retried at the next interval
    server.body = feed([card('alpha')])
    assert watcher.due(the_watch(rt), state, T0 + timedelta(minutes=15))
    await watcher.poll(the_watch(rt), T0 + timedelta(minutes=15))
    assert watch.load_state(rt.store, 'slabbed')['baselined']  # the first good poll is still the baseline
    assert model.inputs == []


@pytest.mark.parametrize('response, reason', [
    (httpx.Response(200, text='<html>hi</html>', headers={'content-type': 'text/html'}), 'not JSON'),
    (httpx.Response(200, text='{nope', headers={'content-type': 'application/json'}), 'not valid JSON'),
    (httpx.Response(200, json={'new_cards': 'soon'}), 'not a list'),
    (httpx.Response(200, json={'new_cards': {'a': 1}}), 'not a list'),
    (httpx.Response(200, content=b'[' + b'1,' * 600_000 + b'1]', headers={'content-type': 'application/json'}),
     'bigger than 1 MB'),
])
async def test_a_feed_that_isnt_usable_is_a_watch_error(setup, response, reason):
    rt, watcher, _ = setup(ScriptedModel([]), body=response)
    await watcher.poll(the_watch(rt), T0)
    [error] = [event.payload for event in rt.store.events_between_runs(10, (ev.WATCH_ERROR,))]
    assert reason in error['reason']


@pytest.mark.parametrize('body', [{'edition': 'x'}, {'new_cards': None}, {'new_cards': []}])
async def test_an_empty_or_missing_array_is_a_quiet_poll(setup, body):
    model = ScriptedModel([])
    rt, watcher, server = setup(model, body=body)
    await watcher.poll(the_watch(rt), T0)
    await watcher.poll(the_watch(rt), T0 + timedelta(minutes=15))
    assert rt.store.events_between_runs(10, (ev.WATCH_ERROR,)) == [] and model.inputs == []
    assert [(event['status'], event['items']) for event in checked(rt)] == [('ok', 0), ('baseline', 0)]
    server.body = feed([card('gamma')])  # cards appear later: they're new, the baseline was the empty feed
    model.turns.append([say('NO_REPLY')])
    assert await watcher.poll(the_watch(rt), T0 + timedelta(minutes=30))


async def test_a_watch_run_is_unattended_with_source_watch_and_the_default_purpose(setup, monkeypatch):
    """source 'watch' is what picks a scheduled run's model on the Claude engine (run.engine_purpose)."""
    from wraithd import run as run_module
    from wraithd.run import RunResult

    calls = []

    async def fake_run(rt, message, **options):
        calls.append(options)
        return RunResult(answer='NO_REPLY', run_id='r1', outcome='answered')

    rt, watcher, server = setup(ScriptedModel([]))
    monkeypatch.setattr(run_module, 'run', fake_run)
    await watcher.poll(the_watch(rt), T0)
    server.body = feed([card('gamma')])
    assert await watcher.poll(the_watch(rt), T0 + timedelta(minutes=15)) == 'r1'
    [options] = calls
    assert 'purpose' not in options and options['source'] == 'watch' and options['callback'] is None
    assert options['untrusted_source'] == 'watch:slabbed'


async def test_a_private_address_is_never_fetched(setup):
    toml = WATCH.replace(URL, 'http://127.0.0.1/feed')
    rt, watcher, server = setup(ScriptedModel([]), toml=toml)
    await watcher.poll(the_watch(rt), T0)
    [error] = [event.payload for event in rt.store.events_between_runs(10, (ev.WATCH_ERROR,))]
    assert error['reason'].startswith('blocked') and server.requests == []


async def test_a_failed_run_retries_the_items_then_gives_up(setup, monkeypatch):
    monkeypatch.setattr(watch, 'MAX_ATTEMPTS', 2)
    model = ScriptedModel([RuntimeError('model down')] * 3 + [RuntimeError('model down')] * 3)
    rt, watcher, server = setup(model)
    await watcher.poll(the_watch(rt), T0)
    server.body = feed([card('gamma')])
    await watcher.poll(the_watch(rt), T0 + timedelta(minutes=15))
    state = watch.load_state(rt.store, 'slabbed')
    assert state['last_status'] == 'run_failed' and len(state['seen']) == 0  # gamma is still new
    calls = len(model.inputs)
    assert calls >= 1
    await watcher.poll(the_watch(rt), T0 + timedelta(minutes=30))  # tried again, fails again: given up
    assert len(model.inputs) > calls
    state = watch.load_state(rt.store, 'slabbed')
    assert len(state['seen']) == 1 and state['attempts'] == {}
    reasons = [event.payload['reason'] for event in rt.store.events_between_runs(10, (ev.WATCH_ERROR,))]
    assert any('gave up on 1 new item' in reason for reason in reasons)
    assert rt.notifier.sent == []
    calls = len(model.inputs)
    await watcher.poll(the_watch(rt), T0 + timedelta(minutes=45))
    assert len(model.inputs) == calls


async def test_a_composite_key_sees_a_new_milestone_of_the_same_card(setup):
    toml = WATCH.replace('items = "new_cards"', 'items = "milestones"').replace('key = "card_url"',
                                                                                'key = "card_url,crossed"')
    model = ScriptedModel([[say('NO_REPLY')]])
    alpha = {'startup': 'Alpha', 'x_handle': '@alpha', 'card_url': 'https://slabbedmrr.com/c/alpha', 'at': 'x'}
    rt, watcher, server = setup(model, toml=toml, body=feed(milestones=[{**alpha, 'crossed': 1000}]))
    await watcher.poll(the_watch(rt), T0)
    server.body = feed(milestones=[{**alpha, 'crossed': 1000}, {**alpha, 'crossed': 10000}])
    assert await watcher.poll(the_watch(rt), T0 + timedelta(minutes=15))
    sent = json.dumps(model.inputs[0], default=str)
    assert '10000' in sent and '\\"crossed\\": 1000,' not in sent


async def test_long_strings_and_many_items_are_trimmed(setup, monkeypatch):
    monkeypatch.setattr(watch, 'MAX_ITEMS_PER_RUN', 2)
    model = ScriptedModel([[say('NO_REPLY')], [say('NO_REPLY')]])
    rt, watcher, server = setup(model)
    await watcher.poll(the_watch(rt), T0)
    server.body = feed([{**card('a'), 'startup': 'x' * 5000}, card('b'), card('c')])
    await watcher.poll(the_watch(rt), T0 + timedelta(minutes=15))
    sent = json.dumps(model.inputs[0], default=str)
    assert 'x' * 500 in sent and 'x' * 501 not in sent and '/c/c' not in sent
    await watcher.poll(the_watch(rt), T0 + timedelta(minutes=30))  # the one left over runs next poll
    assert '/c/c' in json.dumps(model.inputs[1], default=str)


async def test_tick_keeps_each_watchs_interval_and_quiet_hours(setup):
    toml = WATCH + '\n' + WATCH.replace('[watch.slabbed]', '[watch.night]').replace(
        'every_minutes = 15', 'every_minutes = 15\nquiet_hours = "22:00-07:00"')
    rt, watcher, server = setup(ScriptedModel([]), toml=toml)
    await watcher.tick(T0)
    assert len(server.requests) == 2
    await watcher.tick(T0 + timedelta(minutes=10))
    assert len(server.requests) == 2  # neither is due
    await watcher.tick(datetime(2026, 11, 8, 23, 0, tzinfo=UTC))
    assert len(server.requests) == 3  # only slabbed: night is in its quiet hours


async def test_a_disabled_watch_is_never_polled(setup):
    rt, watcher, server = setup(ScriptedModel([]), toml=WATCH + 'enabled = false\n')
    await watcher.tick(T0)
    assert server.requests == []


async def test_watch_checked_is_recorded_every_nth_unchanged_poll(setup, monkeypatch):
    monkeypatch.setattr(watch, 'CHECKED_EVERY', 3)
    rt, watcher, _ = setup(ScriptedModel([]))
    for poll in range(8):
        await watcher.poll(the_watch(rt), T0 + timedelta(minutes=15 * poll))
    # baseline, then 'ok' (changed), then every 3rd unchanged one
    assert [event['status'] for event in reversed(checked(rt))] == ['baseline', 'ok', 'ok', 'ok']


def test_the_daemon_job_is_on_only_with_a_watch(setup):
    rt, _, _ = setup(ScriptedModel([]), toml='')
    assert 'watch' not in [job.name for job in jobs.jobs(rt)]
    rt, _, _ = setup(ScriptedModel([]))
    [job] = [job for job in jobs.jobs(rt) if job.name == 'watch']
    assert job.every_seconds == watch.TICK_SECONDS


async def test_list_shows_last_poll_seen_count_and_last_error(setup):
    rt, watcher, server = setup(ScriptedModel([]), body=feed([card('alpha'), card('beta')]))
    assert 'last poll: never' in watch.list_text(rt)
    await watcher.poll(the_watch(rt), T0)
    server.body = httpx.Response(500)
    await watcher.poll(the_watch(rt), T0 + timedelta(minutes=15))
    text = watch.list_text(rt)
    assert 'slabbed: on, every 15 min' in text and 'seen: 2' in text and 'last error:' in text and '500' in text


def test_list_without_watches(setup):
    rt, _, _ = setup(ScriptedModel([]), toml='')
    assert 'No watches' in watch.list_text(rt)


# --- config ------------------------------------------------------------------------------------------------

def test_watch_tables_load_from_the_config(home):
    enable(home, WATCH)
    [found] = watch.watches(load_config(home))
    assert found == watch.Watch(name='slabbed', url=URL, items='new_cards', key=('card_url',), every_minutes=15,
                                message='use slabbed-x, mode watch', quiet_hours='', enabled=True)


@pytest.mark.parametrize('change, message', [
    (('every_minutes = 15', 'every_minutes = 4'), 'at least 5'),
    (('key = "card_url"', 'key = " , "'), 'key must name'),
    (('message = "use slabbed-x, mode watch"', 'message = ""'), 'message must say'),
    ((URL, 'ftp://x.example/feed'), 'url must be'),
    (('every_minutes = 15', 'every_minutes = "15"'), 'every_minutes must be int'),
    (('every_minutes = 15', 'every_minutes = 15\nquiet_hours = "late"'), 'quiet_hours must look like'),
    (('every_minutes = 15', 'every_minutes = 15\nevry = 1'), 'evry is not a setting'),
])
def test_a_bad_watch_is_a_config_error(home, change, message):
    enable(home, WATCH.replace(*change))
    with pytest.raises(ConfigError, match=message):
        load_config(home)


def test_an_open_section_keeps_its_tables_and_a_normal_one_still_warns(caplog):
    config_module._warned.clear()
    sections = {'demo': Section({'enabled': False}), 'feeds': Section({}, open=True)}
    with caplog.at_level(logging.WARNING):
        found = load_sections({'demo': {'evry': 1}, 'feeds': {'a': {'url': 'x'}, 'b': {'url': 'y'}}}, sections)
    assert found['feeds'] == {'a': {'url': 'x'}, 'b': {'url': 'y'}}
    assert found['demo'] == {'enabled': False}
    assert '[demo] evry is not a setting' in caplog.text and '[feeds]' not in caplog.text
