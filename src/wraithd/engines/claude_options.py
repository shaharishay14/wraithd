"""
The only place Claude Code's options are built, for the engine, the doctor and the audit alike.

Claude Code gets none of its own tools (tools=[]), only the MCP servers wraithd hands it (strict_mcp_config), none of
the owner's Claude Code settings, hooks or CLAUDE.md (setting_sources=[]), the owner's Claude login rather than any
API key in the environment, and no session files saved in ~/.claude. Every call names its model: with none set,
Claude Code picks Opus, the priciest.

wraithd never reads Claude Code's credentials file or keychain entry, never sets CLAUDE_CODE_OAUTH_TOKEN, and never
calls Anthropic's API itself: only the unmodified Claude Code program, started by claude-agent-sdk, does. It only
reads which credential Claude Code chose (the init message's apiKeySource).

claude_agent_sdk is an optional extra (wraithd[claude], ~300 MB with its bundled CLI), imported only when used.
"""

import os

from wraithd.core.loop import MAX_AGENT_STEPS

SERVER = 'wraithd'  # the in-process MCP server with wraithd's guarded tools (claude_bridge.py)
LOGIN = 'none'  # the init message's apiKeySource when Claude Code is on the /login (Bedrock and Vertex say it too)
# Heartbeat, schedules, memory distill and compaction run on [engine] background_model; the rest on chat_model
BACKGROUND = {'heartbeat', 'schedule', 'distill', 'compaction'}
# Set, Claude Code uses another provider, and its apiKeySource still reads "none"
OTHER_PROVIDERS = ('CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY')
INSTALL_HINT = "Claude Code isn't installed for wraithd: run `pip install 'wraithd[claude]'` (or the uv/pipx same)."


class SubscriptionUnavailable(Exception):
    """Not on the owner's Claude login, or the plan's limit was hit. The caller may fall back if nothing ran."""


def sdk():
    """claude_agent_sdk, imported only when the engine is used. A missing extra says how to install it."""
    try:
        import claude_agent_sdk
    except ImportError:
        raise SubscriptionUnavailable(INSTALL_HINT) from None
    return claude_agent_sdk


def model_for(engine_config: dict, purpose: str) -> str:
    """The model for one purpose (or a run's source, 'schedule'): background_model for BACKGROUND, else chat_model."""
    return engine_config['background_model'] if purpose in BACKGROUND else engine_config['chat_model']


def subscription_env_problem() -> str | None:
    """What in the environment would keep Claude Code off the owner's Claude login, or None."""
    for variable in OTHER_PROVIDERS:
        if os.environ.get(variable, '').strip().lower() not in ('', '0', 'false', 'no', 'off'):
            return f'{variable} is set, so Claude Code would not use your Claude login: unset it'
    return None


def claude_options(instructions: str, server, names: list[str], model: str, stream: bool = False):
    """
    ClaudeAgentOptions for one Claude Code session. server: the bridge's MCP server (None for a tool-less call),
    names: the tools it may call (claude_bridge.bridge), model: always set. The audit checks the fixed fields.
    """
    if not model:
        raise ValueError('claude_options() needs a model: without one Claude Code picks Opus.')
    return sdk().ClaudeAgentOptions(
        system_prompt=instructions, tools=[], mcp_servers={SERVER: server} if server else {},
        allowed_tools=list(names), strict_mcp_config=True, setting_sources=[],
        env={'ANTHROPIC_API_KEY': ''}, extra_args={'no-session-persistence': None},
        max_turns=MAX_AGENT_STEPS, model=model, include_partial_messages=stream)
