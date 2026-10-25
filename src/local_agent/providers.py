"""
Model providers: turn the config into Agents SDK models. The SDK runs the loop the same way for all of them.

- openai: the Responses API.
- ollama and openai-compatible: Chat Completions against a local or self-hosted endpoint.
- anthropic: through LiteLLM (the `anthropic` extra), imported only when it's the configured provider, since
  it's large and slow to import.
"""

import os

from agents import Model, OpenAIChatCompletionsModel, OpenAIResponsesModel
from openai import AsyncOpenAI

from local_agent.config import Config, get_key

OLLAMA_URL = 'http://localhost:11434/v1'


class ProviderError(Exception):
    """The model can't be set up. The message says what to do."""


def _key(provider: str, required: bool) -> str | None:
    key = get_key(provider)
    if required and not key:
        raise ProviderError(f'No API key for {provider}. Set one with: local-agent key set {provider}')
    return key


def _litellm_model(name: str, key: str) -> Model:
    # LiteLLM downloads a price list on import unless told to use its bundled copy, and has telemetry on by
    # default. Neither belongs in a local agent.
    os.environ.setdefault('LITELLM_LOCAL_MODEL_COST_MAP', 'True')
    try:
        import litellm
        from agents.extensions.models.litellm_model import LitellmModel
    except ImportError:
        raise ProviderError('Anthropic models need the anthropic extra: uv tool install "local-agent[anthropic]"') \
            from None
    litellm.telemetry = False
    return LitellmModel(model=f'anthropic/{name}', api_key=key)


def build_model(config: Config, name: str) -> Model:
    """An SDK model for one model name of the configured provider."""
    provider = config.model.provider
    if provider == 'openai':
        client = AsyncOpenAI(api_key=_key('openai', required=True))
        return OpenAIResponsesModel(model=name, openai_client=client)
    if provider == 'anthropic':
        return _litellm_model(name, _key('anthropic', required=True))
    # Ollama ignores the key; a self-hosted endpoint may want one
    key = _key(provider, required=False) or 'local'
    base_url = config.model.base_url or OLLAMA_URL
    return OpenAIChatCompletionsModel(model=name, openai_client=AsyncOpenAI(api_key=key, base_url=base_url))


def build_models(config: Config) -> dict[str, Model]:
    """{'default': ..., 'strong': ...}. Without a separate strong model, there's nothing to switch to."""
    models = {'default': build_model(config, config.model.default)}
    if config.model.strong and config.model.strong != config.model.default:
        models['strong'] = build_model(config, config.model.strong)
    return models
