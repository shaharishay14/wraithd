"""
Model providers: turn the config into Agents SDK models. The SDK runs the loop the same way for all of them.

- openai: the Responses API.
- ollama and openai-compatible: Chat Completions against a local or self-hosted endpoint.
- anthropic: through LiteLLM, imported only when it's the configured provider, since it's large and slow to
  import.
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
    import litellm
    from agents.extensions.models.litellm_model import LitellmModel

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


def can_see_images(config: Config) -> bool:
    """
    Whether the default model takes image input, from LiteLLM's model list. A model the list doesn't know is
    assumed to see for openai and anthropic (their current models all do) and not to for local ones (most Ollama
    models can't).
    """
    provider, name = config.model.provider, config.model.default
    os.environ.setdefault('LITELLM_LOCAL_MODEL_COST_MAP', 'True')
    import litellm

    litellm.telemetry = False
    litellm.suppress_debug_info = True  # it prints a banner for a model it doesn't know
    lookup = {'openai': 'openai', 'anthropic': 'anthropic', 'ollama': 'ollama'}.get(provider)
    try:
        info = litellm.get_model_info(model=name, custom_llm_provider=lookup)
    except Exception:  # not in the list
        return provider in ('openai', 'anthropic')
    return bool(info.get('supports_vision'))
