# Spill

**Files:** `spill/__init__.py`, `core/render.py`, `core/tool_results.py` (efficiency block), `daemon/jobs.py`
(efficiency block), `config.py` (efficiency block)

## One-liner

> A tool result too big for the context is saved whole to a file. The model gets what it got before (the first
> 4,000 characters), plus the size and the path, and can read the rest with `files_read`.

## How it works

- **When it applies.** A tool result over `[spill] over_chars` (default 4,000, the result cap) goes through the
  tool-result hook. The hook saves it to `workspace/files/.spill/<run id>/<n>-<tool>.txt`: the folder is mode 700,
  the file 600, and the names are made safe.
- **What the model gets** (`render.Bounded`, so the cap doesn't cut the note):

  ```
  <the first 4,000 characters, as before>

  [SPILLED: showing the first 4,000 of 182,340 characters. The full output is saved at '.spill/<run>/1-GMAIL_GET_EMAIL.txt';
  read the rest with files_read(path='.spill/<run>/1-GMAIL_GET_EMAIL.txt', offset=4000).]
  ```

  The model never sees less than it did without spilling, so spilling can't make an answer worse.
- **It's an event.** `spilled` records the tool, the size, the path and whether the result was untrusted.
- **What isn't spilled.** `files_read` already returns one part at a time, so it's never spilled; that also means
  reading a spill file can't spill again. With `over_chars = 0` (or less), spill is off and results are truncated
  as before.

## Security

- **Redacted first.** The text is redacted (`core/secrets.py`) before it's written, as it already is for the model
  and the event log. Token shapes and the owner's own keys never reach the disk.
- **Taint survives.**
  - The gate tracks the full text: links and addresses past the preview still count as third-party, and are
    traced to the tool they came from.
  - The preview is wrapped as untrusted when the result was, and so is the spill note inside it.
  - Reading the file back with `files_read` is third-party content and taints the run, as any file does.
- **Third-party text stays on disk.** Spilled output from emails, pages and app tools stays in
  `workspace/files/.spill/`, redacted, for the retention period (`keep_days`, default 7 days). The folder is inside
  the private state folder (`~/.local-agent`, mode 700). The model can read it again with `files_read`, and it's
  third-party content every time.
- **Retention.** A daily daemon job (`spill_cleanup`) deletes run folders not changed for `[spill] keep_days`
  days (0 or less: kept forever). It logs `spill_cleaned` between runs (`local-agent events`).

## Checked how

`tests/efficiency/test_spill.py` and `tests/attacks/test_efficiency_spill.py`:
- A large page leaves a preview in the context and the full text on disk.
- A token-shaped string, and the owner's own key, are redacted on disk.
- Reading the file back taints the run.
- A link past the preview is still traced to the email.
- A page can't close the untrusted wrapper.
- A tool name can't write outside `.spill/`.
- Cleanup removes only old folders.
