"""
The optional sandbox (docs/sandbox.md): off by default ([sandbox] backend = "none"), and Docker is the only other
backend. It changes where an approved command runs, never whether it asks: the shell stays off by default, every
command still needs a "y", and the gate still refuses shell_run when nobody is there.

A broken setting fails closed: the shell refuses (and a sandboxed MCP server doesn't start) with a pointer to
`local-agent audit`, rather than running without the sandbox the owner asked for.
"""

import contextlib
from contextvars import ContextVar
from dataclasses import dataclass

from local_agent.sandbox import docker
from local_agent.sandbox.docker import DockerBackend, container_name, mcp_argv
from local_agent.sandbox.settings import problems

# The running run's event log, so the shell (built before the run has a log) records into the right run
_log: ContextVar = ContextVar('sandbox_log', default=None)


@dataclass(frozen=True)
class Broken:
    """A sandbox that was asked for but can't be built: nothing runs."""

    problems: tuple[str, ...]
    name = 'broken'


def backend_for(config, home, runner=None):
    """None for no sandbox (today's shell), a DockerBackend, or Broken."""
    values = config.section('sandbox')
    found = problems(values, home)
    if found:
        return Broken(tuple(found))
    if values['backend'] == 'none':
        return None
    return DockerBackend(values['image'], values['network'], home.files_dir, runner or docker.run_exec)


def sandboxed(tools: list, rt) -> list:
    """The built-in tools with shell_run swapped for one that runs in the configured sandbox."""
    from local_agent.tools.shell import RUN, shell_tool

    backend = backend_for(rt.config, rt.home)
    if backend is None:
        return tools
    return [shell_tool(rt.home.files_dir, rt.config.shell_timeout, enabled=True, sandbox=backend)
            if tool.name == RUN else tool for tool in tools]


def bind_log(log, stack) -> None:
    """Sandbox events of this run go to its log until the run ends (stack closes)."""
    token = _log.set(log)

    def reset():
        with contextlib.suppress(ValueError):  # set in another context: nothing to undo here
            _log.reset(token)

    stack.callback(reset)


def record(event_type: str, **payload) -> None:
    log = _log.get()
    if log is not None:
        log.append(event_type, **payload)


def mcp_launch(server, env: dict[str, str], values: dict) -> dict:
    """The MCPServerStdio params for a server that opted into the sandbox, or McpConfigError when the settings are
    broken (the server is then left out, like any server that doesn't start)."""
    from local_agent.mcp.config import McpConfigError

    found = problems(values, None, mcp=True)
    if found:
        raise McpConfigError(f'{server.name} is sandboxed, but {"; ".join(found)}. Run local-agent audit.')
    argv = mcp_argv(server.command, server.args, sorted(env), values['image'], values['network'],
                    container_name(f'mcp-{server.name}'))
    return {'command': argv[0], 'args': argv[1:], 'env': env}


def mcp_values() -> dict:
    """The [sandbox] settings for a sandboxed MCP server, read when it connects."""
    from local_agent.config import ConfigError, Home, load_config
    from local_agent.mcp.config import McpConfigError

    try:
        return load_config(Home.locate()).section('sandbox')
    except ConfigError as e:
        raise McpConfigError(f'The settings don\'t load, so a sandboxed server can\'t start: {e}') from None
