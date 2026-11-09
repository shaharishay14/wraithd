# The library

**Files:** `library/__init__.py`, `library/tools.py`, `library/index.py`, `library/cli.py`; its lines in the
capabilities blocks of `store/modules.py` (`capabilities_001`), `tools/__init__.py`, `core/gate.py`,
`core/events.py` and `cli.py`

## One-liner

> What radar has read (posts, repos, articles), each with a short note, and the ideas it has suggested, each with a
> status, in state.db: so it never reads the same item twice, never suggests the same idea twice, and answers "that
> post about X" from its note.

## Items

- **One id per thing.** `x:<post id>` (digits only), `gh:<owner>/<repo>` (lowercase), `url:<url>` (scheme and host
  lowercased, no fragment, no `utm_*` parameters, no trailing slash). An X post or a GitHub repo given as a URL gets
  its `x:` or `gh:` id (`url:https://github.com/Owner/Repo/` is `gh:owner/repo`), so the same thing found in the
  watch list and the bookmarks is one item. GitHub's own pages (`/trending/...`, `/topics/...`) stay URLs.
- **`library_seen(ids)`** answers `{"new": [...], "seen": [...]}` (normalized ids, in the order given) in one query;
  ids it can't read come back under `invalid`. At most 200 per call.
- **`library_save(id, kind, title, url, note, source, author?)`**: `kind` is post, repo or article; `source` is x,
  bookmark, github or web. The note is at most 600 characters, in wraithd's own words. Saving an id again replaces
  the note and keeps the first `read_at`. Title, note and author are redacted (`core/secrets.py`) before they're
  stored.
- **`library_search(query, limit=5)`**: the words of the query against title, author, note, url and id. Title and
  author hits weigh most, the score is scaled by how many words matched, the whole phrase adds a bonus, and newer
  wins a tie. One `LIKE` prefilter, then scoring in Python: well under a second on a few thousand items, no new
  dependency. An empty query gives the newest items.

## Ideas

One tool, `ideas(action, ...)`, with `action` list, save or status (one schema instead of three):

- **`ideas(action="save", title, plan, size, files?, source_ids?)`** -> `{"idea": "2026-11-09#3"}` keeps an idea as `suggested` with the id
  `<local date>#<n>` (`2026-11-09#3`): n counts from 1 each day in the owner's timezone. The plan is at most 400
  characters, `size` S, M or L, `files` the real paths it would touch, `source_ids` the items it came from. An idea
  whose title is the same words as one already kept (any status) is not added again: the answer carries its id and
  `already: <status>`, so radar doesn't suggest it twice.
- **`ideas(action="list", status?, limit=50)`** -> `{"ideas": [...]}` (list is the default action): newest day
  first, in the order suggested that day.
- **`ideas(action="status", id, status)`** -> `{"idea": id, "status": ...}`: suggested, accepted, declined or built.

## wraithd_index

`wraithd_index()` says what wraithd is today, so radar doesn't suggest what's built: `version`; `modules` (each
`.py` in the installed package, as its repo path `src/wraithd/...`, and its docstring's first line); and, when the
package runs from a source checkout (`docs/` and `pyproject.toml` next to `src/`), `docs` (each note's title and the
quote under its "One-liner") and `changelog_head` (the CHANGELOG's first 60 lines). It takes no arguments and reads
nothing outside the package and its checkout.

It's built once per process and cut to 10,000 characters of compact JSON (about 2,500 tokens): the most detail that
fits, from one module per entry with summaries down to one entry per package with its file names, the notes'
one-liners kept longest. The paths and titles alone take about 5,000 characters, so a 6,000 cap would leave the
one-liners a dozen characters each. If even the shortest form doesn't fit, entries are dropped from the end and
`truncated` is set.

## Trust

- **Owner runs only.** The five tools are offered in the owner's runs (chat, schedules, watches), never to a stranger.
  Their schemas are 534 tokens (o200k) on every owner model call; a test keeps them at 600 or less.
- **All READ tier.** The writes change only wraithd's own notes, never the owner's data or the outside world, so a
  scheduled radar run never waits for an approval. Each write is an event between runs: `library_saved` (id, kind,
  source, title), `idea_saved` (id, title, size), `idea_status` (id, status, previous).
- **Notes are third-party data.** They were made from posts and pages, so `library_search` and `ideas` are in
  the gate's `UNTRUSTED_OUTPUT_BUILTINS`: their output is wrapped as untrusted and taints the run, like the page
  itself would (any `ideas` call taints, save and status too: one tool, one rule). They're not private data (`PRIVATE_OUTPUT_BUILTINS`): they aren't the owner's accounts.
- **Not memory.** Memory saved after third-party reading waits in the [review queue](review-queue.md); a library
  note is kept as what it is, someone else's text summarized, and never reaches the profile or the vault.

## Commands

```
wraithd library [list] [--kind post|repo|article] [--limit N]   the latest items read
wraithd library show x:1850...                                  one item and its note
wraithd library search mcp memory                               items matching these words, best first
wraithd ideas [list] [--status suggested|accepted|declined|built]
wraithd ideas status 2026-11-09#3 accepted
```

## Tables

`capabilities_library_items` (id, kind, title, url, author, note, source, read_at, updated_at) and
`capabilities_library_ideas` (id, day, n, title, title_key, plan, files and source_ids as JSON, size, status,
created_at, updated_at), both in one migration, `capabilities_001`.
