"""
The built-in tools: workspace files, the shell, the web, schedules. Which ones a run gets depends on who is
talking and whether anyone is there to approve:

- everyone: read_website, and web_search unless it's off (choose_search says which backend, and why)
- the owner: the file and schedule tools, and notify_owner
- the owner, in a conversation, with [shell] enabled: shell_run (never in unattended runs)
"""

import importlib.util
import sys
from dataclasses import dataclass

from wraithd.config import get_key
from wraithd.schedules import local_timezone
from wraithd.tools.files import file_tools
from wraithd.tools.notify import notify_tool
from wraithd.tools.schedules import schedule_tools
from wraithd.tools.shell import shell_tool
from wraithd.tools.web import (
    COST,
    AnthropicSearch,
    ClaudeSearch,
    DuckDuckGo,
    OpenAISearch,
    SerpAPI,
    read_website_tool,
    web_search_tool,
)

NATIVE = {'openai': OpenAISearch, 'anthropic': AnthropicSearch}  # the model provider's own search, on its key
KEY_NAMES = {'openai': 'OpenAI', 'anthropic': 'Anthropic'}


@dataclass(frozen=True)
class SearchChoice:
    backend: str  # '' when web search is off
    why: str  # one line for doctor and onboard
    key: str | None = None
    missing_key: str = ''  # off because this key isn't set


def _claude_extra() -> bool:
    """claude-agent-sdk (the claude extra) is installed."""
    try:
        return 'claude_agent_sdk' in sys.modules or importlib.util.find_spec('claude_agent_sdk') is not None
    except (ImportError, ValueError):
        return False


def choose_search(config) -> SearchChoice:
    """
    Which search backend runs get. "auto" (the default) is the model provider's own search for openai and
    anthropic, on the model key, and DuckDuckGo for local models (ollama, openai-compatible), which need no key.
    On the Claude Code engine "auto" without that key is DuckDuckGo too: the thinking needs no key there, so a
    missing one shouldn't turn search off.
    """
    setting, provider = config.search, config.model.provider
    if not setting:
        return SearchChoice('', 'off in the settings ([web] search = "")')
    # On Claude Code the thinking runs on the login, so "your model key" would be unclear: name the provider's key
    on_claude = config.section('engine')['name'] == 'claude-code'
    backend = setting if setting != 'auto' else provider if provider in NATIVE else 'duckduckgo'
    if setting == 'auto' and on_claude and backend in NATIVE and not get_key(backend):
        return SearchChoice('duckduckgo', f'duckduckgo ({COST["duckduckgo"]}, since your '
                                          f'{KEY_NAMES[backend]} key isn\'t set; [web] search = "claude" searches '
                                          'through your Claude subscription instead)')
    if backend == 'duckduckgo':
        local = ', since a local model has no provider search' if setting == 'auto' else ''
        return SearchChoice(backend, f'duckduckgo ({COST[backend]}{local})')
    if backend == 'claude':
        if not _claude_extra():
            return SearchChoice('', "off: claude search needs the Claude Code extra (uv tool install "
                                    "'wraithd[claude]'), which isn't installed")
        return SearchChoice(backend, f'claude ({COST[backend]})')
    key = get_key(backend)
    whose = (f'your {KEY_NAMES.get(backend, backend)} key' if on_claude
             else 'your model key' if backend == provider else f'your {backend} key')
    if not key:
        return SearchChoice('', f'off: {backend} search needs {whose}, which is missing', missing_key=backend)
    return SearchChoice(backend, f'{backend} ({whose}), {COST[backend]}', key)


def search_provider(config, transport=None, web_transport=None):
    """The search provider runs get, or None when search is off or its key isn't set."""
    choice = choose_search(config)
    if choice.backend == 'duckduckgo':
        return DuckDuckGo(web_transport)  # through the egress guard, which takes a sync transport
    if choice.backend == 'claude':
        return ClaudeSearch(config.section('engine'))
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
    # New built-in tools: each worker inside its own block only (and in the gate's tables, core/gate.py)
    # >>> security
    # <<< security
    # >>> efficiency
    # <<< efficiency
    # >>> capabilities
    if owner:
        from wraithd.library.tools import library_tools

        tools += library_tools(rt.store, local_timezone(rt.config.timezone))
    # <<< capabilities
    # >>> reach
    # <<< reach
    # >>> evals
    # <<< evals
    # >>> sandbox
    from wraithd import sandbox

    tools = sandbox.sandboxed(tools, rt)
    # <<< sandbox
    return tools
