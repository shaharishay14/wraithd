"""
E2: the stable prefix (tool list + system prompt) is byte-identical on every turn and every run of a conversation;
Anthropic gets cache breakpoints through the Agents SDK -> LiteLLM path; cache reads and writes reach the events.
"""

import json
from datetime import datetime

import httpx
import pytest

from local_agent import run as run_module
from local_agent.config import ModelConfig, set_key
from local_agent.core import events as ev
from local_agent.core import tokens, views
from local_agent.providers import CACHE_POINTS, build_models, model_settings
from local_agent.run import Runtime
from tests.efficiency import scenarios
from tests.efficiency.fakes import CountingModel
from tests.scripted import call, fake_tool, of_type, run_once, say


def at(hour, minute):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 29, hour, minute).astimezone(tz)
    return Clock


async def test_the_prefix_is_the_same_on_every_turn_and_every_run(make_runtime, monkeypatch):
    weather = fake_tool('get_weather', lambda **a: {'temp': 21})
    monkeypatch.setattr(run_module, 'datetime', at(9, 0))
    first = CountingModel([[call('1', 'get_weather', {'location': 'Lisbon'})], [say('Sunny, 21C.')]])
    rt = make_runtime(first)
    scenarios.owner_setup(rt)
    _, events_1 = await run_once(rt, 'Weather in Lisbon?', tools=[weather])

    # The next message of the same conversation, an hour and a quarter later
    monkeypatch.setattr(run_module, 'datetime', at(10, 15))
    second = CountingModel([[say('You asked about Lisbon.')]])
    history = [{'role': 'user', 'content': 'Weather in Lisbon?'}, {'role': 'assistant', 'content': 'Sunny, 21C.'}]
    _, events_2 = await run_once(make_runtime(second), 'What did I ask?', history=history, tools=[weather])

    # Byte for byte: the system prompt on every call, and the same tools in the same order
    assert first.instructions[0] == first.instructions[1] == second.instructions[0]
    assert first.tool_names[0] == first.tool_names[1] == second.tool_names[0]
    hashes = [e.payload['prefix_hash'] for e in of_type(events_1 + events_2, ev.CONTEXT_TOKENS)]
    built = [e.payload['prefix_hash'] for e in of_type(events_1 + events_2, ev.CONTEXT_BUILT)]
    assert len(set(hashes + built)) == 1

    # The time is after the owner's message, never in the prefix
    assert '09:00' not in first.instructions[0] and 'Now:' not in first.instructions[0]
    owner_message = first.inputs[0].index({'role': 'user', 'content': 'Weather in Lisbon?'})
    assert first.inputs[0][owner_message + 1] == {'role': 'system',
                                                   'content': 'Now: Thursday 29 October 2026, 09:00 '
                                                              f'({datetime(2026, 10, 29, 9).astimezone().tzname()}).'}
    assert any('10:15' in str(item.get('content')) for item in second.inputs[0])


async def test_the_prefix_follows_the_agreed_order(make_runtime):
    model, *_ = await scenarios.a_tool_run(make_runtime)
    system = model.instructions[0]
    order = [system.index(marker) for marker in ('You are Local Agent', 'The app may add short notes',
                                                 'STANDING ORDERS', 'SKILLS (', 'MEMORY (')]
    assert order == sorted(order)


async def test_a_memory_change_moves_only_the_end_of_the_prefix(make_runtime):
    model, rt, *_ = await scenarios.a_tool_run(make_runtime)
    rt.memory.save(scenarios.Note(path='people/ana.md', title='Ana', body='Ana is my sister.'))
    after = CountingModel([[say('ok')]])
    await run_once(make_runtime(after), 'hi')
    before, now = model.instructions[0], after.instructions[0]
    assert before != now and before.split('\nMEMORY (')[0] == now.split('\nMEMORY (')[0]


def config(provider):
    return run_module.Config(model=ModelConfig(provider=provider, default='claude-sonnet-5-5', strong=''))


def test_only_anthropic_gets_cache_breakpoints():
    assert model_settings(config('anthropic')).extra_args == {'cache_control_injection_points': CACHE_POINTS}
    for provider in ('openai', 'ollama', 'openai-compatible'):
        assert model_settings(config(provider)).extra_args is None
    assert all(model_settings(config(p)).include_usage for p in ('anthropic', 'openai', 'ollama'))


# --- Anthropic through the real Agents SDK -> LiteLLM path, with the HTTP request caught (no network) ----------

def anthropic_stream(text, usage, tool=None):
    """An Anthropic streamed answer: text, or a tool call (name, arguments)."""
    if tool:
        block = {'type': 'tool_use', 'id': 'toolu_1', 'name': tool[0], 'input': {}}
        delta = {'type': 'input_json_delta', 'partial_json': json.dumps(tool[1])}
    else:
        block, delta = {'type': 'text', 'text': ''}, {'type': 'text_delta', 'text': text}
    events = [
        {'type': 'message_start', 'message': {'id': 'msg_1', 'type': 'message', 'role': 'assistant',
                                              'model': 'claude-sonnet-5-5', 'content': [], 'stop_reason': None,
                                              'usage': {'output_tokens': 1, **usage}}},
        {'type': 'content_block_start', 'index': 0, 'content_block': block},
        {'type': 'content_block_delta', 'index': 0, 'delta': delta},
        {'type': 'content_block_stop', 'index': 0},
        {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use' if tool else 'end_turn'},
         'usage': {'output_tokens': 7}},
        {'type': 'message_stop'},
    ]
    return ''.join(f'event: {e["type"]}\ndata: {json.dumps(e)}\n\n' for e in events).encode()


class Sent(list):
    tools: list


@pytest.fixture
def anthropic(monkeypatch, home):
    """The Anthropic API, played by a function: every request LiteLLM sends is kept, and answered as a stream."""
    sent = Sent()
    sent.tools = []  # tool calls to answer with, in order, before the text answer
    usage = {'input_tokens': 40, 'cache_creation_input_tokens': 1500, 'cache_read_input_tokens': 300}

    async def send(self, request, **kwargs):
        assert request.url.host == 'api.anthropic.com'
        sent.append(json.loads(request.content))
        tool = sent.tools.pop(0) if sent.tools else None
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, request=request,
                              content=anthropic_stream('Hello Dana.', usage, tool))

    monkeypatch.setattr(httpx.AsyncClient, 'send', send)
    set_key('anthropic', 'sk-ant-test-' + 'x' * 30)
    home.config_path.write_text('[model]\nprovider = "anthropic"\ndefault = "claude-sonnet-5-5"\nstrong = ""\n')
    return sent


async def test_cache_control_survives_the_sdk_and_litellm(anthropic, home):
    rt = Runtime.open(home)
    try:
        result, events = await run_once(rt, 'hello')
    finally:
        rt.close()
    assert result.answer == 'Hello Dana.'
    [body] = anthropic
    # Breakpoint 1: the end of the system prompt (Anthropic caches the tools and the system prompt up to here)
    stable, time_note = body['system']
    assert stable['text'].startswith('You are Local Agent') and stable['cache_control'] == {'type': 'ephemeral'}
    # LiteLLM moves the time note (role system) into Anthropic's system blocks, after the stable one. On a first
    # call it is the last message, so breakpoint 2 lands on it: the cache entry then ends with this run's time,
    # which only this run's next calls read.
    assert time_note['text'].startswith('Now:') and time_note['cache_control'] == {'type': 'ephemeral'}
    assert json.dumps(body).count('cache_control') == 2  # Anthropic takes at most 4
    assert body['tools'] and 'cache_control' not in json.dumps(body['tools'])

    # Usage: LiteLLM counts cache reads and writes inside input_tokens; the events keep them apart
    [call] = of_type(events, ev.CONTEXT_TOKENS)
    assert (call.payload['input_tokens'], call.payload['cache_read'], call.payload['cache_write']) == (1840, 300, 1500)
    [output] = of_type(events, ev.MODEL_OUTPUT)
    assert output.payload['input_tokens'] == 1840
    assert views.tokens_used(events) == 1840 + output.payload['output_tokens']  # each input token counted once


async def test_on_the_next_turn_the_breakpoint_moves_to_the_newest_message(anthropic, home):
    anthropic.tools.append(('get_weather', {'location': 'Lisbon'}))
    rt = Runtime.open(home)
    try:
        result, events = await run_once(rt, 'Weather in Lisbon?',
                                        tools=[fake_tool('get_weather', lambda **a: {'temp': 21})])
    finally:
        rt.close()
    assert result.answer == 'Hello Dana.'
    first, second = anthropic
    assert first['system'][0] == second['system'][0]  # the stable block, byte for byte, marked both times
    assert first['tools'] == second['tools']
    # The time note lost its mark; the tool result, now last, carries it
    assert 'cache_control' not in second['system'][1]
    assert second['messages'][-1]['content'][-1]['type'] == 'tool_result'
    assert second['messages'][-1]['content'][-1]['cache_control'] == {'type': 'ephemeral'}
    assert json.dumps(second).count('cache_control') == 2
    assert [e.payload['cache_read'] for e in of_type(events, ev.CONTEXT_TOKENS)] == [300, 300]


def test_two_purposes_naming_one_model_share_it(home):
    set_key('anthropic', 'sk-ant-test-' + 'x' * 30)
    cfg = run_module.Config(model=ModelConfig(provider='anthropic', default='claude-sonnet-5-5', strong=''),
                            sections={'models': {'compaction': 'claude-sonnet-5-5'}})
    models = build_models(cfg)
    assert models['compaction'] is models['default']
    assert type(models['default']).__name__ == 'CachingLitellmModel'


def test_the_prefix_hash_sees_tools_and_instructions():
    a, b = scenarios.fake_tool('a'), scenarios.fake_tool('b')
    assert tokens.prefix_hash('x', [a, b]) == tokens.prefix_hash('x', [a, b])
    assert tokens.prefix_hash('x', [a, b]) != tokens.prefix_hash('x', [b, a])
    assert tokens.prefix_hash('x', [a]) != tokens.prefix_hash('y', [a])
