# Progressive disclosure

**File:** `core/disclosure.py` (memory, skills and the web join in Phases 2 to 4)

## One-liner

> The model starts with a small prompt and a map of what exists, then pulls in details when it needs them:
> search, then view, then use. The order is enforced in code; the prompt only explains it.

## App actions: find → describe → run

Past a dozen app actions, their schemas stay out of the tool list. The model gets three tools instead:

- `find_app_actions(query)`: a short list (action, app, one-line summary, access). No schemas, no scores.
- `describe_app_action(action)`: one action's arguments, plus the app's guide the first time it's needed.
- `run_app_action(action, arguments)`: runs it, only if it was described in this run, and only with the
  required arguments present and no unknown ones. A rejected call is logged as a failed tool call, so the stop
  conditions count it.

Up to 12 actions, the schemas go in directly: discovery costs round trips, and with a handful of tools the
schemas are cheap. The run (Phase 2) records which mode it used and the size of its tool definitions, so the
choice can be measured.

### Recently used actions skip the search

The owner's runs keep asking the same apps the same things. In discovery mode, the actions the owner's runs called,
and the gate allowed, in the 7 days before today are also offered directly: their schemas are in the tool list, the
model calls them by name, and `run_app_action` takes them without a describe. Find, describe and run stay for the
rest. A calendar question goes straight to the calendar action: two or three turns instead of six.

- **At most 10, most used first** (`[mcp] recent_actions = 10`; `0` turns it off). Only actions this run has (after
  `allowed_tools`), only the owner's runs (a run with `owner=False` gets none), and a call the gate refused doesn't
  count. One indexed query over the event log per run (`Store.tools_used`).
- **The same all day.** The window is whole days, from local midnight a week ago to this midnight, so an action
  used today is offered from tomorrow. The tool list doesn't change within a day: the cached prefix holds, and
  the warm Claude Code session (which restarts when its tool names change) restarts at most once a day, on the
  first message after midnight, and only if the set changed.
- **They taint the run, like direct mode.** Their descriptions are third-party text in the tool list from the
  first step, so the run adds them (only theirs) as untrusted input, not private data: `context_built` shows
  `untrusted_context: ["MCP tool descriptions"]` and `recent_actions`.
- **Cost:** each action's schema on every call, cached after the first. A calendar read and a Gmail search
  measure 501 tokens per call (`tests/efficiency/test_recent_actions.py`, which holds the budget); one saved
  find + describe round trip is two model calls with the whole prompt.

The same pattern runs through every registry: memory (profile and keyword map in the prompt, then search,
then read), skills (one line each, then `skill_load`; skills that are off get one short line saying what turns
them on, see [Skills](skills.md)), the web (search snippets, then a page in parts).

The prompt's size has a budget, checked by `tests/efficiency/test_prompt_size.py` on an empty home and on a home
with four owner skills (`core/tokens.py` counts): a change that grows it much fails and says by how much.

## Why it stays safe

`run_app_action` calls the same guarded tool a direct call would. The permission gate, approvals, the
untrusted-output wrapper and the event log all see the real action (`GMAIL_SEND_EMAIL`), never just the
dispatcher.

A connected app's descriptions are written by whoever runs its server, so `describe_app_action` marks its result
untrusted when the action is an MCP tool: the gate wraps it and the run is tainted from then on. `find_app_actions`
summaries (one capped sentence each) don't taint; an action can't run without being described first.

## On a laptop

- **Apps are MCP servers, not Composio.** The catalog takes any guarded tools, and each app's guide is the MCP
  server's own instructions instead of agent-studio's hand-written Composio guide library. Find and describe
  show each action's tier as the run's gate sees it: read, needs approval, always allowed.
- **It matters more here.** With a few MCP servers connected, the tool list grows with every server; on a
  small local model the context and the tool choice both suffer sooner than on gpt-4o.
- **MCP tools are named `SERVER_TOOL`** (`GMAIL_SEND_EMAIL`), so the app is still the part before the first
  underscore, and the threshold counts every connected server's tools together.

Source: [Tool Design is All About the Flow](https://michaellivs.com/blog/tool-design-is-all-about-the-flow/), [Skills aren't the innovation](https://michaellivs.com/blog/skills-arent-the-innovation/), [Ergonomic coding surfaces](https://michaellivs.com/blog/ergonomic-coding-surfaces-for-agents/).
