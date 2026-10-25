"""
The state folder, the config file, keys in the keychain, and the providers they turn into.
"""

import os
import stat

import pytest
from agents import OpenAIChatCompletionsModel, OpenAIResponsesModel

from local_agent import config, providers
from local_agent.config import ConfigError, Home, load_config


def test_the_home_comes_from_the_environment_and_is_created_private(tmp_path):
    home = Home.locate()
    assert home.root == tmp_path / 'home'  # tests never see the real ~/.local-agent
    home.ensure()
    assert stat.S_IMODE(os.stat(home.root).st_mode) == 0o700
    assert home.config_path.read_text().startswith('# Local Agent settings')
    assert home.agents_md.exists() and home.memory_dir.is_dir() and home.skills_dir.is_dir()
    home.agents_md.write_text('mine')
    home.ensure()
    assert home.agents_md.read_text() == 'mine'  # never overwritten


def test_the_default_config_loads(home):
    loaded = load_config(home)
    assert (loaded.model.provider, loaded.model.default, loaded.model.strong) == ('openai', 'gpt-4o-mini', 'gpt-4o')
    assert loaded.run_tokens == 200_000 and loaded.run_tool_calls == 40 and loaded.daily_tokens is None
    assert 'answer_strangers = false' in home.config_path.read_text() and loaded.answer_strangers is False


@pytest.mark.parametrize('text, problem', [
    ('[model]\nprovider = "gemini"\n', 'provider must be one of'),
    ('[model]\nprovider = "openai-compatible"\n', 'base_url is required'),
    ('[budget]\nrun_tokens = -5\n', 'positive whole number'),
    ('[budget\n', 'not valid TOML'),
    ('[telegram]\nanswer_strangers = "yes"\n', 'answer_strangers must be true or false'),
])
def test_a_bad_config_says_what_to_fix(home, text, problem):
    home.config_path.write_text(text)
    with pytest.raises(ConfigError, match=problem):
        load_config(home)


def test_set_values_keeps_comments_uncomments_examples_and_adds_sections(home):
    config.set_values(home, 'model', {'provider': 'openai-compatible', 'base_url': 'http://localhost:8000/v1'})
    config.set_values(home, 'owner', {'timezone': 'Europe/London'})
    config.set_values(home, 'telegram', {'answer_strangers': True})
    config.set_values(home, 'budget', {'daily_tokens': 500000, 'run_tokens': 1000})
    text = home.config_path.read_text()
    assert 'provider = "openai-compatible" # openai | anthropic | ollama | openai-compatible' in text
    assert 'base_url = "http://localhost:8000/v1"\n' in text and '# base_url' not in text
    assert 'timezone = "Europe/London"\n' in text and '# email = "you@example.com"' in text
    assert 'run_tokens = 1000            # per run, input + output' in text  # the comment stays in its column
    loaded = load_config(home)
    assert (loaded.model.base_url, loaded.timezone, loaded.answer_strangers, loaded.daily_tokens, loaded.run_tokens) \
        == ('http://localhost:8000/v1', 'Europe/London', True, 500000, 1000)


def test_set_values_that_break_the_config_leave_it_as_it_was(home):
    before = home.config_path.read_text()
    with pytest.raises(ConfigError, match='provider must be one of'):
        config.set_values(home, 'model', {'provider': 'gemini'})
    assert home.config_path.read_text() == before


def test_keys_live_in_the_keychain_not_the_config(home, isolated):
    config.set_key('openai', 'sk-test-key-in-keychain')
    assert isolated.passwords == {('local-agent', 'openai'): 'sk-test-key-in-keychain'}
    assert config.get_key('openai') == 'sk-test-key-in-keychain'
    assert 'sk-test' not in home.config_path.read_text()


def test_an_environment_key_is_the_fallback(monkeypatch):
    assert config.get_key('anthropic') is None
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'env-anthropic-key-123')
    assert config.get_key('anthropic') == 'env-anthropic-key-123'


def test_openai_uses_the_responses_api_with_the_keychain_key(home):
    config.set_key('openai', 'sk-test-key-in-keychain')
    models = providers.build_models(load_config(home))
    assert set(models) == {'default', 'strong'}
    assert isinstance(models['default'], OpenAIResponsesModel) and models['default'].model == 'gpt-4o-mini'
    assert models['strong'].model == 'gpt-4o'


def test_a_missing_key_says_how_to_set_it(home):
    with pytest.raises(providers.ProviderError, match='local-agent key set openai'):
        providers.build_models(load_config(home))


def test_ollama_needs_no_key_and_has_no_strong_model_unless_set(home):
    home.config_path.write_text('[model]\nprovider = "ollama"\ndefault = "llama3.2"\nstrong = ""\n')
    models = providers.build_models(load_config(home))
    assert list(models) == ['default'] and isinstance(models['default'], OpenAIChatCompletionsModel)
    assert models['default'].model == 'llama3.2'
    assert str(models['default']._client.base_url).startswith('http://localhost:11434/v1')


def test_anthropic_goes_through_litellm_without_downloads_or_telemetry(home, monkeypatch):
    monkeypatch.delenv('LITELLM_LOCAL_MODEL_COST_MAP', raising=False)
    home.config_path.write_text('[model]\nprovider = "anthropic"\ndefault = "claude-haiku-5-5"\n'
                                'strong = "claude-sonnet-5-5"\n')
    config.set_key('anthropic', 'sk-ant-test-key-1234567890')
    models = providers.build_models(load_config(home))
    import litellm
    assert models['default'].model == 'anthropic/claude-haiku-5-5'
    assert models['strong'].model == 'anthropic/claude-sonnet-5-5'
    assert os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] == 'True' and litellm.telemetry is False
