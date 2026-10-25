# MCP: your apps

**Files:** `mcp/config.py`, `mcp/client.py`, `mcp/commands.py`, `core/gate.py`, `core/disclosure.py`

## One-liner

> Apps (mail, calendar, notes...) come in as MCP servers the owner adds by hand. Their secrets live in the keychain
> and go only to their own server; their tools are discovered rather than listed, and each one is gated by what it
> does: read-only runs, anything else asks, until the owner says "always allow".

## How it works

- **Adding a server:** `local-agent mcp add gmail --provides email --env GMAIL_TOKEN -- npx -y some-gmail-mcp` for a
  local (stdio) server, or `--url https://.../mcp --header Authorization` for a remote (streamable HTTP) one. The
  block in `config.toml` names the env vars and headers; their values go to the keychain (asked for without echo
  if not given). `mcp list`, `mcp tools <name>` (connects and shows each tool's tier), `mcp remove <name>` (and
  its secrets).
- **Connecting:** with the Agents SDK's own clients (`MCPServerStdio`, `MCPServerStreamableHttp`), once per
  terminal session, or per run otherwise. A server that doesn't start is reported and left out; the run goes on.
  The daemon (Phase 5) will keep connections open.
- **Names:** a server's tools become `SERVER_TOOL` (`GMAIL_SEND_EMAIL`), so the disclosure catalog groups them by
  server, and the server's own instructions become its guide, shown with its first describe.
- **Discovery:** MCP tools are app tools: past a dozen in total, the model gets `find_app_actions` ->
  `describe_app_action` -> `run_app_action` instead of the schemas, so the prompt doesn't grow with each server
  (a one-line note names the connected apps). A handful are still given directly.
- **Gate tiers:** a tool the server annotates read-only runs freely. Anything else is a write, so the approval card
  asks, until the owner runs `local-agent mcp allow <server> <tool>` (undo with `mcp disallow`). An always-allowed
  tool runs without asking, but like any pre-approval, once the run has read third-party content it may only go to
  recipients the owner wrote; otherwise the owner is asked again (warned about the stranger's address), or, with
  nobody there, it's refused. Unattended runs only run read-only or always-allowed tools.
- **Taint:** every MCP result is third-party content (emails, messages, documents are written by other people):
  wrapped, tainting the run, and tracked as the owner's private data for the link rule.
- **Skills follow what's connected:** `--provides email` (or `calendar`, or any word) declares what a server gives.
  Skills declare what they need (`requires: [email]`), so the morning brief appears once a connected server
  provides email, and disappears when it's gone or fails to start.

## Secrets

Each server's secrets are read from the keychain when it connects and passed only to it: a local server's process
gets a minimal environment (`PATH`, `HOME`...) plus its own variables, never the owner's environment, so it sees
neither the model keys nor another server's token; a remote server's headers go on its requests only. Every value
is registered for redaction, so a server that echoes its token shows `[REDACTED:owner_secret]` to the model and
the log. The shell never has them: they're never in Local Agent's own environment.

## What a server says is third-party text

Tool names, descriptions and a server's instructions come from the server. They're capped, and the harness's own
markup (`<system-reminder>`, `<untrusted_tool_output>`) is neutralized in them, so a description can't pose as the
harness. The tier comes from the server's own annotation, which means trusting the server not to mark a send as
read-only: it's code the owner chose to run with their account. Phase 7 tests descriptions that carry instructions.

## On a laptop

agent-studio reached apps through Composio: one hosted service holding everyone's OAuth tokens, and a verb list to
guess what each of its thousands of actions does. Here the owner picks each server and holds each token, and the
server says itself whether a tool only reads. Composio can still be one MCP server among others, never required.
Remote servers use headers or bearer tokens for now; OAuth flows are a later decision.
