# Telegram

**File:** `channels/telegram.py`

## One-liner

> The owner talks to their agent from their phone through their own Telegram bot. One chat is paired as the
> owner's. The bot is personal: anyone else who finds it gets silence, unless the owner turns on answering them.

## How it works

- **Setup:** create a bot with BotFather, then `local-agent key set telegram` (the token goes to the keychain and is
  registered for redaction). The daemon polls for updates with `getUpdates` over httpx: no public URL, no webhook,
  no extra library.
- **Pairing:** `local-agent telegram pair` (or the web UI) shows a 6-digit code valid for 10 minutes. Sending
  `/pair <code>` to the bot makes that chat the owner's. Five wrong tries and the code is gone; a new code replaces
  the old one; a used code is gone.
- **The owner's messages** are runs like the terminal's: memory, files, schedules, apps. A write shows a card with
  the arguments, what approving means and the third-party warnings, and **Allow** / **Deny** buttons. A tap decides
  it in the same approval store the terminal and the web UI use; the buttons are replaced by the outcome. Only the
  owner's taps count.
- **Anyone else's messages are ignored**, always: no run, no model call, no reply, nothing stored but a log
  line. The one exception is `/pair <code>`, since the owner's first contact comes from a chat that isn't paired
  yet (before pairing, every chat is a stranger; `/start` gets no reply either). Silence costs no tokens and tells
  a stranger nothing, not even that the bot is a Local Agent.
- **Ignored chats are logged** as a `telegram_ignored` event (between runs) with the chat id and username, never the
  text: the words are a stranger's and the log is the owner's. At most one event per chat per hour, so someone
  writing all day can't flood the log. The hour is counted in memory: a daemon restart logs each chat once more.
- **There is no switch to answer strangers.** Earlier versions had `[telegram] answer_strangers`; the owner decided
  the bot is never for anyone else, so it's gone. A config that still has the line loads (a warning says it does
  nothing) and strangers stay ignored.
- **Button taps** from any chat but the owner's are refused ("Only the owner can decide.").
- **Errors** never include the API URL, since the bot token is in it.

## On a laptop

agent-studio linked Telegram through a webhook to a public server. A laptop has no public address, so the daemon
asks Telegram for updates instead (long polling), which works from behind any home router. The bot is the owner's
own: Telegram sees the messages, so the agent's answers are only as private as Telegram is, and the owner decides
what to ask it there.

## Limits

A stranger costs one `getUpdates` result and, at most hourly, one log row: never a model call, so nobody who finds
the bot's name can spend the owner's tokens. Ignored chats are listed by `local-agent events` and
`GET /api/events`.
