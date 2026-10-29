"""
The docker backend. Building the command line is pure (argv(), mcp_argv()), so tests check exactly what docker
would be asked for; running it goes through an injected exec, which tests replace with one that records the argv
and runs nothing.

The shell's container sees one folder, workspace/files/ (by its real path, at /workspace), has no network unless
the owner widened it, no capabilities, a read-only root with a scratch /tmp, a process limit, and the owner's uid.
The command is a single argument to `sh -c` inside the container: no shell on this machine ever parses it.
"""

import asyncio
import os
import signal
import uuid
from dataclasses import dataclass
from pathlib import Path

WORKDIR = '/workspace'
KILL_TIMEOUT_SECONDS = 10
HARDENING = ('--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--read-only', '--tmpfs', '/tmp',
             '--pids-limit', '256', '--env', 'HOME=/tmp')


@dataclass(frozen=True)
class Result:
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool = False


async def run_exec(argv: list[str], env: dict[str, str], timeout: float) -> Result:
    """Runs argv in its own process group with stdin closed; on timeout the whole group is killed."""
    process = await asyncio.create_subprocess_exec(
        *argv, env=env, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, start_new_session=True)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
        return Result(None, b'', b'', timed_out=True)
    return Result(process.returncode, stdout, stderr)


def _user() -> str:
    return f'{os.getuid()}:{os.getgid()}'


def container_name(kind: str) -> str:
    return f'local-agent-{kind}-{uuid.uuid4().hex[:12]}'


@dataclass(frozen=True)
class DockerBackend:
    image: str
    network: str
    files_dir: Path
    runner: object = run_exec  # async (argv, env, timeout) -> Result

    name = 'docker'

    @property
    def mount_source(self) -> str:
        return os.path.realpath(self.files_dir)

    def argv(self, command: str, name: str) -> list[str]:
        return ['docker', 'run', '--rm', '--name', name, '--network', self.network,
                '--mount', f'type=bind,source={self.mount_source},target={WORKDIR}', '--workdir', WORKDIR,
                '--user', _user(), *HARDENING, self.image, 'sh', '-c', command]

    async def run(self, command: str, timeout: float, env: dict[str, str]) -> tuple[Result, str]:
        """(the result, the container's name). Killing the docker client doesn't stop the container, so a
        timeout kills the container by name too."""
        name = container_name('shell')
        result = await self.runner(self.argv(command, name), env, timeout)
        if result.timed_out:
            await self.runner(['docker', 'kill', name], env, KILL_TIMEOUT_SECONDS)
        return result, name


def mcp_argv(command: str, args, env_names, image: str, network: str, name: str) -> list[str]:
    """A stdio MCP server inside docker: nothing mounted, stdin kept open for the protocol, and its secrets passed
    by name only (`-e NAME` takes the value from the docker client's environment, so it's never in argv)."""
    env_flags = [flag for key in env_names for flag in ('-e', key)]
    return ['docker', 'run', '--rm', '-i', '--init', '--name', name, '--network', network, '--user', _user(),
            *HARDENING, *env_flags, image, command, *args]
