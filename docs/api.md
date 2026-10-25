# The local API (contract v1.1)

**Files:** `daemon/api.py`, `daemon/security.py`, `daemon/app.py`

The daemon (`local-agent daemon`) serves this API and the web UI from one address: `http://127.0.0.1:7878`
(port set by `[daemon] port`). It's for the owner's own browser on this machine and nothing else. Changes to this
contract go through the coordinator; this file is the source of truth for the frontend.

**v1.1** (from the frontend's first build): the UI uses hash routes; `GET /api/approvals?status=all` or a comma
list; `run_id` on conversation messages; `approval_resolved` is sent for every outcome, whoever decided; and the
answers on an unconnected MCP server's tools (`409`) and a PUT to a new memory path (it creates the note).

## Auth: the token

- On first start the daemon creates a random token in `~/.local-agent/api-token` (mode 600).
- `local-agent daemon` prints the UI's address with the token in the URL fragment:
  `http://127.0.0.1:7878/#token=<token>`. A fragment is never sent to a server, so it doesn't end up in logs.
- The UI reads `location.hash`, keeps the token (`localStorage`, key `local-agent-token`), removes it from the
  address bar (`history.replaceState`), and sends it on every API call: `Authorization: Bearer <token>`.
- No token, or a wrong one: `401`. The UI then shows "Open the link printed by `local-agent daemon`".
- The token never goes in a query string, and there are no cookies.

## Origin and Host (any website can call localhost)

Every request, including static files, is checked before anything else:

- **Host** must be `127.0.0.1:<port>` or `localhost:<port>`. Anything else (a DNS-rebinding name that resolves to
  127.0.0.1) gets `403 bad_host`.
- **Origin**, when the browser sends one, must be `http://127.0.0.1:<port>` or `http://localhost:<port>`. Anything
  else gets `403 bad_origin`. Requests without an Origin (curl, the CLI) still need the token.
- No CORS headers are ever sent, so a page from another origin can't read a response. Preflight `OPTIONS`
  requests get `403`.
- The daemon binds to `127.0.0.1` only, never `0.0.0.0`.
- For frontend development with a dev server, `[daemon] dev_origin = "http://localhost:5173"` allows exactly that
  one extra Origin. It's off unless set; the token is still required.

## Errors

Every error is JSON with an HTTP status:

```json
{"error": {"code": "not_found", "message": "There is no run 3f2a..."}}
```

| Status | `code` | When |
|---|---|---|
| 400 | `bad_request` | malformed JSON or a missing/invalid field (the message names it) |
| 401 | `unauthorized` | no token or a wrong one |
| 403 | `bad_origin` / `bad_host` | the checks above |
| 404 | `not_found` | no such run, note, skill, schedule, server, approval |
| 409 | `conflict` | already decided, conversation busy, name taken |
| 422 | `invalid` | understood but refused (e.g. a memory path outside the folders) |
| 500 | `internal` | a bug; the message is generic, details are in the daemon's log |

Times are ISO 8601 with an offset (UTC, `2026-11-06T08:00:00.000000+00:00`). Ids are strings, except schedule ids
(integers) and message ids (integers).

## Endpoints

All are under `/api`, JSON in and out (`Content-Type: application/json`), and need the token, except
`GET /api/health`.

### Health and status

`GET /api/health` → `{"ok": true, "version": "0.0.1"}` (no token; still Host- and Origin-checked)

`GET /api/status`
```json
{"version": "0.0.1", "model": {"provider": "openai", "default": "gpt-4o-mini", "strong": "gpt-4o"},
 "timezone": "Europe/London", "shell_enabled": false, "search_enabled": true,
 "telegram": {"configured": true, "paired": true},
 "mcp": [{"name": "gmail", "connected": true, "error": null}],
 "scheduler": {"running": true, "next": {"id": 1, "at": "2026-11-09T08:00:00.000000+00:00"}}}
```

### Chat

`POST /api/chat` with `{"message": "...", "conversation_id": "..."}` (`conversation_id` optional: none starts a
new web conversation). The response is a server-sent event stream (`Content-Type: text/event-stream`). Read it
with `fetch` and a stream reader, not `EventSource`, since it needs the Authorization header and a POST body.
Events, in order:

```
event: start
data: {"conversation_id": "c1...", "run_id": "r1..."}

event: delta                      (many; append content in index order)
data: {"content": "It is ", "index": 0}

event: tool                       (a tool started)
data: {"tool": "memory_search"}

event: approval_required          (show the card; decide with POST /api/approvals/{id})
data: {<approval card, below>}

event: approval_resolved          (the card's outcome, whoever decided it; remove or update the card)
data: {"approval_id": "a1...", "status": "approved"}

event: done                       (always last on success)
data: {"run_id": "r1...", "outcome": "answered", "answer": "It is 18°C in Paris."}

event: error                      (instead of done)
data: {"code": "internal", "message": "..."}
```

`approval_resolved` comes once per card, with `approved`, `denied` or `expired`, whether the decision came from
this UI (`POST /api/approvals/{id}`), another tab, a Telegram button, or the 60-second expiry.

`outcome` is `answered`, `refused` (daily budget) or `failed` (the `answer` then says what happened). Use `answer`
as the final text: it's complete even if a refusal or failure streamed no deltas. One message at a time per
conversation: sending while a run is going gets `409 conflict`.

`GET /api/conversations` → newest first:
```json
[{"id": "c1...", "source": "web", "started_at": "...", "last_message_at": "...", "preview": "What's my day?"}]
```
`source` is `web`, `terminal` or `telegram`. Telegram conversations are listed but read-only in the UI.

`GET /api/conversations/{id}/messages` →
```json
[{"id": 41, "role": "user", "content": "...", "created_at": "...", "run_id": null},
 {"id": 42, "role": "assistant", "content": "...", "created_at": "...", "run_id": "r1..."}]
```
`run_id` links an assistant message to the run that wrote it (`GET /api/runs/{run_id}`). It's `null` for the
owner's messages, for notifications, and for messages saved before v1.1.

### Approvals

The approval card, as in the chat stream and the list:
```json
{"approval_id": "a1...", "tool": "GMAIL_SEND_EMAIL", "arguments": {"to": "dana@co.com", "body": "..."},
 "reason": "",
 "suspicious_recipients": [{"value": "eve@evil.com", "source": "GMAIL_FETCH_EMAILS"}],
 "untrusted_sources": [],
 "status": "pending", "created_at": "...", "expires_at": "...", "channel": "web", "run_id": "r1..."}
```
- `reason`: what approving means, when the arguments don't say it (skill rules, shell, schedules). Show it.
- `suspicious_recipients`: recipients the owner never wrote, with where each came from. Show each one as a
  warning: "eve@evil.com was not written by you (it came from GMAIL_FETCH_EMAILS)".
- `untrusted_sources`: when the action has no recipient to check, the third-party sources read before it. Show
  "This run read third-party content first: ...".
- `status`: `pending`, `approved`, `denied`, `expired`, `preapproved`. `expires_at`: when an unanswered card
  expires (60 seconds after it was asked).

`GET /api/approvals?status=pending` → `[<card>, ...]`, newest first. `status` is optional (default `pending`): one
status, a comma list (`status=approved,denied`), or `all` for the whole history. Anything else: `400 bad_request`.

`POST /api/approvals/{id}` with `{"decision": "approve"}` or `{"decision": "deny"}` → the card with its new status.
Deciding one that's no longer pending: `409 conflict` (the card's current status is in the message).

### Runs

`GET /api/runs?limit=50` (max 200) → newest first:
```json
[{"run_id": "r1...", "started_at": "...", "source": "chat", "message": "What's my day?", "outcome": "answered",
  "turns": 3, "tool_calls": 2, "tokens": 5310, "stop_reason": null, "untrusted_sources": ["GMAIL_FETCH_EMAILS"]}]
```
`source` is `chat` (terminal or web), `telegram`, or `schedule`. `outcome` can also be `running`.

`GET /api/runs/{run_id}` →
```json
{"run_id": "r1...",
 "summary": {"source": "chat", "message": "...", "started_at": "...", "outcome": "answered", "duration_ms": 2310,
             "turns": 3, "tokens": 5310, "tool_calls": 2, "tool_errors": 0, "denied": 0, "stop_reason": null,
             "untrusted_sources": [], "reminders": ["untrusted_content"], "redactions": [], "tools_offered": 14,
             "tool_schema_chars": 6006, "instructions_chars": 1858, "model": "gpt-4o-mini", "model_upgrades": [],
             "disclosure": {"mode": "direct", "apps": null, "memory": "L2 search", "skills_loaded": [],
                            "web_pages_read": 0}},
 "events": [{"seq": 0, "type": "run_start", "at": "...", "payload": {...}}, ...]}
```
Event types and their payloads are listed in [the event log](event-log.md).

### Memory

`GET /api/memory` →
```json
{"folders": {"profile": "Who the user is...", "people": "...", "projects": "...", "preferences": "...",
             "decisions": "...", "routines": "..."},
 "notes": [{"path": "people/dana.md", "title": "Dana", "trust": "high", "source": "agent",
            "updated_at": "..."}]}
```

`GET /api/memory/{folder}/{name}` (e.g. `/api/memory/people/dana.md`) →
```json
{"path": "people/dana.md", "title": "Dana", "body": "- Designer at Acme ...", "trust": "low", "source": "agent",
 "source_detail": "run 3f2a..., after reading web_search", "updated_at": "...", "links": ["website-redesign"],
 "linked_from": ["projects/website-redesign.md"]}
```
Show a low-trust note's `source_detail` next to a warning: it was saved after third-party content.

`PUT /api/memory/{folder}/{name}` with `{"body": "...", "title": "..."}` (title optional) → `200` and the note.
A path that doesn't exist yet creates the note (its title defaults to the name: `projects/garden.md` is "Garden"),
so the UI's "new note" is a PUT too. Saving from the UI is the owner's own edit, so the note becomes high trust:
that's how the owner vouches for a note.
`DELETE /api/memory/{folder}/{name}` → `204`. A path outside the six folders: `422 invalid`.

### Skills

`GET /api/skills` →
```json
[{"name": "morning-brief", "description": "Today's agenda...", "source": "builtin", "requires": ["email"],
  "model": null, "available": false, "missing": ["email"], "files": [], "adjustments": ["Slack first"]}]
```
`available` says whether runs are offered the skill now; `missing` lists what's needed for it (connect a server
that provides it).

`GET /api/skills/{name}` → the same fields plus `"instructions": "..."` (the skill's own steps, without the rules).

`PUT /api/skills/{name}/adjustments` with `{"rules": ["Slack first", "No weather"]}` → the skill. Replaces the whole
list (an empty list removes them all); each rule is one line, at most 300 characters.

`GET /api/skills/proposals` →
`[{"name": "weekly-sales-digest", "description": "...", "instructions": "...", "requires": [], "proposed_by": "run 3f2a..."}]`

`POST /api/skills/proposals/{name}/approve` → the skill (now active). `409 conflict` if an active skill has that
name. `DELETE /api/skills/proposals/{name}` → `204`.

### Schedules

`GET /api/schedules?all=false` →
```json
[{"id": 1, "message": "Send me my morning brief", "cron": "0 8 * * 1-5", "at": null, "timezone": "Europe/London",
  "next_fire_at": "2026-11-09T08:00:00.000000+00:00", "last_fired_at": null, "active": true,
  "when": "every: 0 8 * * 1-5 (next Mon 2026-11-09 08:00 Europe/London)"}]
```
`DELETE /api/schedules/{id}` → `204` (cancels it; `404` if there's no active schedule with that id). Creating
schedules is the agent's job, with the owner's approval, so there's no POST.

### MCP servers

`GET /api/mcp/servers` →
```json
[{"name": "gmail", "transport": "stdio", "target": "npx -y some-gmail-mcp", "provides": ["email"],
  "always_allow": ["send_email"], "secrets": ["GMAIL_TOKEN"], "connected": true, "error": null}]
```
`secrets` lists names only; values never leave the keychain. Adding and removing servers stays in the CLI (it
handles secrets).

`GET /api/mcp/servers/{name}/tools` →
```json
[{"tool": "send_email", "registered_name": "GMAIL_SEND_EMAIL", "description": "Send an email...",
  "tier": "asks"}]
```
`tier` is `read-only`, `asks` or `always allowed`. A server that's configured but not connected (it didn't start,
or the daemon couldn't reach it) gets `409 conflict`, with the reason in the message
(`"gmail is not connected: ..."`); its entry in `GET /api/mcp/servers` has `connected: false` and the `error`.

`PUT /api/mcp/servers/{name}/always-allow/{tool}` → the server. `DELETE` the same path → the server (asks again).
Allowing a read-only tool changes nothing (they already run).

### Telegram

`GET /api/telegram` → `{"configured": true, "paired": true, "owner": "@shahar"}` (`owner` is the paired chat's
name, or null)

`POST /api/telegram/pairing` → `{"code": "483920", "expires_at": "..."}`. The owner sends `/pair 483920` to the
bot within 10 minutes; that chat becomes the owner's. A new pairing replaces the old one.

## The UI files

- The UI is a static single-page app. Its build output (`index.html` and an `assets/` folder) uses relative paths
  (`base: './'` in Vite) and calls the API at `/api/...`.
- The daemon serves the first of these that has an `index.html`: `$LOCAL_AGENT_UI_DIR`, then the `ui/` folder
  inside the installed package (`src/local_agent/ui/`, which a release build fills from `frontend/dist`). If
  neither exists, it serves a short placeholder page that says how to build the UI.
- The UI uses hash routes (`/#/runs/r1...`, `/#/memory`): with relative asset paths, a path route like `/runs/r1`
  would load `./assets` from `/runs/assets`. Any GET that isn't `/api/...` and isn't a file in the folder still
  returns `index.html`, which is harmless, but links should use the hash form.
- During development, build with `vite build --watch` and run the daemon with
  `LOCAL_AGENT_UI_DIR=frontend/dist`, so the UI and the API share one origin; or use a dev server and set
  `[daemon] dev_origin`.

## On a laptop

agent-studio's API sat behind a login, CSRF protection and a public hostname. Here there's no login and no
public hostname, but the threat is different: every website the owner visits can make their browser send
requests to `127.0.0.1`. The token stops a page from using the API, the Origin check stops it from even trying
with the owner's browser, the Host check stops DNS rebinding (a hostile name that resolves to 127.0.0.1), and no
CORS means no other page can read an answer. Binding to 127.0.0.1 keeps the rest of the network out.
