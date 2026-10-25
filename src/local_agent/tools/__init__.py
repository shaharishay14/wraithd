"""
The built-in tools: workspace files, the shell, the web, schedules. Which ones a run gets depends on who is
talking and whether anyone is there to approve:

- everyone: read_website, and web_search when a search key is set
- the owner: the file and schedule tools, and notify_owner
- the owner, in a conversation, with [shell] enabled: shell_run (never in unattended runs)
"""

from local_agent.config import get_key
from local_agent.schedules import local_timezone
from local_agent.tools.files import file_tools
from local_agent.tools.notify import notify_tool
from local_agent.tools.schedules import schedule_tools
from local_agent.tools.shell import shell_tool
from local_agent.tools.web import PROVIDERS, read_website_tool, web_search_tool


def search_provider(config, transport=None):
    """The configured search provider, or None when search is off or its key isn't set."""
    if not config.search:
        return None
    key = get_key(config.search)
    return PROVIDERS[config.search](key, transport) if key else None


def builtin_tools(rt, owner: bool, attended: bool) -> list:
    tools = [read_website_tool(rt.web_transport)]
    provider = search_provider(rt.config, rt.search_transport)
    if provider:
        tools.append(web_search_tool(provider))
    if owner:
        tools += file_tools(rt.home.files_dir)
        tools += schedule_tools(rt.store, local_timezone(rt.config.timezone))
        tools.append(notify_tool(rt.notifier))
        if attended and rt.config.shell_enabled:
            tools.append(shell_tool(rt.home.files_dir, rt.config.shell_timeout, enabled=True))
    return tools
