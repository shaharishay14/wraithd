"""E1: where a run's input tokens go, from its event log."""

import json
from pathlib import Path

import pytest

from local_agent import cli
from local_agent.core import events as ev
from local_agent.core import replay, tokens, views
from local_agent.store import SQLiteStore
from tests.efficiency import scenarios
from tests.efficiency.fakes import CountingModel
from tests.scripted import ScriptedModel, of_type, run_once, say

RUNS = sorted((Path(__file__).parent / 'runs').glob('*.json'))
# What the parts can't account for: each message's and each tool's framing in the provider's format. The meter
# counts the content; a provider adds a few tokens around each message and tool.
NOISE = 0.07


def test_there_are_recorded_runs():
    assert len(RUNS) >= 2


@pytest.mark.parametrize('path', RUNS, ids=lambda path: path.stem)
def test_the_breakdown_adds_up_to_the_reported_input_tokens(path):
    found = tokens.breakdown(replay.load(path))
    assert found['calls'] and found['reported'] > 0
    for call in found['calls']:
        assert sum(call['parts'].values()) == call['measured']
        assert call['measured'] <= call['reported'] <= call['measured'] * (1 + NOISE)
    assert found['measured'] <= found['reported'] <= found['measured'] * (1 + NOISE)


async def test_a_live_run_measures_every_part(make_runtime):
    model, rt, _, events = await scenarios.a_tool_run(make_runtime)
    first, second = [event.payload for event in of_type(events, ev.CONTEXT_TOKENS)]
    for part in (tokens.TOOLS, tokens.SYSTEM, tokens.AGENTS_MD, tokens.SKILLS, tokens.PROFILE, tokens.HISTORY,
                 tokens.NOTES):
        assert first['parts'][part] > 0, part
    assert first['parts'][tokens.TOOL_RESULTS] == 0 and second['parts'][tokens.TOOL_RESULTS] > 500
    assert [first['input_tokens'], second['input_tokens']] == model.reported
    assert first['tokenizer'] == 'o200k_base' and first['model'] == 'scripted'
    # AGENTS.md and the profile are counted as sent, and not again as "system prompt"
    assert first['parts'][tokens.AGENTS_MD] == tokens.count(
        f'\nSTANDING ORDERS (from the user, in every conversation):\n{scenarios.AGENTS_MD.strip()}\n')


async def test_history_and_the_owners_message_count_as_history(make_runtime):
    _, _, _, events = await scenarios.a_conversation_run(make_runtime)
    [call] = of_type(events, ev.CONTEXT_TOKENS)
    expected = sum(tokens.count(text) for text in ('What is the weather in Lisbon today?',
                                                   'Sunny, 22C. A light breeze from the north west.',
                                                   'What did I ask you about?'))
    assert call.payload['parts'][tokens.HISTORY] == expected


async def test_the_meter_does_not_change_what_the_model_gets(make_runtime):
    model = ScriptedModel([[say('hi')]])
    result, _ = await run_once(make_runtime(model), 'hello', owner=False, builtins=False)
    assert result.answer == 'hi' and model.inputs[0][0] == {'role': 'user', 'content': 'hello'}


async def test_a_measuring_failure_never_fails_the_call(make_runtime, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError('tokenizer exploded')
    monkeypatch.setattr(tokens, 'measure', broken)
    result, events = await run_once(make_runtime(CountingModel([[say('fine')]])), 'hello')
    assert result.answer == 'fine'
    [call] = of_type(events, ev.CONTEXT_TOKENS)
    assert 'parts' not in call.payload and call.payload['input_tokens'] > 0


async def test_cache_tokens_are_recorded_and_budgets_count_input_once(make_runtime):
    _, _, _, events = await scenarios.a_tool_run(make_runtime, cache_read=1200, cache_write=300)
    calls = [event.payload for event in of_type(events, ev.CONTEXT_TOKENS)]
    assert [(c['cache_read'], c['cache_write']) for c in calls] == [(1200, 300), (1200, 300)]
    reported = [event.payload for event in of_type(events, ev.MODEL_OUTPUT)]
    # The budget is input + output as reported: cache reads and writes are inside input_tokens, never added again
    assert views.tokens_used(events) == sum(r['input_tokens'] + r['output_tokens'] for r in reported)
    assert of_type(events, ev.RUN_END)[0].payload['tokens'] == views.tokens_used(events)


def test_a_token_count_without_a_tokenizer_is_an_estimate(monkeypatch):
    monkeypatch.setattr(tokens, '_encoder', False)
    assert tokens.count('x' * 40) == 10 and tokens.tokenizer_name() == 'chars/4'


def test_runs_tokens_prints_the_breakdown(home, capsys):
    store = SQLiteStore(home.db_path)
    log = ev.EventLog(store, run_id='abcd1234-run')
    for event in replay.load(RUNS[-1]):
        log.append(event.type, **event.payload)
    log.save()
    store.close()
    assert cli.main(['runs', 'tokens', 'abcd']) == 0
    text = capsys.readouterr().out
    assert 'input tokens reported' in text and 'AGENTS.md' in text and 'tool list' in text
    assert 'prefix: the same on every call' in text

    assert cli.main(['runs', 'tokens', 'abcd', '--json']) == 0
    found = json.loads(capsys.readouterr().out)
    assert found['run_id'] == 'abcd1234-run' and found['reported'] == sum(c['reported'] for c in found['calls'])

    assert cli.main(['runs', 'tokens', 'nope']) == 1
    assert "No run matches 'nope'" in capsys.readouterr().err


def test_a_run_from_before_metering_says_so():
    assert 'no measured model calls' in tokens.render_text('old', tokens.breakdown([]))
