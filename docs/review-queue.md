# The review queue

**Files:** `review/queue.py`, `review/cli.py`, `review/api.py`, `memory/review.py`

## One-liner

> When the agent wants to keep something that may be someone else's words (a memory saved after reading an email,
> a fact distill couldn't trace to the owner), it doesn't keep it: the item waits in a queue until the owner says
> yes. Nothing in the queue is in any prompt, and approving is the only way in.

## How it works

- **An item** is a row in `security_review_items` (state.db): its kind (`memory`, `mcp_tool`, later `skill`),
  the content as JSON, a one-line summary, what queued it (`agent`, `distill`...), the run, the third-party sources
  the run had read (`taint`), and its status: `pending`, then `approved` or `rejected`. Content and summary are
  redacted (`core/secrets.py`) before they're stored.
- **A handler per kind** says what approving does. A module registers its own:
  `queue.register('memory', approve, reject=None, show=None)`. `approve(item, ctx)` (`ctx.home`, `ctx.store`)
  applies the item and returns one line; raising `ReviewError` refuses, and the item stays pending. Modules listed
  in `HANDLER_MODULES` are imported before a decision, so their kinds are always known (`skills/review.py` is
  already listed for skill proposals).
- **Deciding** moves the item from `pending` to `approving` first, in one conditional update, so the CLI and the
  API can't both apply it. A handler that fails puts it back to `pending`.
- **The same fact once.** An item can carry a key; adding one whose key matches a pending item of the same kind
  returns that item. The agent saving a fact and distill finding it again make one item.
- **The owner hears about it** in one short message on the notify path (Telegram, or the inbox): "2 items wait for
  your review (2 memory). Nothing from them is used until you approve: local-agent review list". It carries
  counts only, never the queued text, which is someone else's. A run tells the owner when it queues something
  (when it's given the run's notifier). The daemon's `review_notify` job (every minute) covers everything else:
  distill, and items queued by another process. Each item is announced once.
- **Commands:** `local-agent review list [--all] [--kind K]`, `show ID`, `approve ID`, `reject ID [--reason ...]`.
  The API has the same ([api](api.md#review-queue)).
- **Events**, all between runs and none with the content: `review_queued`, `review_approved`, `review_rejected`,
  `review_notified`.

## Memory items

In a run that has read third-party content, `memory_append` and `memory_write` queue a `memory` item
(`{op, path, title, text, base, detail}`) and leave the vault alone. The model is told it's waiting for the
owner and how they approve it, so it says so instead of claiming the save failed. Distill queues a fact whose quote
isn't in the owner's messages. Approving writes the note as the owner's (high trust). A replacement (`write`) is
refused if the note changed after it was queued (`base` is the note's fingerprint then). The format is in
[memory provenance](memory-provenance.md).

Two picks decide this, each one line in `memory/review.py`: `goes_to_review` (today: every write in a run that read
third-party content, including the owner's own "remember X" in that run) and `APPROVED_TRUST` (today: high).

## MCP tool items

A tool that changed, or appeared on a known server, is queued as `mcp_tool` with its old and new definitions
(`review show` prints both). Approving accepts the new definition from the next connect, and keeps an always-allow
the tool had. Approving is refused if the server changed it again since (the newer item is the one to review).
Rejecting keeps the tool at its stricter tier and doesn't queue that same definition again ([MCP](mcp.md)).

## On a laptop

agent-studio had no queue: a write after third-party content was stored as low trust and marked when read back. That
still holds for notes already in the vault, but a low-trust note is still a note: it's searched, linked, and one
"looks fine" away from the profile. A personal agent has one owner who can say yes in a second, so the safer default
is to keep it out until they do.
