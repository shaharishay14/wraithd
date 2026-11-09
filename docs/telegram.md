# Telegram

**Files:** `channels/telegram.py`, `channels/base.py` (the rules every channel shares: [channels](channels.md)),
`voice.py` (transcription)

## One-liner

> The owner talks to their agent from their phone through their own Telegram bot. One chat is paired as the
> owner's. The bot is personal: anyone else who finds it gets silence, unless the owner turns on answering them.

## How it works

- **Setup:** create a bot with BotFather, then `wraithd key set telegram` (the token goes to the keychain and is
  registered for redaction). The daemon polls for updates with `getUpdates` over httpx: no public URL, no webhook,
  no extra library.
- **Pairing:** `wraithd telegram pair` (or the web UI) shows a 6-digit code valid for 10 minutes. Sending
  `/pair <code>` to the bot makes that chat the owner's. Five wrong tries and the code is gone; a new code replaces
  the old one; a used code is gone.
- **The owner's messages** are runs like the terminal's: memory, files, schedules, apps. A write shows a card with
  the arguments, what approving means and the third-party warnings, and **Allow** / **Deny** buttons. A tap decides
  it in the same approval store the terminal and the web UI use; the buttons are replaced by the outcome. Only the
  owner's taps count.
- **Busy:** while a run answers the owner, the chat shows "typing…" (`sendChatAction`, again every 4 s since it
  lasts 5), until the answer is sent or the run fails. A failed `sendChatAction` is only logged.
- **`telegram_sent`:** each answer and each notify that reaches Telegram is logged as a between-runs event:
  `kind` (answer or notify), `run_id` (None for a notify), `ms` from the run's end (a notify: from its send) until
  Telegram replied, `chunks`, `parse_mode` (`HTML` or None) and `fallback` (a part went again as plain text). No
  message content. So the time after a run is measurable with `wraithd events`.
- **No recap:** when a run already messaged the owner with `notify_owner`, it ends with exactly `NO_REPLY` and
  nothing more is sent (core/silent.py, the same on every channel). The conversation keeps no assistant message
  for it.
- **Photos** go to the model as image input with their caption (the run's message is `[photo] <caption>`, or
  `[photo, no caption]`): the largest size Telegram offers under 5 MB, as a JPEG data URL in an `input_image` part,
  which the Agents SDK turns into OpenAI's or Anthropic's (through LiteLLM) own image part. The event log and the
  conversation keep only the text, so later messages remember that a photo came, not the photo. The owner's photo
  is the owner's input, not third-party content. If the model can't see images (from LiteLLM's model list: most
  Ollama models can't; a model the list doesn't know is assumed to see for OpenAI and Anthropic), there's no run:
  the photo is saved to the inbox and the reply says so.
- **Voice notes** are transcribed and the transcript is the message, marked `[voice note, transcribed]`. Which
  provider does it is `[voice] transcribe`:

  | Setting | What runs | Cost |
  | --- | --- | --- |
  | `auto` (default) | `openai` if its key is set, else `whisper` if installed, else off | |
  | `openai` | gpt-4o-mini-transcribe, on your OpenAI key | about $0.003 per minute |
  | `whisper` | faster-whisper on this computer; `uv tool install --reinstall "wraithd[whisper]"`, the `base` model downloaded on first use | free |
  | `elevenlabs` | ElevenLabs Scribe v2, `wraithd key set elevenlabs` | about $0.004 per minute on your plan |
  | `""` | off | |

  When it's off, the reply says why and how to turn it on; `wraithd doctor` shows the active provider and why.
  A failed transcription is said plainly (a rejected key, a rate limit), never with the key.
- **Files** (documents, audio files, videos, anything else) are saved to `workspace/files/inbox/<date>-<name>`:
  the name is Telegram's, made safe (no folders, no leading dot, plain characters, 100 at most), and a name that's
  taken gets `-2`, `-3`... instead of replacing a file. The save goes through the same checks as `files_write`
  (the folder must resolve inside the workspace, checked again after opening, created with `O_EXCL` and
  `O_NOFOLLOW`). The run's message is the caption plus a line saying where the file is, so the agent can
  `files_read` it; what it reads there is third-party content, as for any document.
- **Downloads are capped:** 20 MB (the Bot API's own limit for bots), 5 MB for photos, checked against the size
  Telegram declares before downloading and again while reading. Too large gets a reply, not a run.
- **Anyone else's messages are ignored**, always: no run, no model call, no reply, nothing stored but a log
  line. The one exception is `/pair <code>`, since the owner's first contact comes from a chat that isn't paired
  yet (before pairing, every chat is a stranger; `/start` gets no reply either). A stranger's photo, voice note or
  file is never downloaded. Silence costs no tokens and tells
  a stranger nothing, not even that the bot is a wraithd.
- **Ignored chats are logged** as a `telegram_ignored` event (between runs) with the chat id and username, never the
  text: the words are a stranger's and the log is the owner's. At most one event per chat per hour, so someone
  writing all day can't flood the log. The hour is counted in memory: a daemon restart logs each chat once more.
- **There is no switch to answer strangers.** Earlier versions had `[telegram] answer_strangers`; the owner decided
  the bot is never for anyone else, so it's gone. A config that still has the line loads (a warning says it does
  nothing) and strangers stay ignored.
- **Button taps** from any chat but the owner's are refused ("Only the owner can decide.") and the chat is logged
  as `telegram_ignored`, within the same once-an-hour limit.
- **Messages to the owner** (`notify_owner`, a schedule's answer, the heartbeat) go to the paired chat and are
  also kept in the **Notifications** inbox, which the web UI lists: sent, failed (stored once, with a warning in the
  log) or with no paired chat. The heartbeat's "Already told them lately" reads that inbox, so it doesn't repeat
  what a schedule just said on Telegram.
- **`/mcp_add`** adds an MCP server from the phone, with `wraithd mcp add`'s arguments, e.g.
  `/mcp_add notes --provides notes -- npx -y some-notes-mcp`. The bot replies with a card showing the exact command
  (or URL) and package; Allow writes it, connects once, pins its tools and lists them. Only the owner's own typed
  message counts: a stranger's is ignored as usual, a forwarded one is refused, and the model has no way to call it.
  Secrets never go in the chat: give names (`--env KEY`, value in the server's env file over SSH) or 1Password
  references (`--env KEY=op://vault/item/field`); a value or anything shaped like a token is refused, stored
  nowhere, and the message is deleted. Details in [MCP](mcp.md#adding-a-server-from-telegram-mcp_add).
- **Formatting:** the model writes Markdown, and replies (and `notify_owner`, schedules, the heartbeat) go as
  Telegram HTML (`parse_mode=HTML`): bold, italics, strikethrough, inline code, code blocks, links (http, https,
  mailto, tg only), quotes; headings become bold lines, bullets `•`, tables a monospace block. Everything else is
  escaped (`&`, `<`, `>`), and every tag the converter writes is closed on the same line, so the HTML is always
  balanced. A long reply is split at line ends (4,000 characters of Markdown, smaller if escaping pushes the HTML
  past Telegram's 4,096), never inside a tag; a code block that has to be split is closed in one message and
  opened again in the next. If Telegram still refuses a part's entities ("can't parse entities"), that part is
  sent again as plain text; any other error isn't retried. Approval and `/mcp_add` cards go plain, with no
  parse mode, so their arguments show exactly as they are. Text with no Markdown goes plain too.
- **Quick actions** (the "/" menu): `[telegram.commands]` in config.toml, one `name = "message"` per line. Without
  the table, five are on:

  ```toml
  [telegram.commands]
  news = "run news-feed"
  drafts = "slabbed drafts"
  jobs = "find jobs"
  watchlist = "show my watch list"
  review = "weekly review"
  ```

  Setting the table replaces these. The daemon registers them with `setMyCommands` when polling starts, and the
  owner's `/news` (typed, from the paired chat) is sent as `run news-feed`, exactly as if typed; `/news tech` sends
  `run news-feed tech`. A name must be a-z, 0-9 and `_`, at most 32; `/pair`, `/mcp_add` and `/start` keep their
  meaning. A line that breaks these is skipped with a warning. Anyone else's `/news` is ignored like any message.
- **Draft buttons**: a notification whose first line is a draft card (`✍️ D2 · reply`, the slabbed-x format) gets
  **Post D2 · Rewrite · Skip**, fixed in code (the model never picks buttons). Post sends `post D2` and Skip sends
  `skip D2` through the same path as typed text, so a destructive `create_post` still shows its approval card.
  Rewrite asks "How should I rewrite D2?", and your next message within 5 minutes is sent as `rewrite D2 <it>` (any
  `/command` cancels it). The button's data is only `draft:2:post` and the like; anything else, or a tap from any
  chat but yours, does nothing (and the other chat is logged as ignored).
- **Errors** never include the API URL or the file download URL, since the bot token is in both.

## On a laptop

agent-studio linked Telegram through a webhook to a public server. A laptop has no public address, so the daemon
asks Telegram for updates instead (long polling), which works from behind any home router. The bot is the owner's
own: Telegram sees the messages, so the agent's answers are only as private as Telegram is, and the owner decides
what to ask it there.

## Limits

A stranger costs one `getUpdates` result and, at most hourly, one log row: never a model call, so nobody who finds
the bot's name can spend the owner's tokens. Ignored chats are listed by `wraithd events` and
`GET /api/events`.
