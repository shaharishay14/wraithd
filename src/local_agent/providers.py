"""
Model providers: turn the config into Agents SDK models. The SDK runs the loop the same way for all of them.

- openai: the Responses API.
- ollama and openai-compatible: Chat Completions against a local or self-hosted endpoint.
- anthropic: through LiteLLM, imported only when it's the configured provider, since it's large and slow to
  import.

Each purpose (chat, distill, compaction, ...) can have its own model ([models] in config.toml); an unset one uses
the default model. Anthropic gets cache breakpoints on the stable prefix (docs/caching.md); OpenAI caches the
same prefix on its own.
"""

import contextlib
import os

import httpx
from agents import Model, ModelSettings, OpenAIChatCompletionsModel, OpenAIResponsesModel
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

    class CachingLitellmModel(LitellmModel):
        """
        LitellmModel, plus Anthropic's cache writes in the usage. LiteLLM reports them as cache_creation_input_tokens
        and the SDK reads cache_write_tokens, so without this every cache write would show as 0. (LiteLLM already
        counts cache reads and writes inside prompt_tokens, so the budgets see every input token.)
        """

        async def _fetch_response(self, *args, **kwargs):
            response = await super()._fetch_response(*args, **kwargs)
            if isinstance(response, tuple):  # streamed: (Response, chunk stream)
                head, stream = response
                return head, _with_cache_writes(stream)
            _copy_cache_writes(response)
            return response

    return CachingLitellmModel(model=f'anthropic/{name}', api_key=key)


def _copy_cache_writes(chunk) -> None:
    usage = getattr(chunk, 'usage', None)
    details = getattr(usage, 'prompt_tokens_details', None)
    written = getattr(usage, 'cache_creation_input_tokens', None) or getattr(details, 'cache_creation_tokens', None)
    if details is not None and written and not getattr(details, 'cache_write_tokens', None):
        # A usage object that won't take it leaves the count at 0
        with contextlib.suppress(AttributeError, TypeError, ValueError):
            details.cache_write_tokens = written


async def _with_cache_writes(stream):
    async for chunk in stream:
        _copy_cache_writes(chunk)
        yield chunk


# Anthropic cache breakpoints (LiteLLM puts cache_control on these messages): the end of the system prompt, which
# caches the tool list and the system prompt; and the last message, so each turn reads the run so far from cache
CACHE_POINTS = [{'location': 'message', 'role': 'system'}, {'location': 'message', 'index': -1}]


def model_settings(config: Config) -> ModelSettings:
    """
    The settings every run's model calls use. include_usage: streaming through LiteLLM (Anthropic) reports token
    usage only when asked, and the budgets count what's reported. Anthropic caches only what's marked; OpenAI
    caches a stable prefix by itself, and Ollama has nothing to mark.
    """
    if config.model.provider == 'anthropic':
        return ModelSettings(include_usage=True, extra_args={'cache_control_injection_points': CACHE_POINTS})
    return ModelSettings(include_usage=True)


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


# What each model call is for. Every place the harness calls a model asks model_for(purpose).
PURPOSES = ('chat', 'strong', 'distill', 'compaction', 'heartbeat', 'subagent')
# The purposes [models] can give their own model; unset, they use the default one
OWN_MODEL_PURPOSES = ('distill', 'compaction', 'heartbeat', 'subagent')
PURPOSE_MODEL = {'chat': 'default', 'strong': 'strong', 'distill': 'distill', 'compaction': 'compaction',
                 'heartbeat': 'heartbeat', 'subagent': 'subagent'}  # purpose -> key in build_models()


def configured_models(config: Config) -> dict[str, str]:
    """{key in build_models(): model name}, for each model the config names: default, strong, and [models]."""
    names = {'default': config.model.default}
    if config.model.strong and config.model.strong != config.model.default:
        names['strong'] = config.model.strong
    purposes = config.section('models')
    names.update({purpose: purposes[purpose] for purpose in OWN_MODEL_PURPOSES if purposes[purpose]})
    return names


def build_models(config: Config) -> dict[str, Model]:
    """
    {'default': ..., 'strong': ..., and a purpose's own model when [models] sets one}. A missing key means the
    default model (model_for). Without a separate strong model, there's nothing to switch to. Two keys naming the
    same model share one instance.
    """
    built: dict[str, Model] = {}
    models = {}
    for key, name in configured_models(config).items():
        if name not in built:
            built[name] = build_model(config, name)
        models[key] = built[name]
    return models


def model_for(models: dict[str, Model], purpose: str) -> Model:
    """
    The model for one purpose, from build_models(). A purpose whose model isn't configured gets the default one
    (no strong model: model_for('strong') is the default model, so there's nothing to switch to).
    """
    if purpose not in PURPOSE_MODEL:
        raise ValueError(f'Unknown model purpose {purpose!r}; one of {", ".join(PURPOSES)}')
    return models.get(PURPOSE_MODEL[purpose]) or models['default']


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


# --- doctor: does each configured model exist? -----------------------------------------------------------------

MODEL_LIST_TIMEOUT = 5  # seconds; doctor never waits long on the network


def _model_list_request(config: Config) -> tuple[str, dict] | None:
    """(url, headers) of the provider's model list, or None when its key is missing."""
    provider = config.model.provider
    if provider in ('openai', 'anthropic'):
        key = get_key(provider)
        if not key:
            return None
        if provider == 'openai':
            return 'https://api.openai.com/v1/models', {'Authorization': f'Bearer {key}'}
        return 'https://api.anthropic.com/v1/models?limit=1000', {'x-api-key': key, 'anthropic-version': '2023-06-01'}
    key = get_key(provider)
    return (config.model.base_url or OLLAMA_URL).rstrip('/') + '/models', ({'Authorization': f'Bearer {key}'}
                                                                         if key else {})


def available_models(config: Config, transport=None) -> set[str] | None:
    """The model names the provider lists, or None when they can't be had (no key, no answer, an odd answer)."""
    request = _model_list_request(config)
    if request is None:
        return None
    url, headers = request
    try:
        with httpx.Client(transport=transport, timeout=MODEL_LIST_TIMEOUT) as client:
            response = client.get(url, headers=headers)
        listed = response.json().get('data') if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError, AttributeError):
        return None
    if not isinstance(listed, list):
        return None
    return {str(item.get('id')) for item in listed if isinstance(item, dict) and item.get('id')}


def doctor_checks(home, config: Config, transport=None) -> list:
    """One line per configured model: listed by the provider (ok) or not (FAIL). No key or no answer: info only."""
    from local_agent.doctor import FAIL, INFO, OK, Check

    names = configured_models(config)
    uses = {}
    for key, name in names.items():
        uses.setdefault(name, []).append(key)
    listed = available_models(config, transport)
    if listed is None:
        return [Check(INFO, f'Models: {", ".join(sorted(uses))} (couldn\'t get {config.model.provider}\'s model list '
                            'to check them)')]
    found = []
    for name, keys in uses.items():
        known = name in listed
        found.append(Check(OK if known else FAIL, f'Model {name} ({", ".join(keys)}): '
                           + ('available' if known else f'not in {config.model.provider}\'s model list'),
                           f'set another model name in {home.config_path} ([model] or [models])'))
    return found
