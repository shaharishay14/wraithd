"""[engine] in config.toml, claude_options() (the one place Claude Code's options are built), model_for() and
subscription_env_problem()."""

import importlib.util
import sys

import pytest

from wraithd.config import SUBSCRIPTION_DAILY_TOKENS, ConfigError, load_config, set_values
from wraithd.core.loop import MAX_AGENT_STEPS
from wraithd.engines.claude_options import SubscriptionUnavailable, claude_options, model_for, subscription_env_problem

SAFE = dict(tools=[], strict_mcp_config=True, setting_sources=[], env={'ANTHROPIC_API_KEY': ''},
            extra_args={'no-session-persistence': None})


def safe_fields(options):
    return {name: getattr(options, name) for name in SAFE}


# --- [engine] -----------------------------------------------------------------------------------------------------

def test_engine_defaults(home):
    config = load_config(home)
    assert config.section('engine') == {'name': 'agents-sdk', 'chat_model': 'sonnet', 'background_model': 'haiku',
                                        'schedule_model': '', 'fallback': False, 'idle_minutes': 10}
    assert config.daily_tokens is None  # off unless set, as before


def test_subscription_mode_turns_the_daily_budget_on_unless_set(home):
    set_values(home, 'engine', {'name': 'claude-code'})
    assert load_config(home).daily_tokens == SUBSCRIPTION_DAILY_TOKENS
    set_values(home, 'budget', {'daily_tokens': 5000})
    assert load_config(home).daily_tokens == 5000


@pytest.mark.parametrize('values', [{'name': 'claude'}, {'chat_model': ' '}, {'fallback': 'yes'},
                                    {'idle_minutes': 0}])
def test_bad_engine_settings_are_refused(home, values):
    with pytest.raises(ConfigError, match=r'\[engine\]'):
        set_values(home, 'engine', values)


@pytest.mark.parametrize('purpose, expected', [
    ('chat', 'sonnet'), ('strong', 'sonnet'), ('subagent', 'sonnet'),
    ('heartbeat', 'haiku'), ('schedule', 'haiku'), ('distill', 'haiku'), ('compaction', 'haiku')])
def test_each_purpose_picks_its_model(home, purpose, expected):
    assert model_for(load_config(home).section('engine'), purpose) == expected


def test_the_models_are_the_owners_to_set(home):
    set_values(home, 'engine', {'chat_model': 'opus', 'background_model': 'claude-haiku-4-5'})
    engine = load_config(home).section('engine')
    assert (model_for(engine, 'chat'), model_for(engine, 'heartbeat')) == ('opus', 'claude-haiku-4-5')


def test_schedule_model_moves_only_scheduled_runs(home):
    set_values(home, 'engine', {'schedule_model': 'sonnet'})
    engine = load_config(home).section('engine')
    assert model_for(engine, 'schedule') == 'sonnet'
    assert [model_for(engine, p) for p in ('heartbeat', 'distill', 'compaction')] == ['haiku'] * 3


# --- claude_options() -------------------------------------------------------------------------------------------

def test_options_are_safe_and_name_the_model(claude_sdk):
    claude_sdk()
    options = claude_options('be wraithd', {'type': 'sdk'}, ['mcp__wraithd__x'], 'haiku', stream=True)
    assert safe_fields(options) == SAFE
    assert options.system_prompt == 'be wraithd' and options.mcp_servers == {'wraithd': {'type': 'sdk'}}
    assert options.allowed_tools == ['mcp__wraithd__x'] and options.model == 'haiku'
    assert options.include_partial_messages and options.max_turns == MAX_AGENT_STEPS


def test_a_tool_less_call_has_no_servers(claude_sdk):
    claude_sdk()
    options = claude_options('x', None, [], 'haiku')
    assert options.mcp_servers == {} and options.allowed_tools == [] and not options.include_partial_messages
    assert safe_fields(options) == SAFE


def test_a_model_is_always_set(claude_sdk):
    claude_sdk()
    for missing in ('', None):
        with pytest.raises(ValueError, match='Opus'):
            claude_options('x', None, [], missing)


def test_there_is_no_way_to_loosen_the_safe_fields(claude_sdk):
    claude_sdk()
    with pytest.raises(TypeError):
        claude_options('x', None, [], 'haiku', tools=['Bash'])


def test_a_caller_changing_its_options_never_changes_the_next_ones(claude_sdk):
    claude_sdk()
    first = claude_options('x', None, [], 'haiku')
    first.env['ANTHROPIC_API_KEY'] = 'sk-ant-leak'
    first.tools.append('Bash')
    first.extra_args.clear()
    assert safe_fields(claude_options('x', None, [], 'haiku')) == SAFE


def test_without_the_extra_it_says_how_to_install(monkeypatch):
    monkeypatch.setitem(sys.modules, 'claude_agent_sdk', None)  # the import fails
    with pytest.raises(SubscriptionUnavailable, match=r"wraithd\[claude\]"):
        claude_options('x', None, [], 'haiku')


@pytest.mark.skipif(importlib.util.find_spec('claude_agent_sdk') is None, reason='the claude extra is not installed')
def test_the_real_sdk_takes_these_options():
    import claude_agent_sdk

    options = claude_options('x', None, [], 'haiku')
    assert isinstance(options, claude_agent_sdk.ClaudeAgentOptions)
    assert safe_fields(options) == SAFE


# --- subscription_env_problem() -------------------------------------------------------------------------------

def test_a_clean_environment_is_no_problem(monkeypatch):
    for variable in ('CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY'):
        monkeypatch.delenv(variable, raising=False)
    assert subscription_env_problem() is None


@pytest.mark.parametrize('variable', ['CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY'])
def test_bedrock_vertex_and_foundry_are_refused(monkeypatch, variable):
    monkeypatch.setenv(variable, '1')
    assert variable in subscription_env_problem()
    monkeypatch.setenv(variable, '0')
    assert subscription_env_problem() is None
