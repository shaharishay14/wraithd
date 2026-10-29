"""E4: tool output over [spill] over_chars goes to a file, redacted; the model gets today's preview plus where the
rest is; reading it back taints the run."""

import json
import os
import time

from agents import FunctionTool

from local_agent.config import set_values
from local_agent.core import events as ev
from local_agent.core import render, views
from local_agent.spill import FOLDER, cleanup, cleanup_job, spill_hook
from tests.scripted import ScriptedModel, call, fake_tool, of_type, run_once, say

TOKEN = 'sk-proj-' + 'A1b2C3d4E5f6G7h8I9j0' * 2
PAGE = ('Quarterly report. ' * 300) + f'Config dump: OPENAI={TOKEN}. ' + ('Appendix line. ' * 400)


def page_tool(text=PAGE, name='BROWSER_GET_PAGE'):
    """An app action returning a big page: third-party content, like any MCP tool's output."""
    async def fetch(ctx, payload):
        return text
    return FunctionTool(name=name, description='Fetch a page.', on_invoke_tool=fetch,
                        params_json_schema={'type': 'object', 'properties': {}}, strict_json_schema=False)


def spilled_files(home):
    return sorted((home.files_dir / FOLDER).rglob('*.txt'))


async def test_a_large_page_puts_a_preview_in_context_and_the_full_text_on_disk(make_runtime, home):
    model = ScriptedModel([[call('1', 'BROWSER_GET_PAGE', {})], [say('Read it.')]])
    rt = make_runtime(model)
    result, events = await run_once(rt, 'read the report page', app_tools=[page_tool()])

    [path] = spilled_files(home)
    saved = path.read_text()
    assert path.relative_to(home.files_dir).as_posix() == f'{FOLDER}/{result.run_id}/1-BROWSER_GET_PAGE.txt'
    assert len(saved) > render.MAX_TOOL_RESULT_CHARS * 2 and saved.endswith('Appendix line. ')
    assert oct(path.stat().st_mode & 0o777) == '0o600' and oct(path.parent.stat().st_mode & 0o777) == '0o700'

    seen = model.tool_outputs(1)['call_1']
    assert seen.startswith('<untrusted_tool_output tool="BROWSER_GET_PAGE">')  # still third-party
    # The model gets what it got before spilling (the first 4,000 characters), then the size and the path
    assert PAGE[:render.MAX_TOOL_RESULT_CHARS] in seen and 'Appendix' not in seen
    shown = f'{FOLDER}/{result.run_id}/1-BROWSER_GET_PAGE.txt'
    assert f'showing the first 4,000 of {len(saved):,} characters' in seen
    assert f"files_read(path='{shown}', offset=4000)" in seen and 'TRUNCATED' not in seen

    [spilled] = of_type(events, ev.SPILLED)
    assert spilled.payload == {'tool': 'BROWSER_GET_PAGE', 'chars': len(saved), 'path': shown, 'untrusted': True}


async def test_a_token_shaped_string_is_redacted_on_disk(make_runtime, home):
    model = ScriptedModel([[call('1', 'BROWSER_GET_PAGE', {})], [say('ok')]])
    await run_once(make_runtime(model), 'read it', app_tools=[page_tool()])
    [path] = spilled_files(home)
    saved = path.read_text()
    assert TOKEN not in saved and 'OPENAI=[REDACTED:openai_key]' in saved


async def test_reading_the_spilled_file_back_taints_the_run(make_runtime, home):
    # A trusted tool's big output, so the only third-party content is the file read back
    big = fake_tool('calculate', lambda **a: {'rows': [f'row {n}' for n in range(1500)]})
    first = ScriptedModel([[call('1', 'calculate', {})], [say('done')]])
    result, events = await run_once(make_runtime(first), 'compute', tools=[big])
    assert views.untrusted_content_seen(events) == []
    shown = of_type(events, ev.SPILLED)[0].payload['path']

    second = ScriptedModel([[call('2', 'files_read', {'path': shown, 'offset': 4000})], [say('ok')]])
    _, later = await run_once(make_runtime(second), 'read the rest')
    output = second.tool_outputs(1)['call_2']
    assert output.startswith('<untrusted_tool_output tool="files_read">') and 'row ' in output
    assert f'"path": "{shown}"' in output
    assert views.untrusted_content_seen(later) == ['files_read']
    assert of_type(later, ev.SPILLED) == []  # files_read is never spilled: it already pages


async def test_small_results_and_files_read_are_left_alone(make_runtime, home):
    hook = spill_hook(make_runtime(ScriptedModel([])), ev.EventLog())
    assert hook('calculate', 'x' * render.MAX_TOOL_RESULT_CHARS, False) == 'x' * render.MAX_TOOL_RESULT_CHARS
    assert hook('files_read', 'x' * 50_000, True) == 'x' * 50_000
    assert spilled_files(home) == []


async def test_spill_can_be_turned_off(make_runtime, home):
    set_values(home, 'spill', {'over_chars': 0})
    model = ScriptedModel([[call('1', 'BROWSER_GET_PAGE', {})], [say('ok')]])
    _, events = await run_once(make_runtime(model), 'read it', app_tools=[page_tool()])
    assert spilled_files(home) == [] and of_type(events, ev.SPILLED) == []
    assert 'TRUNCATED' in model.tool_outputs(1)['call_1']  # as before spill existed


async def test_a_higher_threshold_spills_less(make_runtime, home):
    set_values(home, 'spill', {'over_chars': 100_000})
    model = ScriptedModel([[call('1', 'BROWSER_GET_PAGE', {})], [say('ok')]])
    await run_once(make_runtime(model), 'read it', app_tools=[page_tool()])
    assert spilled_files(home) == []


async def test_each_spill_in_a_run_gets_its_own_file(make_runtime, home):
    model = ScriptedModel([[call('1', 'BROWSER_GET_PAGE', {}), call('2', 'BROWSER_GET_PAGE', {})], [say('ok')]])
    await run_once(make_runtime(model), 'read it twice', app_tools=[page_tool()])
    assert [path.name for path in spilled_files(home)] == ['1-BROWSER_GET_PAGE.txt', '2-BROWSER_GET_PAGE.txt']


def test_cleanup_deletes_old_run_folders_only(home):
    root = home.files_dir / FOLDER
    for name, age_days in (('old', 8), ('recent', 2)):
        (root / name).mkdir(parents=True)
        (root / name / '1-x.txt').write_text('x')
        stamp = time.time() - age_days * 86400
        os.utime(root / name, (stamp, stamp))
    assert cleanup(home, keep_days=0) == []  # 0: kept
    assert cleanup(home, keep_days=7) == ['old']
    assert sorted(path.name for path in root.iterdir()) == ['recent']


async def test_the_cleanup_job_records_what_it_deleted(make_runtime, home):
    rt = make_runtime(ScriptedModel([]))
    folder = home.files_dir / FOLDER / 'old-run'
    folder.mkdir(parents=True)
    stamp = time.time() - 30 * 86400
    os.utime(folder, (stamp, stamp))
    await cleanup_job(rt)
    [event] = rt.store.events_between_runs(5, (ev.SPILL_CLEANED,))
    assert event.payload == {'folders': 1, 'keep_days': 7} and not folder.exists()
    await cleanup_job(rt)  # nothing to delete: no event
    assert len(rt.store.events_between_runs(5, (ev.SPILL_CLEANED,))) == 1


def test_spill_cleanup_is_a_daily_daemon_job(make_runtime):
    from local_agent.daemon import jobs
    [job] = [job for job in jobs.jobs(make_runtime(ScriptedModel([]))) if job.name == 'spill_cleanup']
    assert job.every_seconds == 24 * 3600 and job.run is cleanup_job


async def test_a_spilled_json_result_is_still_readable_by_the_model(make_runtime, home):
    rows = fake_tool('calculate', lambda **a: {'rows': [f'row {n}' for n in range(1500)]})
    model = ScriptedModel([[call('1', 'calculate', {})], [say('done')]])
    await run_once(make_runtime(model), 'compute', tools=[rows])
    output = model.tool_outputs(1)['call_1']
    assert output.startswith('{"rows": ["row 0"')
    [path] = spilled_files(home)
    assert json.loads(path.read_text())['rows'][-1] == 'row 1499'
