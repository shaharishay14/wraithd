# Security

**Files:** `core/gate.py`, `core/egress.py`, `core/secrets.py`, `core/stop.py`

## One-liner

> Prompts can be talked out of anything, so every rule that must hold lives in code: what a tool may do, what
> the agent may do after reading someone else's text, where it may connect, what it may see, and how much it
> may spend.

## The permission gate

Every tool call passes through `PermissionGate` before it runs (`guard_function_tools` wraps each SDK tool;
a tool that can't be wrapped is dropped, never passed through):

- **Read** runs freely. **Write** needs the owner's approval. **Destructive or unknown** never runs.
- **Approvals** are rows in the store: pending, then approved, denied or expired after 60 seconds. Only a
  pending row can be decided, so a late click can't overturn an expiry. The gate polls the store rather than
  memory, so the decision can come from another process (the CLI while the daemon runs).
- **Unattended runs** (a schedule firing, nobody watching) may only run writes the owner pre-approved for that
  tool; each one is logged in the same approvals table, so there's one audit trail.
- **Taint tracking.** Once a third-party source (an email, a page, an MCP tool's output) enters the run, the
  model's requests may be steered by someone else. From then on, a pre-approved write may only go to
  recipients the owner wrote themselves; an action whose recipient can't be checked needs approval. In a live
  conversation, the approval card names the recipients the owner never wrote, and where they came from.
- **Links.** After the run reads private data, only links the owner wrote open freely, so an attacker can't
  make the agent "visit" a URL that spells out the data.
- Third-party output is wrapped in `<untrusted_tool_output>`; the wrapper can't be closed from inside, and the
  reminder tag is neutralized so a page can't pose as the harness. That's a hint for the model; the taint
  rules above are the defense.
- **MCP tool descriptions are third-party text too.** Given to the model directly, they taint the run from its
  first step (`MCP tool descriptions`); in discovery, `describe_app_action` marks its result untrusted.
- **The review queue.** What the agent would keep from a tainted run (a memory write, a fact distill can't trace to
  the owner) waits for the owner's approval instead ([review queue](review-queue.md)).
- **Always ask** (`always_ask`): a write that is never pre-approved, whatever the config says, and never runs
  unattended. MCP tools the server annotates `destructiveHint` get it ([MCP](mcp.md)).

## Egress: no way back into the machine

`read_website` ([the web](web.md)) opens URLs the model chose, and the model's URL can come from an email. Every hop of every fetch
is checked: http(s) on default ports, no `user:pass@`, every resolved address public, redirects followed by
hand and re-checked, the connected peer checked again before the body is read, 2 MB cap.

## Secrets never reach the model

Key and token shapes, and the owner's own keys by exact value, are replaced with `[REDACTED:kind]` in tool
results before the model sees them, in every string the event log stores, and (Phase 2) in memory writes.
It's a pattern scan, so it's best effort.

## The built-in tools

- **Files** are confined to `workspace/files/` by a real-path check ([files](files.md)).
- **The shell** is off by default; on, every command needs a "y", and it never runs unattended ([shell](shell.md)).
- **Schedules** are approved one by one, since each is a standing instruction ([schedules](schedules.md)).
- File contents, command output, pages and search snippets are all third-party content: wrapped, and they taint
  the run.

## The local API

Every website the owner visits can make their browser call `127.0.0.1`. The daemon's API needs a token (handed to
the UI in a URL fragment), checks Host (no DNS rebinding) and Origin, sends no CORS headers, and binds to 127.0.0.1
only ([api](api.md)).

## Checking the setup

`local-agent audit` checks the live setup against these rules: the state folder private and nothing in it
writable by others, the API bound to loopback with a long, private token, the shell off or gated, no keys in
`config.toml`. It lists standing permissions (always-allowed tools, schedules) as info, and
exits 1 on a failure. `doctor` reports it in one line. Why each rule exists is in [From EC2 to
laptop](ec2-to-laptop.md).

## Budgets

40 tool calls and 200,000 tokens per run, checked before every model call. An optional daily token budget,
checked before every call too, and before the run starts, so a spent budget costs no model call at all. The
tokens are what the provider reports. Every run asks for usage on the stream (`include_usage`), since LiteLLM
reports none for Anthropic otherwise, and the budgets would count zero.

## On a laptop

- **The inside of the network changed.** On EC2 the target was `169.254.169.254`, the metadata service that
  hands out credentials. On a laptop it's everything that trusts the machine: localhost (the agent's own API,
  dev servers, Ollama on port 11434), the home or office LAN (the router, printers, a NAS), and link-local
  addresses. The same rule ("every address must be public, on every hop") already blocks all of them; the
  tests now say so explicitly.
- **`requests` became `httpx`**, the stack's HTTP client. The peer address after connecting comes from
  httpx's network stream instead of urllib3's socket.
- **Server secrets became the owner's secrets.** agent-studio redacted its Django settings keys by value.
  Here the owner's keys come from the keychain or the environment: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` and
  `TELEGRAM_BOT_TOKEN` are read from the environment, and anything else the process loads (keychain keys, MCP
  tokens) is added with `register_secret`.
- **No Composio.** The verb rules that classify `APP_VERB_OBJECT` names stay as the fallback, but the
  Composio-specific overrides are gone. MCP tools get tiers from their own annotations instead ([MCP](mcp.md):
  read-only tools run, others ask until the
  owner marks them "always allow"). Every tool that isn't built in still counts as third-party output and
  private data.
- **No traces leave the machine.** The Agents SDK uploads run traces to OpenAI by default; every `Runtime` turns
  that off when it's built, so chat, the daemon, schedules and the evals never send them.
- **The owner is identified by config, not a user row.** `owner_email` and the standing instructions
  (`AGENTS.md`) are passed to the gate; recipients found there count as written by the owner.

## What's honest to say

- A misclassified write would be asked about, and an unknown verb is denied, but the verb rules can be wrong.
- The peer check after connecting happens after the request is sent. A DNS answer that changes between the
  check and the connection still can't return data to the model, but the request was made.

Source: [psst](https://michaellivs.com/blog/psst-secrets-for-agents/), [A thousand ways to sandbox an agent](https://michaellivs.com/blog/sandbox-comparison-2026/), [The Claw ecosystem](https://michaellivs.com/blog/personal-ai-agents-compared/).
