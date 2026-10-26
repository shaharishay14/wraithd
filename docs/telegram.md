# Telegram

**Files:** `channels/telegram.py`, `voice.py` (transcription)

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
  | `whisper` | faster-whisper on this computer; `uv tool install --reinstall "local-agent[whisper]"`, the `base` model downloaded on first use | free |
  | `elevenlabs` | ElevenLabs Scribe v2, `local-agent key set elevenlabs` | about $0.004 per minute on your plan |
  | `""` | off | |

  When it's off, the reply says why and how to turn it on; `local-agent doctor` shows the active provider and why.
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
  a stranger nothing, not even that the bot is a Local Agent.
- **Ignored chats are logged** as a `telegram_ignored` event (between runs) with the chat id and username, never the
  text: the words are a stranger's and the log is the owner's. At most one event per chat per hour, so someone
  writing all day can't flood the log. The hour is counted in memory: a daemon restart logs each chat once more.
- **There is no switch to answer strangers.** Earlier versions had `[telegram] answer_strangers`; the owner decided
  the bot is never for anyone else, so it's gone. A config that still has the line loads (a warning says it does
  nothing) and strangers stay ignored.
- **Button taps** from any chat but the owner's are refused ("Only the owner can decide.").
- **Errors** never include the API URL or the file download URL, since the bot token is in both.

## On a laptop

agent-studio linked Telegram through a webhook to a public server. A laptop has no public address, so the daemon
asks Telegram for updates instead (long polling), which works from behind any home router. The bot is the owner's
own: Telegram sees the messages, so the agent's answers are only as private as Telegram is, and the owner decides
what to ask it there.

## Limits

A stranger costs one `getUpdates` result and, at most hourly, one log row: never a model call, so nobody who finds
the bot's name can spend the owner's tokens. Ignored chats are listed by `local-agent events` and
`GET /api/events`.
