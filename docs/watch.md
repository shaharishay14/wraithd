# Feed watches

**Files:** `daemon/watch.py`; its lines in the capabilities blocks of `daemon/jobs.py`, `config.py` (an `open`
Section: any `[watch.<name>]` table is kept, and `watch.check` validates it), `core/events.py` and `cli.py`

## One-liner

> The daemon fetches a JSON feed on a timer, in code, and compares its items with the ones it has seen. It runs the
> agent only when something is new. A quiet poll costs 0 model tokens (one HTTP GET). A poll with new items costs
> one run.

## Setting one up

One `[watch.<name>]` table in `config.toml` per feed:

```toml
[watch.slabbed]
url = "https://slabbedmrr.com/api/agent-feed"
items = "new_cards"          # the JSON array to watch: a dot path ("data.items"); "" = the feed is the array
key = "card_url"             # what makes an item new; "card_url,crossed" = a composite key
every_minutes = 15           # at least 5
message = "use slabbed-x, mode watch"
# quiet_hours = "22:00-07:00"  # local time; without it a watch runs at all hours (not [heartbeat]'s quiet hours)
# enabled = false            # keep it configured but stop polling
```

A wrong value fails at config load with the reason, like any other setting. The daemon's `watch` job runs only when
at least one watch is configured. It checks every minute which watches are due.

## How it works

- **Fetch, in code:** `core/egress.safe_get`, the same guard `read_website` uses: http(s) on default ports only,
  public addresses only, every redirect and the connected peer checked, a 10 s timeout. Then the feed must be at
  most 1 MB, be JSON (`application/json` or `+json`), and hold a list at `items`. A missing or null array counts as
  empty, so the poll is quiet (`milestones` can be `[]` for days). Anything else is a `watch_error`. No model is
  involved. So a typo in `items` reads as an empty feed: `wraithd watch` shows the item count of each poll.
- **Seen keys:** kept per watch in the store's settings (`watch:<name>`), the newest 5000 of them. An item's key is
  its `key` fields together. An item that has none of them is skipped.
- **The first good poll is a baseline:** everything in the feed then is marked seen and nothing runs, so turning a
  watch on doesn't act on ten old cards. Its `watch_checked` event has `status: "baseline"`. A feed that errors
  first (a 404 before it's live) doesn't count: the first poll that succeeds is the baseline.
- **New items: one run.** The message is `message`, then only the new items as JSON: at most 20 per run (the rest
  run at the next poll), each string cut to 500 characters. The run is:
  - unattended like a schedule: `source: "watch"`, no approval callback (no shell, writes only if pre-approved).
  - tainted: the items are third-party input (`untrusted_source: "watch:<name>"`, so `run_start` records
    `untrusted_inputs: ["watch:slabbed"]`), and the gate treats the run like one that read a web page.
  - given the configured MCP apps, like a scheduled run.
  - on a scheduled run's model: the default purpose, like a schedule. On the Claude Code engine, `source: "watch"`
    picks the scheduled-run model (`run.engine_purpose()`).
- **The answer** goes to the owner through the notifier (Telegram when paired, else the inbox), headed `👀 <name>`.
  An answer of exactly `NO_REPLY` sends nothing.
- **Marked seen only after the run answered.** A run that fails or is refused leaves its items new, so the next poll
  tries them again. After 3 failed runs on an item it is marked seen anyway, with a `watch_error` saying so.
- **Errors retry at the next interval**, not every minute: the poll time is recorded either way.

## Events (between runs)

| Event | Payload | When |
|---|---|---|
| `watch_checked` | `name`, `items`, `new`, `status` (`baseline`, `ok`, `new`, `error`), `ms` | when status, item count or new count changes, else every 24th poll, so a quiet watch doesn't flood the log |
| `watch_fired` | `name`, `new`, `run_id`, `outcome` | new items ran the agent |
| `watch_error` | `name`, `reason` | the feed couldn't be fetched or read, a run on new items failed, or items were given up on |

## The command

- `wraithd watch` (or `wraithd watch list`): each watch with its settings, last poll and status, seen count and
  last error.
- `wraithd watch now <name>`: one poll now, due or not. It makes a model call only if the feed has new items.

## Cost

| Poll | Model tokens |
|---|---|
| baseline (the first) | 0 |
| quiet (nothing new) | 0: one HTTP GET |
| new items | one run (the same as a scheduled run's) |

A 15-minute watch is 96 GETs a day. If those ran the agent every time, that's about 96 runs a day.

## Tests

`tests/daemon/test_watch.py`, offline: an `httpx.MockTransport` serves a fixture with the feed's shape behind the
real egress checks, and the model is scripted. The tests cover: the baseline runs nothing; quiet polls make 0 model
calls; a new card runs once with only that item; the items are tainted; NO_REPLY sends nothing; fetch and parse
errors become `watch_error` and retry; failed runs retry and then give up; composite keys; trimming; intervals and
quiet hours; the config checks.
