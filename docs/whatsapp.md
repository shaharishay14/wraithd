# WhatsApp: the options

**Status:** a decision note. Nothing is built this phase (owner decision, 2026-10-29).

## One-liner

> WhatsApp would be a second phone channel next to Telegram, on the same channel interface
> ([channels](channels.md)) and the same rules. The hard part isn't the code: it's that WhatsApp has no
> long-polling bot API, so every option either needs a public URL or an unofficial client.

## The rules any option must keep

These are Telegram's rules ([telegram](telegram.md)), and the channel interface enforces them in shared code, not
per channel:

- **One paired owner.** A pairing code from `local-agent <channel> pair`, sent from the owner's phone. Five wrong
  tries and the code is gone.
- **Strangers are ignored.** No run, no model call, no reply, no download. One `<channel>_ignored` event per
  chat per hour, with the sender's id and name and never the text.
- **Approvals** are buttons where the channel has them, and numbered replies where it doesn't
  ("1 Allow, 2 Deny"). Only the owner's answer counts, and only for a pending approval.
- **Voice notes** go through `voice.py` transcription, marked `[voice note, transcribed]`.
- **Photos** become image input, and **files** are saved to `workspace/files/inbox/` through the same checks as
  `files_write`. What the agent reads there is third-party content.
- **Secrets:** the channel's token is in the keychain and registered for redaction. Errors never include it.
- **No public URL** (PLAN: everything is local). Remote access is tailnet-only ([remote access](remote-access.md)).

## The ban risk, plainly

Options 2 and 3 use an unofficial client. That's against WhatsApp's terms of service. **Meta can ban the number
it runs on, permanently, without warning and without appeal.** That means losing the WhatsApp account on that
number: its chats, its groups, and the number's use for WhatsApp. Bans are more likely for new numbers, for
numbers that send a lot, and when the protocol library is out of date. There's no setting that makes it safe.
The only real mitigation is to run it on a number the owner can afford to lose, never their personal one.

## What a stranger costs

On every option, a message from anyone but the paired owner costs **no model call and no tokens**:

- no run, no reply, no download
- at most one `<channel>_ignored` event per chat per hour (sender id and name only)

The shared code in `channels/base.py` enforces this before any model is involved, as it does for Telegram
today. What a stranger can still cost is outside the agent:

- **Cloud API:** Meta's webhook POST (free to receive). Because the bot doesn't answer, no conversation is opened,
  so there's no per-conversation charge.
- **Bridges:** one update through the bridge.

## Option 1: the official WhatsApp Business Cloud API

Meta hosts the number, and the daemon sends with `POST graph.facebook.com/v{N}/{phone-id}/messages`.

- **Setup:** a Meta business account, an app in Meta's developer console, a phone number that isn't on a
  personal WhatsApp account, a permanent system-user token, and a webhook URL with a verify token. Reaching
  real users needs business verification.
- **How messages arrive:** only by webhook. Meta POSTs every message to a public HTTPS URL. There is no polling.
- **Risks:**
  - It breaks the "no public URL" rule: something on the internet must accept Meta's POSTs. That could be
    Tailscale Funnel, a tunnel or a small relay, but each is a public endpoint the owner has to run and that
    anyone can hit.
  - Each POST must be checked with Meta's `X-Hub-Signature-256` (the app secret), or anyone can fake the owner.
  - The bot can only start a conversation inside the 24-hour customer-service window. Outside it, it needs
    pre-approved templates, so scheduled messages and `notify_owner` after a quiet day would fail or need a
    template.
  - Per-conversation pricing, and Meta's business-policy review.
- **On R1:** a `WhatsAppCloudChannel` whose `receive` is fed by a webhook route rather than a poll loop. It sends
  through the Graph API and downloads media through the media-id endpoint (a two-step fetch, with the token).
  Approvals use interactive reply buttons (up to 3). It's the only option with real buttons.
- **Test plan:** a fake Graph API on `httpx.MockTransport` (send, media URL, media download), plus signed and
  unsigned webhook POSTs into the ASGI app:
  - a bad signature is refused with no event
  - a stranger is ignored with no model call
  - pairing works
  - a button reply decides the approval
  - a reply outside the 24-hour window is reported, not dropped silently
- **Verdict:** the most legitimate option, but the wrong shape for a laptop agent. It needs a public URL and a
  business setup, and the 24-hour window breaks the scheduler's "the answer always arrives".

## Option 2: a WhatsApp Web bridge inside the daemon

An unofficial client speaks the WhatsApp Web protocol as a linked device of the owner's own number, the way
WhatsApp Desktop does. Examples are whatsmeow (Go), Baileys (Node) and neonize (Python bindings to whatsmeow).

- **Setup:** `local-agent whatsapp link` shows a QR code, and the owner scans it in WhatsApp → Linked devices.
  No Meta account and no public URL: the client keeps an outbound connection, as polling does.
- **Risks:**
  - Against WhatsApp's terms. Meta bans numbers that automate, and a ban hits the owner's personal number.
  - The protocol changes without notice, and the library breaks until it's updated.
  - The session keys stored on disk are as good as the owner's WhatsApp account. They'd need the keychain or a
    mode-600 file, and audit coverage.
  - In-process means a native or Node dependency inside our Python process (neonize ships a Go shared library).
    A crash or hang there takes the daemon with it.
  - Messaging "yourself" is awkward: the agent *is* the owner's number. The owner would talk to it in their own
    "Message yourself" chat, so pairing means "that self-chat", and every other chat is someone else's
    conversation the agent must never read, not just ignore.
- **On R1:** a `WhatsAppWebChannel` whose `receive` gets events from the library. Approvals are numbered replies
  ("1 Allow / 2 Deny"), since linked devices can't send real buttons reliably.
- **Test plan:** a fake library object (no network) that emits message events and records sends. Then the R1
  shared tests: pairing, stranger ignored with no model call and no download, a numbered reply deciding an
  approval, a "1" from a stranger refused.
- **Verdict:** no public URL, but the ban risk lands on the owner's own number, and the dependency lives in our
  process.

## Option 3: a bridge as its own process

The same unofficial client, but in a separate small process (a whatsmeow or Baileys bridge). It talks to the
daemon over loopback or a Unix socket.

- **Setup:** `local-agent whatsapp install-bridge` fetches a pinned bridge binary or package, and it runs as a
  second service next to the daemon (launchd/systemd, the same pattern as `daemon install`). Linking is the QR
  scan, shown through the bridge. Ideally the bridge uses a second number (a cheap SIM or eSIM), so a ban costs
  nothing personal and "the owner's chat" is an ordinary chat, as on Telegram.
- **How it connects:** the bridge exposes a tiny local HTTP API on 127.0.0.1 (or a Unix socket, mode 600):
  `GET /updates?offset=` (long poll), `POST /send`, `GET /media/{id}`. That's deliberately the shape of the
  Telegram Bot API, so the daemon side is a polling channel like Telegram's. The bridge needs its own token, from
  a file it creates at mode 600, which the daemon reads.
- **Risks:**
  - The same ban risk and terms problem as option 2, which a second number contains.
  - It's one more process to install, update and watch: `daemon status` and `doctor` would show the bridge.
  - The bridge sees every chat on its number. With a dedicated number there are only the owner and strangers.
  - The bridge itself is third-party code with the session keys. Pin a version and hash, and keep it off the
    network except WhatsApp's servers.
- **On R1:** a `WhatsAppBridgeChannel` that's nearly a copy of Telegram's transport, with `send`, `download` and
  `close_prompt` over loopback HTTP. Everything else is shared code: the owner check, pairing, the ignore log,
  approvals as numbered replies, voice and files. A crash in the bridge is a channel outage, not a daemon crash.
- **Test plan:**
  - A fake bridge on `httpx.MockTransport`, with the same fixtures as `tests/daemon/test_telegram.py`: pairing
    (right, wrong, expired, five tries), a stranger with no run, model call, reply or download, one ignored event
    per hour, a numbered approval reply from the owner and from a stranger, a voice note to transcription,
    a file to the inbox, oversized media.
  - Error handling: the bridge token is never in an error, a bridge that's down is a warning plus a retry.
  - Attack tests: a forged update with the owner's id from an unauthenticated caller is refused, because the
    bridge token is required.

## Side by side

| | Cloud API | Web bridge in the daemon | Bridge as its own process |
|---|---|---|---|
| Public URL | yes (webhook) | no | no |
| Meta account | business + verification | no | no |
| Terms / ban risk | none | high, on the owner's number | high, contained by a second number |
| Proactive messages (schedules) | templates only, after 24 h | yes | yes |
| Approvals | real buttons | numbered replies | numbered replies |
| Dependency in our process | none (httpx) | native or Node library | none (httpx) |
| A crash in the WhatsApp side | n/a | takes the daemon down | a channel outage |
| Code on top of R1 | webhook route + signature check + channel | channel + library glue | channel (Telegram-shaped) |

## Pick

**Option 3: a bridge as its own process, on a second number.** It keeps the no-public-URL rule. It keeps
schedules and `notify_owner` working at any hour. It keeps unofficial protocol code out of our process. And on
our side it's a Telegram-shaped polling channel on the R1 interface, so the security rules stay in shared code.
The cost is the terms of service. That's the owner's call to make knowingly, and why the docs should say "use a
second number". If the owner won't accept any ban risk, the Cloud API is the fallback, behind Tailscale Funnel
with signature checks, and with the documented loss of messages outside the 24-hour window.
