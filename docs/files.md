# Workspace files

**Files:** `tools/files.py`, `tools/documents.py`

## One-liner

> The agent can list, read and write files in `~/.local-agent/workspace/files/` (PDF, Word and spreadsheets read
> as text), and nowhere else. The
> boundary is a real-path check in code, so `..`, absolute paths and symlinks can't talk their way out of it.

## How it works

- **Three tools:** `files_list(path)`, `files_read(path, offset)` (3,500 characters per call, like a web page,
  with `next_offset` for the next part), `files_write(path, content, append)` (atomic, creates folders).
- **Documents come out as text.** `files_read` on a `.pdf` (pypdf, pages marked `--- page 3 ---`), a `.docx`
  (paragraphs, headings marked `#`/`##`, tables as tab-separated rows), a `.xlsx` (openpyxl in read-only mode with
  cached values, `--- sheet <name> ---`, tab-separated rows, empty trailing rows and columns left out) or a `.csv`
  returns the extracted text, paged with `offset` like any text file. The result says which: `format` is `pdf`,
  `docx`, `xlsx`, `csv` or `text`. The text is extracted again on each call; it isn't cached.
- **The boundary:** the model's path is joined to the folder and resolved with every symlink followed
  (`Path.resolve()`), then it must still be inside the folder (`is_relative_to`). Absolute paths and `~` are
  refused before that. A symlink inside the folder that points out is refused, and isn't even listed.
- **Writes need no approval.** They can only change files in this one folder, which exists for the agent to work
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
  folder: a swapped path finds nothing to move, the write fails, and nothing lands outside.
- **What's left.** `files_list` still checks then lists, so a swap mid-listing could show names and sizes (never
  contents) from outside. `files_write` creates missing folders before its check, so a swap at that moment could
  leave an empty folder outside. Both need a process already running as the owner, which could read the files
  itself.
- **Documents are read within caps**, since anyone can send one: 50 MB on disk, 100 MB once a `.docx`/`.xlsx` is
  unzipped (a zip bomb is refused before it's opened), 2,000,000 characters of text, 500 pages, 50 sheets,
  20,000 rows and 200 columns per sheet or CSV. Hitting a cap adds a `limits` note to the result, so the model
  knows it hasn't seen the whole document. Extraction runs in a thread and the run waits 30 seconds at most; a
  thread can't be killed, so the caps are what bound the work left behind.
- **What can't be read says so.** A password-protected PDF (one encrypted only to set permissions opens), a
  damaged file, an old `.doc`/`.xls` renamed, or a scanned PDF with no text layer returns an error that tells the
  model what the user can do: remove the password, re-save it, or run it through OCR. There's no OCR here.
- `.docx` is parsed with the standard library (it's a zip of XML) rather than python-docx, which needs lxml: the
  dependencies stay pure Python. Embedded images, comments, footnotes and headers are not read.
- Other binary files (images, `.doc`, `.xls`, ...) are refused rather than dumped into the context.
