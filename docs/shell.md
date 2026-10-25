# The shell

**File:** `tools/shell.py`

## One-liner

> Off by default. When the owner turns it on, the agent can ask to run a shell command, and every single command
> is shown in full and runs only after a "y". It never runs unattended.

## How it works

- **Off:** `[shell] enabled = false` in `config.toml` is the default. Off means the tool isn't offered, and the
  tool itself refuses too, in case it's ever offered by mistake.
- **On, in a conversation:** `shell_run(command)` is a write, so the permission gate shows the approval card: the
  exact command, a warning that it runs with the owner's permissions, and any third-party content read before it.
  Anything but "y" is no. In the terminal the question waits for an answer; where a card can go unanswered (the
  web UI, Telegram), it expires after 60 seconds and the command doesn't run.
- **Never unattended:** a scheduled run has nobody to ask. `run()` doesn't offer the shell then, and the gate
  refuses `shell_run` outright when nobody is watching, even if it was somehow pre-approved (`NEVER_UNATTENDED`).
- **When it runs:** in `workspace/files/`, with stdin closed, in its own process group so the timeout
  (`timeout_seconds`, 30 by default) kills everything it started, 10,000 characters of output per stream, and an
  environment without the API keys the harness knows about. A non-zero exit counts as a failed tool call, so three
  in a row stop the run.
- **Output is third-party content:** a command can print anything (a downloaded page, a file someone sent), so it's
  wrapped and taints the run, like an email.

## Why not leave it out

The plan's open question was whether to ship a shell before there's a sandbox. It ships off, with the strongest
gate there is (a person reading each command), because the alternative is people pasting commands from the
agent into their own terminal, with no log and no timeout.

## On a laptop

There is no sandbox: an approved command runs as the owner, with everything their account can reach. The working
folder is a default, not a boundary (`cd ..` works). The safety is the approval, the log of every command
(`local-agent runs show`), and the rule that nothing runs while nobody is there. A Docker sandbox is listed under
"Not now" in the plan.
