"""
NO_REPLY: an answer that delivers nothing, on every channel. A run that already messaged the owner with notify_owner
(or a schedule or watch that found nothing) ends with exactly NO_REPLY, so no recap follows the message itself.

- Telegram sends nothing; the terminal chat prints nothing; the web API's done event has answer "" and
  "silent": true; schedules and watches deliver nothing.
- The conversation keeps no assistant message for it (conversation.py, terminal.py): NO_REPLY never reaches the
  history, and an empty assistant message would be refused by some providers.
- Chat answers stream, so hold_back() keeps text back while it could still become NO_REPLY, and lets it through
  as soon as it can't.
"""

NO_REPLY = 'NO_REPLY'


def is_silent(answer: str | None) -> bool:
    """True when the answer is exactly NO_REPLY (surrounding whitespace aside)."""
    return (answer or '').strip() == NO_REPLY


class HeldBack:
    """
    Wraps a run callback: content_delta events wait while the text so far (since the last other event) could still
    be NO_REPLY, and go through, in order, once it can't. Any other event (a tool call, an approval) lets the waiting
    text through first. finish(answer) at the end of the run sends what still waits, unless the answer is silent.
    """

    def __init__(self, callback):
        self.callback = callback
        self.waiting: list[dict] = []
        self.through = False  # this stretch of text already can't be NO_REPLY: the rest streams as it comes

    def _text(self) -> str:
        return ''.join(data['content'] for data in self.waiting).lstrip()

    async def _release(self) -> None:
        waiting, self.waiting = self.waiting, []
        for data in waiting:
            await self.callback('content_delta', data)

    async def __call__(self, event_type: str, data: dict) -> None:
        if event_type == 'content_delta':
            if self.through:
                return await self.callback(event_type, data)
            self.waiting.append(data)
            if not NO_REPLY.startswith(self._text().rstrip()):
                self.through = True
                await self._release()
            return
        self.through = False
        if self.waiting:
            await self._release()
        await self.callback(event_type, data)

    async def finish(self, answer: str | None) -> None:
        if is_silent(answer):
            self.waiting = []
        else:
            await self._release()


def hold_back(callback) -> HeldBack | None:
    return HeldBack(callback) if callback is not None else None
