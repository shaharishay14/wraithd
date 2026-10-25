# Workspace files

**File:** `tools/files.py`

## One-liner

> The agent can list, read and write text files in `~/.local-agent/workspace/files/`, and nowhere else. The
> boundary is a real-path check in code, so `..`, absolute paths and symlinks can't talk their way out of it.

## How it works

- **Three tools:** `files_list(path)`, `files_read(path, offset)` (3,500 characters per call, like a web page,
  with `next_offset` for the next part), `files_write(path, content, append)` (atomic, creates folders).
- **The boundary:** the model's path is joined to the folder and resolved with every symlink followed
  (`Path.resolve()`), then it must still be inside the folder (`is_relative_to`). Absolute paths and `~` are
  refused before that. A symlink inside the folder that points out is refused, and isn't even listed.
- **Writes need no approval.** They can only change files in this one folder, which exists for the agent to work
  in. The approval card is for things that leave the folder or the machine.
- **What's read is third-party content.** Documents people drop in here (a PDF from a colleague, a page the agent
  saved) are written by someone else, so `files_read` output is wrapped and taints the run, like an email. It's
  also the owner's private data, so after reading it only links the owner wrote open freely.
- **Owner only.** A stranger on Telegram (Phase 5) gets no file tools.

## On a laptop

agent-studio had no file tools: the server's disk wasn't the user's. On a laptop the files are the point, and so
is the boundary: the rest of the home folder holds SSH keys, browser profiles and the keychain's neighbours. One
folder, checked in code, keeps the useful part without the rest.

## Limits

- The check is done before opening, so a symlink swapped in between the check and the open (a race) isn't caught.
  Nothing but the owner writes in that folder, so that needs a local attacker already.
- Text only: binary files are refused rather than dumped into the context.
