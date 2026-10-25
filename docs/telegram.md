# Telegram

**File:** `channels/telegram.py`

## One-liner

> The owner talks to their agent from their phone through their own Telegram bot. One chat is paired as the
> owner's; everyone else who finds the bot is a stranger whose words are third-party content.

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
- **Anyone else's messages** run with `owner=False` (no memory, files, schedules, apps or skill changes) and as
  third-party input: the run is tainted from its first step and addresses in the message don't count as the
  owner's. Their conversations are kept per chat, like the owner's.
- **Errors** never include the API URL, since the bot token is in it.

## On a laptop

agent-studio linked Telegram through a webhook to a public server. A laptop has no public address, so the daemon
asks Telegram for updates instead (long polling), which works from behind any home router. The bot is the owner's
own: Telegram sees the messages, so the agent's answers are only as private as Telegram is, and the owner decides
what to ask it there.

## Limits

Anyone who finds the bot's name can message it and spend the owner's tokens (the daily budget caps that). Whether
strangers should be answered at all, and rate limits for them, are Phase 7 decisions.
