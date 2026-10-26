"""
The built-in tools: workspace files, the shell, the web, schedules. Which ones a run gets depends on who is
talking and whether anyone is there to approve:

- everyone: read_website, and web_search unless it's off (choose_search says which backend, and why)
- the owner: the file and schedule tools, and notify_owner
- the owner, in a conversation, with [shell] enabled: shell_run (never in unattended runs)
"""

from dataclasses import dataclass

from local_agent.config import get_key
from local_agent.schedules import local_timezone
from local_agent.tools.files import file_tools
from local_agent.tools.notify import notify_tool
from local_agent.tools.schedules import schedule_tools
from local_agent.tools.shell import shell_tool
from local_agent.tools.web import (
    COST,
    AnthropicSearch,
    DuckDuckGo,
    OpenAISearch,
    SerpAPI,
    read_website_tool,
    web_search_tool,
)

NATIVE = {'openai': OpenAISearch, 'anthropic': AnthropicSearch}  # the model provider's own search, on its key


@dataclass(frozen=True)
class SearchChoice:
    backend: str  # '' when web search is off
    why: str  # one line for doctor and onboard
    key: str | None = None
    missing_key: str = ''  # off because this key isn't set


def choose_search(config) -> SearchChoice:
    """
    Which search backend runs get. "auto" (the default) is the model provider's own search for openai and
    anthropic, on the model key, and DuckDuckGo for local models (ollama, openai-compatible), which need no key.
    """
    setting, provider = config.search, config.model.provider
    if not setting:
        return SearchChoice('', 'off in the settings ([web] search = "")')
    backend = setting if setting != 'auto' else provider if provider in NATIVE else 'duckduckgo'
    if backend == 'duckduckgo':
        local = ', since a local model has no provider search' if setting == 'auto' else ''
        return SearchChoice(backend, f'duckduckgo ({COST[backend]}{local})')
    key = get_key(backend)
    whose = 'your model key' if backend == provider else f'your {backend} key'
    if not key:
        return SearchChoice('', f'off: {backend} search needs {whose}, which is missing', missing_key=backend)
    return SearchChoice(backend, f'{backend} ({whose}), {COST[backend]}', key)


def search_provider(config, transport=None, web_transport=None):
    """The search provider runs get, or None when search is off or its key isn't set."""
    choice = choose_search(config)
    if choice.backend == 'duckduckgo':
        return DuckDuckGo(web_transport)  # through the egress guard, which takes a sync transport
    if choice.backend in NATIVE:
        return NATIVE[choice.backend](choice.key, transport)
    return SerpAPI(choice.key, transport) if choice.backend == 'serpapi' else None


def builtin_tools(rt, owner: bool, attended: bool) -> list:
    tools = [read_website_tool(rt.web_transport)]
    provider = search_provider(rt.config, rt.search_transport, rt.web_transport)
    if provider:
        tools.append(web_search_tool(provider))
    if owner:
        tools += file_tools(rt.home.files_dir)
        tools += schedule_tools(rt.store, local_timezone(rt.config.timezone))
        tools.append(notify_tool(rt.notifier))
        if attended and rt.config.shell_enabled:
            tools.append(shell_tool(rt.home.files_dir, rt.config.shell_timeout, enabled=True))
    return tools
