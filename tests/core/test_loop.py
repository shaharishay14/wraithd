"""
The harness around real runs: run() with the Agents SDK Runner and a scripted model. Multi-step tool use,
errors going back to the model, the step limit and stop conditions ending in an honest answer, the result cap,
retries, the strong-model switch, and the event timeline every run leaves in the store.
"""

import pytest
from agents import Agent

from local_agent.core import events as ev
from local_agent.core import gate as gate_module
from local_agent.core import loop, render
from local_agent.core.gate import PermissionGate, guard_function_tools
from tests.scripted import ScriptedModel, call, fake_tool, of_type, run_once, say

WEATHER = fake_tool('get_weather', lambda **args: {'tool': 'get_weather', **args})
CALCULATE = fake_tool('calculate', lambda **args: {'tool': 'calculate', **args})


def broken(**args):
    raise ConnectionError('weather service down')


async def test_multi_step_task_runs_tools_until_the_model_answers(make_runtime, events):
    model = ScriptedModel([
        [call('1', 'get_weather', {'location': 'Paris'})],
        [call('2', 'calculate', {'expression': '18 * 9 / 5 + 32'})],
        [say('It is 64.4F in Paris')],
    ])
    result, _ = await run_once(make_runtime(model), 'Paris weather in Fahrenheit?', tools=[WEATHER, CALCULATE],
                               callback=events, owner=False)
    assert result.answer == 'It is 64.4F in Paris' and result.outcome == 'answered'
    # the answer was streamed to the channel in pieces, in order
    deltas = [data for event, data in events.recorded if event == 'content_delta']
    assert len(deltas) > 1 and ''.join(d['content'] for d in deltas) == 'It is 64.4F in Paris'
    assert [d['index'] for d in deltas] == list(range(len(deltas)))
    assert model.tool_names[0] == ['calculate', 'get_weather']  # a stranger gets no memory or skill tools
    assert 'Paris' in model.tool_outputs(1)['call_1']  # step 2 saw step 1's result
    assert '18 * 9' in model.tool_outputs(2)['call_2']


async def test_a_run_leaves_a_full_event_timeline(make_runtime):
    model = ScriptedModel([[call('1', 'get_weather', {'location': 'Paris'})], [say('Sunny in Paris')]])
    result, events = await run_once(make_runtime(model), 'weather in Paris?', tools=[WEATHER])
    assert result.answer == 'Sunny in Paris'

    assert [e.type for e in events] == [
        ev.RUN_START, ev.CONTEXT_BUILT,
        ev.TURN_START, ev.MODEL_OUTPUT, ev.TOOL_CALL, ev.TOOL_RESULT,
        ev.TURN_START, ev.MODEL_OUTPUT,
        ev.RUN_END,
    ]
    by_type = {e.type: e.payload for e in events}
    assert by_type[ev.RUN_START]['source'] == 'chat' and by_type[ev.RUN_START]['message'] == 'weather in Paris?'
    # the owner gets memory and skill tools next to the run's own
    assert by_type[ev.CONTEXT_BUILT]['tools_offered'] == ['get_weather', 'memory_search', 'memory_read',
                                                          'memory_append', 'memory_write', 'skill_load',
                                                          'skill_propose', 'skill_adjust']
    assert by_type[ev.CONTEXT_BUILT]['skills_offered'] == ['skill-writer', 'weekly-review']
    assert by_type[ev.CONTEXT_BUILT]['memory_notes'] == 0 and by_type[ev.CONTEXT_BUILT]['model'] == 'scripted'
    assert by_type[ev.TOOL_CALL] == {'tool': 'get_weather', 'arguments': {'location': 'Paris'}, 'access': 'read',
                                     'allowed': True}
    assert by_type[ev.TOOL_RESULT]['outcome'] == 'ok'
    assert by_type[ev.MODEL_OUTPUT]['text_chars'] == len('Sunny in Paris')
    assert by_type[ev.RUN_END]['outcome'] == 'answered' and by_type[ev.RUN_END]['turns'] == 2
    assert by_type[ev.RUN_END]['stop_reason'] is None


async def test_the_prompt_has_the_standing_orders_memory_and_skills(make_runtime, home):
    home.agents_md.write_text('Always answer in English, even when I write in Hebrew.')
    model = ScriptedModel([[say('hi')]])
    await run_once(make_runtime(model), 'hi')
    instructions = model.instructions[0]
    assert 'Always answer in English' in instructions
    assert 'MEMORY (what you know about the user' in instructions and 'No notes yet' in instructions
    assert '- weekly-review:' in instructions and '<system-reminder>' in instructions


async def test_tool_crash_goes_back_to_the_model_as_an_error(make_runtime):
    def flaky(**args):
        if args.get('location') == 'Atlantis':
            raise ValueError('Unknown place Atlantis')
        return {'location': args.get('location')}

    model = ScriptedModel([
        [call('1', 'get_weather', {'location': 'Atlantis'})],
        [call('2', 'get_weather', {'location': 'Athens'})],
        [say('Athens is sunny')],
    ])
    result, _ = await run_once(make_runtime(model), 'weather in Atlantis?', tools=[fake_tool('get_weather', flaky)])
    assert result.answer == 'Athens is sunny'
    assert 'Unknown place Atlantis' in model.tool_outputs(1)['call_1']


async def test_three_tool_errors_in_a_row_stop_the_run_with_a_reason(make_runtime):
    model = ScriptedModel([[call(str(i), 'get_weather', {'location': city})]
                           for i, city in enumerate(['Paris', 'Lyon', 'Nice'])]
                          + [[say('The weather service is down, so I could not check.')]])

    result, events = await run_once(make_runtime(model), 'weather in four cities?',
                                    tools=[fake_tool('get_weather', broken)])

    assert result.answer == 'The weather service is down, so I could not check.'
    # three failed calls, then the stop: one more model call, with no tools, to answer
    assert len(model.inputs) == 4 and model.tool_names[-1] == []
    final_instruction = model.inputs[-1][-1]
    assert final_instruction['role'] == 'system' and 'last 3 tool calls failed' in final_instruction['content']
    assert [e.payload['outcome'] for e in of_type(events, ev.TOOL_RESULT)] == ['error'] * 3
    assert of_type(events, ev.STOP)[0].payload['reason'] == 'tool_errors'
    assert events[-1].payload['stop_reason'] == 'tool_errors'


async def test_the_same_call_three_times_is_a_loop(make_runtime):
    model = ScriptedModel([[call(str(i), 'calculate', {'expression': '1+1'})] for i in range(3)] + [[say('It is 2.')]])
    result, events = await run_once(make_runtime(model), 'what is 1+1?', tools=[fake_tool('calculate', lambda **a: 2)])
    assert result.answer == 'It is 2.'
    assert len(model.inputs) == 4 and model.tool_names[-1] == []
    assert 'calculate was called 3 times' in model.inputs[-1][-1]['content']
    assert events[-1].payload['stop_reason'] == 'repeated_call'


async def test_step_limit_forces_an_honest_final_answer(make_runtime):
    steps = loop.MAX_AGENT_STEPS
    model = ScriptedModel([[call(str(i), 'calculate', {'expression': f'{i}+1'})] for i in range(steps)]
                          + [[say('I could not finish: still calculating')]])

    result, events = await run_once(make_runtime(model), 'loop forever', tools=[fake_tool('calculate', lambda **a: 2)])
    assert result.answer == 'I could not finish: still calculating'
    assert len(model.inputs) == steps + 1
    assert model.tool_names[-1] == []  # the last call has no tools
    final_input = model.inputs[-1]
    assert final_input[-1] == {'role': 'system', 'content': loop.STEP_LIMIT_INSTRUCTION}
    # ...and it still has everything the agent did, so it can't pretend
    assert sum(1 for item in final_input if isinstance(item, dict) and item.get('type') == 'function_call_output') \
        == steps
    assert of_type(events, ev.STOP)[0].payload['reason'] == 'max_turns'


async def test_big_tool_result_is_capped_and_marked(make_runtime):
    model = ScriptedModel([[call('1', 'web_search', {'query': 'x'})], [say('done')]])
    await run_once(make_runtime(model), 'search', tools=[fake_tool('web_search', lambda **a: 'x' * 100_000)])
    content = model.tool_outputs(1)['call_1']
    assert 'TRUNCATED' in content and len(content) < render.MAX_TOOL_RESULT_CHARS + 1200
    assert content.startswith('<untrusted_tool_output tool="web_search">')


async def test_plain_chat_streams_through_the_same_runner(make_runtime, events):
    model = ScriptedModel([[say('Hello! How can I help?')]])
    result, _ = await run_once(make_runtime(model), 'hi', callback=events, owner=False)
    assert result.answer == 'Hello! How can I help?'
    assert model.tool_names == [[]]
    assert ''.join(d['content'] for e, d in events.recorded if e == 'content_delta') == 'Hello! How can I help?'


async def test_a_blocked_tool_is_logged_as_denied(make_runtime, monkeypatch):
    monkeypatch.setattr(gate_module, 'BUILTIN_TOOL_ACCESS', {**gate_module.BUILTIN_TOOL_ACCESS,
                                                             'calculate': gate_module.ToolAccess.DENY})
    model = ScriptedModel([[call('1', 'calculate', {'expression': '1+1'})], [say('I am not allowed to calculate.')]])
    _, events = await run_once(make_runtime(model), 'what is 1+1?', tools=[CALCULATE])
    tool_call, tool_result = of_type(events, ev.TOOL_CALL)[0], of_type(events, ev.TOOL_RESULT)[0]
    assert tool_call.payload['allowed'] is False and tool_call.payload['access'] == 'deny'
    assert tool_result.payload['outcome'] == 'denied'


# --- failures and retries ----------------------------------------------------------------------------------

async def test_failure_before_anything_happened_is_retried(make_runtime):
    model = ScriptedModel([ConnectionError('blip'), [say('ok after retry')]])
    result, _ = await run_once(make_runtime(model), 'hi')
    assert result.answer == 'ok after retry' and len(model.inputs) == 2


async def test_failure_after_a_tool_ran_is_not_retried(store):
    ran = []
    model = ScriptedModel([[call('1', 'get_weather', {'location': 'Paris'})], ConnectionError('blip'),
                           [call('2', 'get_weather', {'location': 'Paris'})], [say('ran twice')]])
    tools = guard_function_tools([fake_tool('get_weather', lambda **a: ran.append('get_weather') or 1)],
                                 PermissionGate(store))
    with pytest.raises(loop.RunAlreadyStartedError):
        await loop.run_with_step_limit(Agent(name='t', instructions='', tools=tools, model=model),
                                       [{'role': 'user', 'content': 'hi'}])
    assert ran == ['get_weather']  # the tool ran once, not again


async def test_a_failed_run_says_what_was_already_done_and_is_saved(make_runtime, monkeypatch):
    monkeypatch.setattr(gate_module, 'BUILTIN_TOOL_ACCESS', {**gate_module.BUILTIN_TOOL_ACCESS,
                                                             'send_note': gate_module.ToolAccess.WRITE})
    sent = []
    model = ScriptedModel([[call('1', 'send_note', {'recipient_email': 'me@home.com', 'body': 'hi'})],
                           ConnectionError('blip')])
    rt = make_runtime(model)
    from local_agent.store import base as store_base

    async def approve(event_type, data):
        if event_type == 'approval_required':
            rt.store.decide_approval(data['approval_id'], store_base.APPROVED)

    monkeypatch.setattr(gate_module, 'APPROVAL_POLL_SECONDS', 0.01)
    result, events = await run_once(rt, 'send me a note', callback=approve,
                                    tools=[fake_tool('send_note', lambda **a: sent.append(a) or 'sent')])
    assert sent and result.outcome == 'failed'
    assert 'These actions were already done:\n- Send note (to me@home.com)' in result.answer
    assert events[-1].type == ev.RUN_END and events[-1].payload['outcome'] == 'failed'


async def test_nothing_done_says_nothing_changed(make_runtime):
    model = ScriptedModel([ConnectionError('a'), ConnectionError('b'), ConnectionError('c')])
    result, events = await run_once(make_runtime(model), 'hi')
    assert result.outcome == 'failed' and 'Nothing was changed' in result.answer
    assert events[-1].payload['error'] == 'ConnectionError'


async def test_over_the_daily_budget_no_model_call_is_made(make_runtime, home):
    home.config_path.write_text('[budget]\ndaily_tokens = 1000\n')
    rt = make_runtime(ScriptedModel([]))
    rt.store.save_events('earlier', [ev.Event(seq=0, type=ev.RUN_END, payload={'tokens': 1200})])
    from local_agent.core.stop import DAILY_BUDGET_MESSAGE
    result, events = await run_once(rt, 'hi')
    assert result.answer == DAILY_BUDGET_MESSAGE and result.outcome == 'refused'
    assert events[-1].payload['stop_reason'] == 'daily_budget'


# --- the strong model --------------------------------------------------------------------------------------

STRONG_SKILL = '---\nname: careful-triage\ndescription: Sort things carefully.\nmodel: strong\n---\n1. Be careful.\n'


async def test_a_strong_skill_moves_the_rest_of_the_run_to_the_strong_model(make_runtime, home):
    (home.skills_dir / 'careful-triage').mkdir()
    (home.skills_dir / 'careful-triage' / 'SKILL.md').write_text(STRONG_SKILL)
    cheap = ScriptedModel([[call('1', 'skill_load', {'name': 'careful-triage'})]])
    strong = ScriptedModel([[say('Sorted, carefully.')]])
    result, events = await run_once(make_runtime(cheap, strong=strong), 'sort my things carefully')

    assert result.answer == 'Sorted, carefully.'
    assert len(cheap.inputs) == 1 and len(strong.inputs) == 1
    # the strong model sees everything so far, including the loaded instructions
    assert '1. Be careful.' in strong.tool_outputs(0)['call_1']
    [upgrade] = of_type(events, ev.MODEL_UPGRADE)
    assert upgrade.payload == {'skill': 'careful-triage', 'model': 'scripted'}
    assert len(of_type(events, ev.TURN_START)) == 2


async def test_without_a_strong_model_nothing_switches(make_runtime, home):
    (home.skills_dir / 'careful-triage').mkdir()
    (home.skills_dir / 'careful-triage' / 'SKILL.md').write_text(STRONG_SKILL)
    model = ScriptedModel([[call('1', 'skill_load', {'name': 'careful-triage'})], [say('Done.')]])
    result, events = await run_once(make_runtime(model), 'sort my things carefully')
    assert result.answer == 'Done.' and of_type(events, ev.MODEL_UPGRADE) == []
