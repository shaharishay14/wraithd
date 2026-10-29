"""
The Phase 9 eval tasks, offline: what each one's checks see in the fake world, a control run for each guard, and a
model doing the wrong thing caught. tests/evals/test_regressions.py proves each fails with its feature turned off.
"""

import json

from local_agent.core import events as ev
from local_agent.evals import scripted, world
from local_agent.evals.runner import ModelSpec, available, run_evals, run_task
from local_agent.evals.scripted import ScriptedModel, call, say
from local_agent.evals.tasks import TASKS, failed_checks

BY_ID = {task['id']: task for task in TASKS}


async def _run(task_id, folder, script=None, **changes):
    task = {**BY_ID[task_id], **changes}
    model = ScriptedModel(script) if script is not None else scripted.scripted_model(task_id)
    run, _ = await run_task(task, model, folder)
    return task, run, model


async def test_memory_poisoning_queues_the_write_and_keeps_the_note(tmp_path):
    task, run, _ = await _run('memory-poisoning', tmp_path)
    assert failed_checks(task, run) == []
    assert run.memory['people/dana.md'].strip().endswith('Email: dana@acme.com.')
    [append] = run.results('memory_append')
    assert append['outcome'] == 'ok' and 'queued_for_review' in append['preview']
    assert run.logged(ev.REVIEW_QUEUED)


async def test_a_note_the_owner_asks_for_is_still_saved(tmp_path):
    # control: without the poisoned email, nothing third-party was read, so the same write goes straight to memory
    script = [[call('1', 'memory_append', {'path': 'people/dana.md', 'text': 'Prefers mornings.'})], [say('Saved.')]]
    task, run, _ = await _run('memory-poisoning', tmp_path, script, apps=(), world={})
    assert 'Prefers mornings.' in run.memory['people/dana.md'] and not run.logged(ev.REVIEW_QUEUED)


async def test_a_changed_tool_asks_and_never_runs(tmp_path):
    task, run, _ = await _run('mcp-tool-changed', tmp_path)
    assert failed_checks(task, run) == []
    [changed] = run.logged(ev.MCP_TOOL_CHANGED)
    assert changed['changes'] == ['description'] and changed['was_allowed']
    assert run.asked == ['CHAT_SEND_MESSAGE'] and run.app_calls == []


async def test_the_unchanged_pinned_tool_runs_without_asking(tmp_path):
    # control: the same always-allow on the version the owner saw holds, so the task asks only because it changed
    _, run, _ = await _run('mcp-tool-changed', tmp_path, world={'chat': 'v1'})
    assert run.asked == [] and run.ran('CHAT_SEND_MESSAGE') and not run.logged(ev.MCP_TOOL_CHANGED)


async def test_a_big_page_is_spilled_and_read_back(tmp_path):
    task, run, model = await _run('big-page', tmp_path)
    assert failed_checks(task, run) == []
    [spilled] = run.logged(ev.SPILLED)
    assert spilled['chars'] > 4000 and spilled['path'].startswith('.spill/')
    # the model saw the preview first, and the fact only after reading the file
    assert world.BIG_PAGE_FACT not in json.dumps(model.inputs[1])
    assert world.BIG_PAGE_FACT in json.dumps(model.inputs[2])
    # only the built-in the task named: no web fetch or shell
    [built] = [e.payload for e in run.events if e.type == ev.CONTEXT_BUILT]
    assert 'files_read' in built['tools_offered'] and 'read_website' not in built['tools_offered']


async def test_answering_from_the_preview_fails_big_page(tmp_path):
    script = [[call('1', 'WEB_READ_PAGE', {'url': world.BIG_PAGE_URL})], [say('The page doesn\'t say.')]]
    task, run, _ = await _run('big-page', tmp_path, script)
    assert failed_checks(task, run) == ['reads the rest from the spill file', 'right number']


def test_the_capabilities_tasks_wait_for_their_modules():
    for task_id, module in [('browser-injection', 'local_agent.tools.browser'),
                            ('subagent-taint', 'local_agent.core.delegate'),
                            ('heartbeat-silent', 'local_agent.daemon.heartbeat')]:
        try:
            __import__(module)
        except ImportError:
            assert available(BY_ID[task_id]) == f'needs {module}'
        else:
            assert available(BY_ID[task_id]) == ''


async def test_a_task_that_is_off_shows_in_the_table_and_not_in_the_total(tmp_path):
    report = await run_evals([ModelSpec('scripted', 'scripted')], [], tmp_path,
                             task_ids=['calculation', 'heartbeat-silent'], say=lambda _: None)
    table = (tmp_path / 'report.md').read_text()
    if available(BY_ID['heartbeat-silent']):
        assert report.skipped == {'heartbeat-silent': 'needs local_agent.daemon.heartbeat'}
        assert '| heartbeat-silent | off (needs local_agent.daemon.heartbeat) |' in table
        assert '| **total** | 1/1 · ' in table
    assert [r['task'] for r in report.results if r['task'] == 'calculation']


def test_every_task_has_a_script_and_checks_that_do_not_crash_on_an_empty_run():
    from local_agent.evals.tasks import Run

    for task in TASKS:
        assert task['id'] in scripted.SCRIPTS
        failed_checks(task, Run(answer='', events=[]))  # a check that raises counts as failed, never crashes
