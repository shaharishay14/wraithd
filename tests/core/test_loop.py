"""
The harness around real runs: the Agents SDK Runner with a scripted model. Multi-step tool use, errors going
back to the model, the step limit and stop conditions ending in an honest answer, the result cap, and the
event timeline every run leaves in the store.
"""

import json
from datetime import UTC, datetime

import pytest
from agents import Agent

from local_agent.core import events as ev
from local_agent.core import gate as gate_module
from local_agent.core import loop, render
from local_agent.core.gate import PermissionGate, guard_function_tools
from tests.scripted import ScriptedModel, call, fake_tool, run_agent, say

WEATHER = fake_tool('get_weather', lambda **args: {'tool': 'get_weather', **args})
CALCULATE = fake_tool('calculate', lambda **args: {'tool': 'calculate', **args})


def broken(**args):
    raise ConnectionError('weather service down')


async def test_multi_step_task_runs_tools_until_the_model_answers(store, events):
    model = ScriptedModel([
        [call('1', 'get_weather', {'location': 'Paris'})],
        [call('2', 'calculate', {'expression': '18 * 9 / 5 + 32'})],
        [say('It is 64.4F in Paris')],
    ])
    answer, _ = await run_agent(model, [WEATHER, CALCULATE], 'Paris weather in Fahrenheit?', store=store,
                                callback=events)
    assert answer == 'It is 64.4F in Paris'
    # the answer was streamed to the channel in pieces, in order
    deltas = [data for event, data in events.recorded if event == 'content_delta']
    assert len(deltas) > 1 and ''.join(d['content'] for d in deltas) == 'It is 64.4F in Paris'
    assert [d['index'] for d in deltas] == list(range(len(deltas)))
    assert model.tool_names[0] == ['calculate', 'get_weather']
    assert 'Paris' in model.tool_outputs(1)['call_1']  # step 2 saw step 1's result
    assert '18 * 9' in model.tool_outputs(2)['call_2']


async def test_a_run_leaves_a_full_event_timeline(store):
    model = ScriptedModel([[call('1', 'get_weather', {'location': 'Paris'})], [say('Sunny in Paris')]])
    answer, log = await run_agent(model, [WEATHER], 'weather in Paris?', store=store)
    assert answer == 'Sunny in Paris'

    events = store.run_events(log.run_id)
    assert [e.type for e in events] == [
        ev.RUN_START, ev.CONTEXT_BUILT,
        ev.TURN_START, ev.MODEL_OUTPUT, ev.TOOL_CALL, ev.TOOL_RESULT,
        ev.TURN_START, ev.MODEL_OUTPUT,
        ev.RUN_END,
    ]
    by_type = {e.type: e.payload for e in events}
    assert by_type[ev.TOOL_CALL] == {'tool': 'get_weather', 'arguments': {'location': 'Paris'}, 'access': 'read',
                                     'allowed': True}
    assert by_type[ev.TOOL_RESULT]['outcome'] == 'ok'
    assert by_type[ev.MODEL_OUTPUT]['text_chars'] == len('Sunny in Paris')
    assert by_type[ev.RUN_END]['turns'] == 2 and by_type[ev.RUN_END]['stop_reason'] is None


async def test_tool_crash_goes_back_to_the_model_as_an_error(store):
    def flaky(**args):
        if args.get('location') == 'Atlantis':
            raise ValueError('Unknown place Atlantis')
        return {'location': args.get('location')}

    model = ScriptedModel([
        [call('1', 'get_weather', {'location': 'Atlantis'})],
        [call('2', 'get_weather', {'location': 'Athens'})],
        [say('Athens is sunny')],
    ])
    answer, _ = await run_agent(model, [fake_tool('get_weather', flaky)], 'weather in Atlantis?', store=store)
    assert answer == 'Athens is sunny'
    assert 'Unknown place Atlantis' in model.tool_outputs(1)['call_1']


async def test_three_tool_errors_in_a_row_stop_the_run_with_a_reason(store):
    model = ScriptedModel([[call(str(i), 'get_weather', {'location': city})]
                           for i, city in enumerate(['Paris', 'Lyon', 'Nice'])]
                          + [[say('The weather service is down, so I could not check.')]])

    answer, log = await run_agent(model, [fake_tool('get_weather', broken)], 'weather in four cities?', store=store)

    assert answer == 'The weather service is down, so I could not check.'
    # three failed calls, then the stop: one more model call, with no tools, to answer
    assert len(model.inputs) == 4 and model.tool_names[-1] == []
    final_instruction = model.inputs[-1][-1]
    assert final_instruction['role'] == 'system' and 'last 3 tool calls failed' in final_instruction['content']
    events = store.run_events(log.run_id)
    assert [e.payload['outcome'] for e in events if e.type == ev.TOOL_RESULT] == ['error'] * 3
    assert next(e for e in events if e.type == ev.STOP).payload['reason'] == 'tool_errors'
    assert events[-1].payload['stop_reason'] == 'tool_errors'


async def test_the_same_call_three_times_is_a_loop(store):
    model = ScriptedModel([[call(str(i), 'calculate', {'expression': '1+1'})] for i in range(3)] + [[say('It is 2.')]])
    answer, log = await run_agent(model, [fake_tool('calculate', lambda **a: 2)], 'what is 1+1?', store=store)
    assert answer == 'It is 2.'
    assert len(model.inputs) == 4 and model.tool_names[-1] == []
    assert 'calculate was called 3 times' in model.inputs[-1][-1]['content']
    assert store.run_events(log.run_id)[-1].payload['stop_reason'] == 'repeated_call'


async def test_step_limit_forces_an_honest_final_answer(store):
    steps = loop.MAX_AGENT_STEPS
    model = ScriptedModel([[call(str(i), 'calculate', {'expression': f'{i}+1'})] for i in range(steps)]
                          + [[say('I could not finish: still calculating')]])

    answer, log = await run_agent(model, [fake_tool('calculate', lambda **a: 2)], 'loop forever', store=store)
    assert answer == 'I could not finish: still calculating'
    assert len(model.inputs) == steps + 1
    assert model.tool_names[-1] == []  # the last call has no tools
    final_input = model.inputs[-1]
    assert final_input[-1] == {'role': 'system', 'content': loop.STEP_LIMIT_INSTRUCTION}
    # ...and it still has everything the agent did, so it can't pretend
    assert sum(1 for item in final_input if isinstance(item, dict) and item.get('type') == 'function_call_output') \
        == steps
    assert log.of_type(ev.STOP)[0].payload['reason'] == 'max_turns'


async def test_big_tool_result_is_capped_and_marked(store):
    model = ScriptedModel([[call('1', 'web_search', {'query': 'x'})], [say('done')]])
    await run_agent(model, [fake_tool('web_search', lambda **a: 'x' * 100_000)], 'search', store=store)
    content = model.tool_outputs(1)['call_1']
    assert 'TRUNCATED' in content and len(content) < render.MAX_TOOL_RESULT_CHARS + 1200
    assert content.startswith('<untrusted_tool_output tool="web_search">')


async def test_plain_chat_streams_through_the_same_runner(store, events):
    model = ScriptedModel([[say('Hello! How can I help?')]])
    answer, _ = await run_agent(model, [], 'hi', store=store, callback=events)
    assert answer == 'Hello! How can I help?'
    assert model.tool_names == [[]]
    assert ''.join(d['content'] for e, d in events.recorded if e == 'content_delta') == 'Hello! How can I help?'


async def test_a_blocked_tool_is_logged_as_denied(store, monkeypatch):
    monkeypatch.setattr(gate_module, 'BUILTIN_TOOL_ACCESS', {**gate_module.BUILTIN_TOOL_ACCESS,
                                                             'calculate': gate_module.ToolAccess.DENY})
    model = ScriptedModel([[call('1', 'calculate', {'expression': '1+1'})], [say('I am not allowed to calculate.')]])
    _, log = await run_agent(model, [CALCULATE], 'what is 1+1?', store=store)
    tool_call, tool_result = log.of_type(ev.TOOL_CALL)[0], log.of_type(ev.TOOL_RESULT)[0]
    assert tool_call.payload['allowed'] is False and tool_call.payload['access'] == 'deny'
    assert tool_result.payload['outcome'] == 'denied'


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


async def test_failure_before_anything_happened_can_be_retried(store):
    # The caller's retry (Phase 2) relies on this: nothing ran, so the original error comes through untouched
    model = ScriptedModel([ConnectionError('blip')])
    with pytest.raises(ConnectionError):
        await loop.run_with_step_limit(Agent(name='t', instructions='', tools=[], model=model),
                                       [{'role': 'user', 'content': 'hi'}])


async def test_events_are_saved_even_when_the_run_fails(store):
    model = ScriptedModel([[call('1', 'get_weather', {'location': 'Paris'})], ConnectionError('blip')])
    with pytest.raises(loop.RunAlreadyStartedError):
        await run_agent(model, [WEATHER], 'weather?', store=store)
    saved = store.run_events(store.latest_run(source='chat', since=datetime(2000, 1, 1, tzinfo=UTC)))
    assert [e.type for e in saved][-2:] == [ev.TOOL_RESULT, ev.TURN_START]
    assert json.loads(json.dumps(saved[0].payload))['message'] == 'weather?'

