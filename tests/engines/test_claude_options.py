"""[engine] in config.toml, claude_options() (the one place Claude Code's options are built) and login_problem()."""

import importlib.util

import pytest

from wraithd.config import SUBSCRIPTION_DAILY_TOKENS, ConfigError, load_config, set_values
from wraithd.engines import claude_code
from wraithd.engines.claude_code import claude_options, login_problem

SAFE = dict(tools=[], strict_mcp_config=True, setting_sources=[], env={'ANTHROPIC_API_KEY': ''},
            extra_args={'no-session-persistence': None})


def safe_fields(options):
    return {name: getattr(options, name) for name in SAFE}


# --- [engine] -----------------------------------------------------------------------------------------------------

def test_engine_defaults_to_the_agents_sdk_with_cheap_background_model(home):
    config = load_config(home)
    assert config.section('engine') == {'name': 'agents-sdk', 'fallback': False, 'chat_model': 'sonnet',
                                        'background_model': 'haiku'}
    assert config.daily_tokens is None  # off unless set, as before


def test_subscription_mode_turns_the_daily_budget_on_unless_set(home):
    set_values(home, 'engine', {'name': 'claude-code'})
    assert load_config(home).daily_tokens == SUBSCRIPTION_DAILY_TOKENS
    set_values(home, 'budget', {'daily_tokens': 5000})
    assert load_config(home).daily_tokens == 5000


@pytest.mark.parametrize('values', [{'name': 'claude'}, {'chat_model': ' '}, {'fallback': 'yes'}])
def test_bad_engine_settings_are_refused(home, values):
    with pytest.raises(ConfigError, match=r'\[engine\]'):
        set_values(home, 'engine', values)


@pytest.mark.parametrize('purpose, source, expected', [
    ('chat', 'chat', 'sonnet'), ('chat', 'telegram', 'sonnet'), ('subagent', 'subagent', 'sonnet'),
    ('heartbeat', 'heartbeat', 'haiku'), ('chat', 'schedule', 'haiku'), ('distill', '', 'haiku'),
    ('compaction', '', 'haiku')])
def test_each_purpose_picks_its_model(home, purpose, source, expected):
    assert claude_code.model_for(load_config(home), purpose, source) == expected


def test_the_models_are_the_owners_to_set(home):
    set_values(home, 'engine', {'chat_model': 'opus', 'background_model': 'claude-haiku-4-5'})
    config = load_config(home)
    assert claude_code.model_for(config, 'chat') == 'opus'
    assert claude_code.model_for(config, 'heartbeat', 'heartbeat') == 'claude-haiku-4-5'


# --- claude_options() -------------------------------------------------------------------------------------------

def test_options_are_safe_by_default(claude_sdk):
    claude_sdk()
    assert safe_fields(claude_options()) == SAFE


def test_overrides_add_but_never_loosen(claude_sdk):
    claude_sdk()
    options = claude_options(system_prompt='be wraithd', mcp_servers={'wraithd': {}}, model='haiku',
                             allowed_tools=['mcp__wraithd__x'], max_turns=3, include_partial_messages=True)
    assert (options.system_prompt, options.model, options.max_turns) == ('be wraithd', 'haiku', 3)
    assert options.allowed_tools == ['mcp__wraithd__x'] and options.include_partial_messages
    assert safe_fields(options) == SAFE
    for loosen in ({'tools': ['Bash']}, {'strict_mcp_config': False}, {'setting_sources': ['user']},
                   {'env': {'ANTHROPIC_API_KEY': 'sk-ant'}}, {'extra_args': {}}, {'permission_mode': 'bypass'},
                   {'cwd': '/'}):
        with pytest.raises(ValueError, match="can't set"):
            claude_options(**loosen)


def test_a_caller_changing_its_options_never_changes_the_next_ones(claude_sdk):
    claude_sdk()
    first = claude_options()
    first.env['ANTHROPIC_API_KEY'] = 'sk-ant-leak'
    first.tools.append('Bash')
    first.extra_args.clear()
    assert safe_fields(claude_options()) == SAFE


def test_without_the_extra_it_says_how_to_install(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, 'claude_agent_sdk', None)  # import fails
    with pytest.raises(claude_code.SubscriptionUnavailable, match=r"wraithd\[claude\]"):
        claude_options()


@pytest.mark.skipif(importlib.util.find_spec('claude_agent_sdk') is None, reason='the claude extra is not installed')
def test_the_real_sdk_takes_these_options():
    import claude_agent_sdk

    options = claude_options(system_prompt='x', model='haiku', max_turns=1)
    assert isinstance(options, claude_agent_sdk.ClaudeAgentOptions)
    assert safe_fields(options) == SAFE


# --- login_problem() --------------------------------------------------------------------------------------------

def test_the_subscription_login_is_no_problem():
    assert login_problem({'apiKeySource': 'none'}, {}) is None


@pytest.mark.parametrize('source', ['ANTHROPIC_API_KEY', 'apiKeyHelper', '/login managed key'])
def test_any_other_credential_is_a_problem(source):
    assert source in login_problem({'apiKeySource': source}, {})


def test_no_source_is_a_problem():
    assert 'did not say' in login_problem({}, {})


@pytest.mark.parametrize('variable', ['CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX'])
def test_bedrock_and_vertex_also_say_none_but_are_not_the_login(variable):
    assert variable in login_problem({'apiKeySource': 'none'}, {variable: '1'})
    assert login_problem({'apiKeySource': 'none'}, {variable: '0'}) is None
