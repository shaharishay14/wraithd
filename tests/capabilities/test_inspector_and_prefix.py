"""runs show nests subagents; between-runs events read as one line; the new tools keep the prompt prefix stable."""

from local_agent.core import events as ev
from local_agent.core import inspector
from local_agent.core.events import Event
from local_agent.tools.browser import JsonSiteDriver
from tests.capabilities.fakes import enable
from tests.scripted import ScriptedModel, call, of_type, run_once, say


async def test_runs_show_nests_the_subagent_under_its_delegate_call(make_runtime, home):
    enable(home, '[subagents]\nenabled = true')
    model = ScriptedModel([[call('1', 'delegate', {'task': 'Add 2 and 2'})], [say('4')], [say('It is 4.')]])
    rt = make_runtime(model)
    result, events = await run_once(rt, 'Add')
    child_id = of_type(events, ev.SUBAGENT_RESULT)[0].payload['child_run_id']

    text = inspector.render_text(result.run_id, events, children=rt.store.run_events)
    lines = text.splitlines()
    at = next(i for i, line in enumerate(lines) if 'subagent_result' in line)
    assert lines[at + 1] == f'      │ Run {child_id}'
    assert f'      │   subagent of run {result.run_id}' in lines
    assert inspector.summarize(events)['subagents'] == [child_id]
    # Without a loader (the API's run detail), the child's id is still on the delegate line
    assert child_id[:20] in inspector.render_text(result.run_id, events)


def test_between_runs_events_read_as_one_line():
    def line(event_type, **payload):
        return inspector.between_runs_text(Event(seq=0, type=event_type, payload=payload))

    assert line(ev.REVIEW_QUEUED, id=3, kind='skill', source='agent', run_id='abcdef1234', taint=['read_website']) \
        == 'Queued skill item #3 for your review (run abcdef12), after reading read_website'
    assert line(ev.REVIEW_APPROVED, id=3, kind='memory', by='cli') == \
        'You approved memory item #3 (cli), and it was applied'
    assert line(ev.REVIEW_NOTIFIED, items=2, counts={}, channel='telegram') == \
        'Told you 2 items wait for review (on telegram)'
    assert 'asks again' in line(ev.MCP_TOOL_CHANGED, server='gmail', tool='send', changes=[], loosened=True)
    assert line(ev.HEARTBEAT_RUN, run_id='r1', sent=False, tokens=900) == \
        'Heartbeat (run r1): nothing worth a message · 900 tokens'
    assert line(ev.HEARTBEAT_SILENT, run_id='r1', outcome='failed') == \
        'Heartbeat stayed quiet (run r1) (the run failed)'
    assert line(ev.REVIEW_QUEUED, broken=True) == '{"broken": true}'  # an odd payload still shows, as JSON


async def test_the_prefix_is_stable_with_the_new_tools_on(make_runtime, home):
    enable(home, '[browser]\nenabled = true\n\n[subagents]\nenabled = true')
    models = []
    for message in ('first', 'second'):
        model = ScriptedModel([[say('ok')]])
        rt = make_runtime(model)
        rt.browser_driver = JsonSiteDriver
        _, events = await run_once(rt, message)
        models.append((model, of_type(events, ev.CONTEXT_BUILT)[0].payload['prefix_hash']))
    (first, hash_1), (second, hash_2) = models
    assert first.instructions[0] == second.instructions[0] and first.tool_names[0] == second.tool_names[0]
    assert hash_1 == hash_2 and {'delegate', 'browser_open'} <= set(first.tool_names[0])


async def test_off_the_prefix_is_what_it_was(make_runtime, monkeypatch):
    from local_agent.core import delegate
    from local_agent.tools import browser

    off = ScriptedModel([[say('ok')]])
    _, events = await run_once(make_runtime(off), 'hi')
    monkeypatch.setattr(browser, 'run_tools', lambda *a, **k: [])
    monkeypatch.setattr(delegate, 'delegate_tools', lambda *a, **k: [])
    without = ScriptedModel([[say('ok')]])
    _, baseline = await run_once(make_runtime(without), 'hi')
    assert of_type(events, ev.CONTEXT_BUILT)[0].payload['prefix_hash'] == \
        of_type(baseline, ev.CONTEXT_BUILT)[0].payload['prefix_hash']
