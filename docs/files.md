# Workspace files

**File:** `tools/files.py`

## One-liner

> The agent can list, read, write and edit text files in `~/.local-agent/workspace/files/`, and nowhere else. The
> boundary is a real-path check in code, so `..`, absolute paths and symlinks can't talk their way out of it.

## How it works

- **Four tools:** `files_list(path)`, `files_read(path, offset)` (3,500 characters per call, like a web page,
  with `next_offset` for the next part), `files_write(path, content, append)` (atomic, creates folders), and
  `files_edit(path, old_text, new_text, replace_all)` for changing part of a file.
- **Editing instead of rewriting.** A model that rewrites a whole file to change one line spends tokens on all of
  it and can drop or mangle the rest. `files_edit` replaces `old_text` with `new_text` and leaves every other byte
  as it was. `old_text` must match exactly once: not found is an error that says to copy the text from
  `files_read` (spaces and line breaks included), and found N times says to add the surrounding lines or set
  `replace_all`. An empty `new_text` deletes. The result is `{path, replacements, bytes, context}`, where
  `context` is the changed lines and two on each side, numbered, so the model can check the edit without reading
  the file again. A file with Windows line endings matches plain newlines in `old_text`, and keeps its CRLF.
  Documents (`.pdf`, `.docx`, `.xlsx`...), binary files and files that aren't UTF-8 are refused rather than
  damaged, and a file over 1,000,000 characters is too large to edit. The write is atomic (a temporary file in
  the checked folder, renamed over the original), keeps the file's mode, and if it fails the original stays as it
  was.
- **The boundary:** the model's path is joined to the folder and resolved with every symlink followed
  (`Path.resolve()`), then it must still be inside the folder (`is_relative_to`). Absolute paths and `~` are
  refused before that. A symlink inside the folder that points out is refused, and isn't even listed.
- **Writes and edits need no approval.** They can only change files in this one folder, which exists for the agent to work
  in. The approval card is for things that leave the folder or the machine.
- **What's read is third-party content.** Documents people drop in here (a PDF from a colleague, a page the agent
  saved) are written by someone else, so `files_read` output is wrapped and taints the run, like an email. It's
  also the owner's private data, so after reading it only links the owner wrote open freely.
- **Owner only.** A run with `owner=False` gets no file tools.

## On a laptop

agent-studio had no file tools: the server's disk wasn't the user's. On a laptop the files are the point, and so
is the boundary: the rest of the home folder holds SSH keys, browser profiles and the keychain's neighbours. One
folder, checked in code, keeps the useful part without the rest.

## Limits

- **The race is closed for reads and writes.** A symlink swapped in between the check and the open (by another
  process on the machine) is caught after opening: the file is opened with `O_NOFOLLOW` on its real path, and the
  file or folder that was actually opened must be the same one (`samestat`) that the path resolves to inside the
  folder now. Writes open the folder first, check it, then create the temporary file in it (`dir_fd`) and rename
  it over the name, which replaces a symlink rather than following it; appends open with `O_NOFOLLOW`. Python on
  macOS has no `renameat`, so the rename there goes by path, but the temporary file only exists in the checked
  folder: a swapped path finds nothing to move, the write fails, and nothing lands outside. `files_edit` reads
  through the same checked open and writes the same way as a replacing `files_write`.
- **What's left.** `files_list` still checks then lists, so a swap mid-listing could show names and sizes (never
  contents) from outside. `files_write` creates missing folders before its check, so a swap at that moment could
  leave an empty folder outside. Both need a process already running as the owner, which could read the files
  itself.
- Text only: binary files are refused rather than dumped into the context.
