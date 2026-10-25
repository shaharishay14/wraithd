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

The same pattern runs through every registry: memory (profile and keyword map in the prompt, then search,
then read), skills (one line each, then `skill_load`), the web (search snippets, then a page in parts).

## Why it stays safe

`run_app_action` calls the same guarded tool a direct call would. The permission gate, approvals, the
untrusted-output wrapper and the event log all see the real action (`GMAIL_SEND_EMAIL`), never just the
dispatcher.

## On a laptop

- **Apps are MCP servers, not Composio.** The catalog takes any guarded tools, and each app's guide is the MCP
  server's own instructions instead of agent-studio's hand-written Composio guide library. Find and describe
  show each action's tier as the run's gate sees it: read, needs approval, always allowed.
- **It matters more here.** With a few MCP servers connected, the tool list grows with every server; on a
  small local model the context and the tool choice both suffer sooner than on gpt-4o.
- **MCP tools are named `SERVER_TOOL`** (`GMAIL_SEND_EMAIL`), so the app is still the part before the first
  underscore, and the threshold counts every connected server's tools together.

Source: [Tool Design is All About the Flow](https://michaellivs.com/blog/tool-design-is-all-about-the-flow/), [Skills aren't the innovation](https://michaellivs.com/blog/skills-arent-the-innovation/), [Ergonomic coding surfaces](https://michaellivs.com/blog/ergonomic-coding-surfaces-for-agents/).
