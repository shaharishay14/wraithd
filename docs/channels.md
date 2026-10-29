# Channels

**Files:** `channels/base.py` (the interface and the shared rules), `channels/telegram.py` (the first channel),
`channels/conversation.py`

## One-liner

> A channel is a transport: it turns its service's updates into `Incoming` and `Decision`, and implements four
> calls. Pairing, the owner check, ignoring strangers, attachments and approvals live once, in `Channel`, so a
> second channel (WhatsApp, [the options](whatsapp.md)) can't get them wrong.

## The interface

```python
class Channel:
    name = 'telegram'                 # settings prefix (telegram.owner_chat ...), conversation kind, run source
    title = 'Telegram'                # how replies name the service ("download from Telegram")
    ignored_event = ev.TELEGRAM_IGNORED

    # the transport: each channel
    async def send(self, chat, text, choices=None) -> where   # choices: [Choice(label, data)]
    async def fetch(self, attachment, limit) -> bytes          # FileTooLarge / ChannelError
    async def acknowledge(self, decision, text) -> None        # "Approved", "Only the owner can decide."
    async def close_prompt(self, where, status) -> None        # replace the choices with the outcome

    # shared
    async def receive(self, Incoming(chat, who, text, caption, attachment))
    async def decide(self, Decision(chat, data, ref, who))
```

- **`receive`**: `/pair <code>` pairs. Any chat but the owner's is ignored (no run, no model call, no reply, no
  download) and logged at most once an hour by chat id and name, never the text. `/start` gets a short hello. The
  rest is a run in the chat's conversation, in its own task, so choices keep being handled while it runs.
- **Attachments** (`Attachment(kind, ref, size, name, mime)`): a `photo` is image input, or is saved to the inbox
  when the model can't see. A `voice` note is transcribed through `voice.py`. A `file` is saved to
  `workspace/files/inbox/` with the same checks as `files_write`. Sizes are checked against what the service
  declares before downloading, and again while reading.
- **Approvals**: `approval_prompt(data)` gives the card's text and two choices, `a:<id>` and `d:<id>`. A channel
  with buttons shows them as buttons; a channel without them would number them. `decide` takes a choice only from
  the paired owner's chat. Anyone else is refused and logged as ignored, and nothing changes in the approval.
- **Pairing** codes are per channel (`base.new_pairing(store, name)`): 6 digits, 10 minutes, five tries.

## Telegram on it

`TelegramChannel` maps the Bot API onto the interface:

- `handle(update)` builds the `Incoming` from a message. For a photo it picks the largest size under 5 MB.
- A `callback_query` becomes a `Decision`.
- `send` turns choices into an inline keyboard. `fetch` is `getFile` plus the download, `acknowledge` is
  `answerCallbackQuery`, and `close_prompt` is `editMessageReplyMarkup`.

Its names, settings keys, replies and limits are unchanged, and `tests/daemon/test_telegram*.py` pass as they were.
The one addition: a stranger's button tap is now logged as `telegram_ignored`, like a stranger's message.

## Tests

`tests/reach/test_channel.py` runs the shared rules on a channel with no service at all (an in-memory transport):

- pairing
- a stranger's text and file ignored, with no model call
- the owner's run and file
- an approval that a stranger can't decide and the owner can

It also checks that a stranger's Telegram tap is refused and logged.
