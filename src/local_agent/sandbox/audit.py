"""
The sandbox's lines in `local-agent audit` and `local-agent doctor`. The audit fails on a sandbox setting that's
broken (the shell would refuse), widened beyond workspace/files/ and no network, or that reads as if a sandboxed
shell could run unattended. It only reads.
"""

import shutil
import tomllib

from local_agent.doctor import FAIL, INFO, OK, Check
from local_agent.sandbox.settings import DEFAULTS, problems

# Keys that would mean "let the sandboxed shell run with nobody there". No such setting exists, and none ever will.
UNATTENDED_KEYS = ('unattended', 'unattended_shell', 'allow_unattended')


def _raw(home) -> dict:
    """config.toml as written: load_config drops keys it doesn't know, and those are what some rules look for."""
    try:
        return tomllib.loads(home.config_path.read_text()) if home.config_path.exists() else {}
    except tomllib.TOMLDecodeError:
        return {}


def sandbox_rules(home, config) -> list[Check]:
    values = config.section('sandbox')
    raw = _raw(home)
    table = raw.get('sandbox') if isinstance(raw.get('sandbox'), dict) else {}
    shell = raw.get('shell') if isinstance(raw.get('shell'), dict) else {}
    sandboxed = [server.name for server in config.mcp_servers if getattr(server, 'sandbox', False)]
    docker = values['backend'] == 'docker'
    fix = f'edit [sandbox] in {home.config_path} (see docs/sandbox.md)'
    shell_problems = problems(values, home)
    found = [Check(FAIL, f'Sandbox: {problem}; the shell refuses to run', fix) for problem in shell_problems]
    if sandboxed:
        found += [Check(FAIL, f'Sandbox: {problem}; sandboxed MCP servers ({", ".join(sandboxed)}) won\'t start', fix)
                  for problem in problems(values, home, mcp=True) if problem not in shell_problems]
    if docker and values['network'] == 'bridge':
        found.append(Check(FAIL, 'Sandbox: the shell\'s container can reach the network ([sandbox] network = '
                                 '"bridge"), so a command can send out what it reads', f'set network = "none" in '
                                 f'[sandbox] of {home.config_path}'))
    # The gate's own rule (shell_run asks every time, never unattended) is the core audit's "Shell:" line
    unattended = [f'[{where}] {key}' for where, keys in (('sandbox', table), ('shell', shell))
                  for key in keys if key in UNATTENDED_KEYS]
    if unattended:
        found.append(Check(FAIL, f'Sandbox: {", ".join(unattended)} is not a setting: the shell never runs '
                                 'unattended, sandboxed or not', f'remove it from {home.config_path}'))
    unknown = sorted(key for key in table if key not in DEFAULTS and key not in UNATTENDED_KEYS)
    if unknown:
        found.append(Check(FAIL, f'Sandbox: [sandbox] {", ".join(unknown)} {"is" if len(unknown) == 1 else "are"} '
                                 'not a setting and does nothing: the shell only ever sees workspace/files',
                           f'remove {", ".join(unknown)} from [sandbox] in {home.config_path}'))
    if not found:
        found.append(Check(OK, 'Sandbox: docker, the shell sees only workspace/files, no network, never unattended'
                           if docker else 'Sandbox: off (the shell, when on, runs as you after each "y")'))
    if sandboxed:
        found.append(Check(INFO, f'Sandboxed MCP servers ({", ".join(sandboxed)}): docker, nothing mounted, network '
                                 f'{values["network"]}'))
    return found


def doctor_checks(home, config) -> list[Check]:
    """One line: docker is there when something is set to run in it."""
    sandboxed = any(getattr(server, 'sandbox', False) for server in config.mcp_servers)
    if config.section('sandbox')['backend'] != 'docker' and not sandboxed:
        return []
    path = shutil.which('docker')
    return [Check(OK if path else FAIL, f'Docker: {path}' if path else 'Docker: not found, so the sandbox can\'t run',
                  'install Docker Desktop (or docker engine), or set [sandbox] backend = "none"')]
