"""
The Claude Code engine: Anthropic's unmodified Claude Code program runs the thinking loop, signed in with the
owner's own Claude Pro or Max login. wraithd never sees, stores or forwards that login: it never reads Claude
Code's credentials file or keychain entry, never sets CLAUDE_CODE_OAUTH_TOKEN, and never calls Anthropic's API
with a login token. It only reads which credential Claude Code chose (the init message's apiKeySource).

Claude Code gets no tools of its own (tools=[]) and none of the owner's other Claude Code settings or MCP servers:
only wraithd's guarded tools, through the in-process MCP server in claude_bridge.py, so every action still goes
through the gate. wraithd's history is sent as text (transcript), so its compaction, memory and conversation
store stay in charge.

claude_agent_sdk is an optional extra (wraithd[claude], ~300 MB with its bundled CLI), imported only when used.
"""

import os
from collections.abc import Mapping

from wraithd.config import Config

SUBSCRIPTION = 'none'  # apiKeySource when Claude Code uses the /login (or Bedrock/Vertex: see login_problem)
# Providers that also report apiKeySource "none" but aren't the owner's Claude login
OTHER_PROVIDERS = ('CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY')
# The fields that keep Claude Code to wraithd's tools and the owner's login. No caller may change them.
SAFE = {
    'tools': [],  # none of Claude Code's own tools (shell, files, web)
    'strict_mcp_config': True,  # only the MCP servers wraithd hands it
    'setting_sources': [],  # none of the owner's Claude Code settings, hooks or CLAUDE.md
    'env': {'ANTHROPIC_API_KEY': ''},  # the login, never a key from the environment
    'extra_args': {'no-session-persistence': None},  # no session files left in ~/.claude
}
OVERRIDABLE = ('system_prompt', 'mcp_servers', 'allowed_tools', 'max_turns', 'model', 'include_partial_messages')
# The purposes and sources that use [engine] background_model; everything else uses chat_model
BACKGROUND_PURPOSES = ('distill', 'compaction', 'heartbeat')
BACKGROUND_SOURCES = ('schedule', 'heartbeat')
INSTALL_HINT = "Claude Code isn't installed for wraithd: run `pip install 'wraithd[claude]'` (or uv/pipx the same)."


class SubscriptionUnavailable(Exception):
    """Not on the owner's Claude login, or the plan's limit was hit. Nothing ran yet, so the caller may fall back."""


def sdk():
    """claude_agent_sdk, imported only when the engine is used. A missing extra says how to install it."""
    try:
        import claude_agent_sdk
    except ImportError:
        raise SubscriptionUnavailable(INSTALL_HINT) from None
    return claude_agent_sdk


def claude_options(**overrides):
    """
    The ClaudeAgentOptions every wraithd use of Claude Code starts from (run, complete, doctor, audit). Always:
    no built-in tools, only wraithd's MCP servers, none of the owner's settings, the login rather than an API key,
    and no saved session. overrides may set only OVERRIDABLE fields; anything else raises ValueError, so a caller
    can never loosen the safe ones.
    """
    refused = sorted(set(overrides) - set(OVERRIDABLE))
    if refused:
        raise ValueError(f'claude_options() can\'t set {", ".join(refused)}: only {", ".join(OVERRIDABLE)}.')
    values = {name: value for name, value in overrides.items() if value is not None}
    # fresh copies, so nothing a caller does to the options changes SAFE
    safe = {name: (dict(value) if isinstance(value, dict) else list(value) if isinstance(value, list) else value)
            for name, value in SAFE.items()}
    return sdk().ClaudeAgentOptions(**values, **safe)


def login_problem(init_data: Mapping, environ: Mapping | None = None) -> str | None:
    """
    None when Claude Code is on the owner's Claude subscription login; else what's wrong, in one line. init_data
    is the init SystemMessage's data. apiKeySource "none" also means Bedrock or Vertex, so those are checked in
    environ (the environment Claude Code started with; default os.environ).
    """
    environ = os.environ if environ is None else environ
    other = next((name for name in OTHER_PROVIDERS if str(environ.get(name, '')).strip().lower()
                  not in ('', '0', 'false', 'no')), None)
    if other:
        return f'Claude Code is set to use another provider ({other} is set), not your Claude login: unset it.'
    source = init_data.get('apiKeySource')
    if source == SUBSCRIPTION:
        return None
    if not source:
        return 'Claude Code did not say which login it uses: run `claude` and type /login.'
    return f'Claude Code is using {source}, not your Claude login: run `claude` and type /login.'


def model_for(config: Config, purpose: str = 'chat', source: str = '') -> str:
    """[engine] background_model for heartbeat, schedules, memory distill and compaction; chat_model otherwise."""
    engine = config.section('engine')
    background = purpose in BACKGROUND_PURPOSES or source in BACKGROUND_SOURCES
    return engine['background_model'] if background else engine['chat_model']
