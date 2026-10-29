# The sandbox

**Files:** `sandbox/__init__.py`, `sandbox/settings.py`, `sandbox/docker.py`, `sandbox/audit.py`, `tools/shell.py`

## One-liner

> Optional, and off by default. With `[sandbox] backend = "docker"`, an approved shell command runs in a container
> that sees only `workspace/files/` and has no network. It changes where a command runs, never whether it asks.

## What stays the same

- The shell is still off by default (`[shell] enabled = false`), and every command still needs a "y".
- It never runs unattended, sandboxed or not: `run()` doesn't offer it with nobody there, and the gate refuses
  `shell_run` outright (`NEVER_UNATTENDED`). There is no setting to change that, and the audit fails on one that
  looks like it (`unattended`, `unattended_shell`).
- The same timeout (`[shell] timeout_seconds`), output cap (10,000 characters per stream) and an environment without
  the keys the harness knows about. The output is still third-party text: wrapped, and it taints the run.

## Settings

```toml
[sandbox]
backend = "none"                  # none (the default: the shell runs as you) | docker
image = "debian:bookworm-slim"    # what the shell and sandboxed MCP servers run in
network = "none"                  # none | bridge
```

Docker is the only backend. The settings load even when they're wrong, so `local-agent audit` can say what's wrong;
the shell refuses to run until it's fixed.

## The shell under docker

```
docker run --rm --name local-agent-shell-<id> --network none
  --mount type=bind,source=<real path of workspace/files>,target=/workspace --workdir /workspace
  --user <your uid>:<gid> --cap-drop ALL --security-opt no-new-privileges --read-only --tmpfs /tmp
  --pids-limit 256 --env HOME=/tmp <image> sh -c <the command>
```

- **One mount.** `workspace/files/` by its real path, at `/workspace`. The state folder (`config.toml`, `state.db`,
  the API token), your home folder and everything else aren't in the container, so `../`, absolute paths and a
  symlink inside `files/` lead to paths that don't exist there. Files the command writes are yours (`--user`).
- **No network** unless you set `network = "bridge"`, and the audit fails on that.
- **The command is one argument** to `sh -c` inside the container. No shell on this machine parses it, so it can't
  add docker flags.
- **The timeout** kills the docker client's process group and then the container by name (`docker kill`), since
  stopping the client doesn't stop the container.
- **Fail closed.** A broken setting (an unknown backend or network, an image name that looks like a flag, a
  `workspace/files` that is really `/`, your home folder or the state folder) makes `shell_run` refuse with a
  pointer to `local-agent audit`. It never falls back to running without the sandbox you asked for. No Docker on
  the machine is a clear error, not a fallback either.
- The tool's description tells the model it runs in a sandbox at `/workspace`. The approval card still says the
  command runs with your permissions: the gate doesn't know about the sandbox, so it errs on the safe side.

## MCP servers (opt-in, per server)

```
local-agent mcp add notes --sandbox --env NOTES_TOKEN -- npx -y some-notes-mcp
```

stores `sandbox = true` in that server's block (local servers only; `--url` refuses it). The server then starts
as `docker run --rm -i --init ... -e NOTES_TOKEN <image> npx -y some-notes-mcp`: nothing mounted, the `[sandbox]`
network, the same hardening, and its secrets passed by name (`-e NAME` takes the value from the docker client's
environment), so a value is never on a command line.

- **The default image has no node or python**, so most servers (`npx`, `uvx`) won't start in it. Set
  `[sandbox] image` to one that has the server's runtime (`node:22-slim`, `python:3.12-slim`). A sandboxed server
  that doesn't start says so, and points at `[sandbox] image`. There's one image for everything; no per-server
  images.
- A server that talks to an app on the internet needs `network = "bridge"`. For a sandboxed server that's expected
  (the audit lists it as info); for the shell under docker the audit fails on it, since both share the setting.
- A broken `[sandbox]` setting keeps sandboxed servers from starting (reported like any server that fails), never
  starts them unsandboxed.

## Events

| Event | When |
|---|---|
| `sandbox_run` | an approved command ran in docker: backend, image, network, mounts, container, exit code, timed out (no output) |
| `sandbox_refused` | the settings are broken, so the command didn't run: the problems |
| `sandbox_mcp_start` | a sandboxed MCP server started (between runs): server, command |

The shell is built before the run has an event log, so the run hands its log to the sandbox through a context
variable (`sandbox.bind_log`), reset when the run ends; a subagent's run logs to its own.

## The audit

`local-agent audit` fails (exit 1) on each of these, with the fix:

1. `backend` is not `none` or `docker`.
2. Docker with `network` other than `none` (`bridge` runs but is widened; anything else refuses).
3. A widened mount: the real path of `workspace/files` is `/`, your home folder, contains the state folder, or has a
   `,` (it would break docker's `--mount`).
4. An `image` that isn't a plain image name (empty, starts with `-`, has spaces or shell characters).
5. Anything that reads as "unattended": `unattended`, `unattended_shell` or `allow_unattended` in `[sandbox]` or
   `[shell]`. (The gate's own rule is the core audit's "Shell:" line.)
6. Any other key in `[sandbox]` (`mounts`, `volumes`, `privileged`...): it does nothing, but whoever wrote it thinks
   it does.

Sandboxed MCP servers are an info line, and their settings are checked too (1 doesn't apply: they always use
docker). `local-agent doctor` adds one line when anything uses docker: is `docker` on the PATH.

## Tests

The default tests use a fake docker that records the argv and the client's environment and runs nothing
(`tests/sandbox/`, `tests/attacks/test_sandbox_escape.py`). One real test runs against Docker, skipped by default:

```
LOCAL_AGENT_DOCKER_TESTS=1 uv run pytest tests/sandbox/test_real_docker.py
```

## What's honest to say

- A container is a strong fence, not a perfect one: a kernel or Docker bug can break out of it. The approval on
  every command is still the first defense.
- With `network = "bridge"` a command can send what it reads in `workspace/files/` anywhere.
- On macOS, Docker Desktop runs containers in a VM and shares folders into it; `workspace/files` has to be in a
  shared folder (`~/.local-agent` is, by default).
