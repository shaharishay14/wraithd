"""
What every phone channel shares, so the rules live in one place and a new channel only writes its transport:

- Pairing: a 6-digit code (`local-agent <channel> pair`) the owner sends as `/pair <code>` within 10 minutes; that
  chat becomes the owner's. Five wrong tries and the code is gone.
- The owner's messages are runs; anyone else's get no run, no model call, no reply and no download, except
  `/pair <code>`. Each such chat is logged as the channel's ignored event (chat and name, never the text), at most
  once an hour per chat.
- Attachments from the owner: a photo is image input (saved instead when the model can't see), a voice note is
  transcribed (voice.py), any other file is saved to workspace/files/inbox/.
- Approvals are a prompt with choices (buttons, or numbered replies on a channel without them); only the owner's
  answer counts.

A channel subclass turns its service's updates into Incoming and Decision, and implements send, fetch,
acknowledge and close_prompt. Settings are stored under '<name>.<key>' (telegram.owner_chat ...).
"""

import asyncio
import base64
import json
import logging
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from local_agent import providers, voice
from local_agent.channels.conversation import reply
from local_agent.core import events as ev
from local_agent.core.gate import approval_card
from local_agent.store import base as store_base
from local_agent.tools import files

logger = logging.getLogger(__name__)

PAIRING_MINUTES = 10
PAIRING_TRIES = 5
IGNORED_LOG_EVERY = timedelta(hours=1)  # per stranger chat
MAX_FILE_BYTES = 20_000_000
MAX_PHOTO_BYTES = 5_000_000  # what goes to the model as an image (Anthropic takes up to 5 MB)
INBOX = 'inbox'  # in workspace/files/
ALLOW, DENY = 'a', 'd'  # a choice's data is '<a|d>:<approval id>'


class ChannelError(Exception):
    """A call to the channel's service failed. The message never includes a token or a URL with one in it."""


class FileTooLarge(ChannelError):
    """A download is over its cap."""


class Unusable(Exception):
    """An attachment that won't reach a run. The message is the reply the owner gets."""


@dataclass(frozen=True)
class Attachment:
    kind: str  # 'photo', 'voice' or 'file'
    ref: str  # the channel's own id to fetch it with
    size: int | None = None  # what the service declares, before downloading
    name: str = ''  # a file's name, '' when the service gives none
    mime: str = ''


@dataclass(frozen=True)
class Incoming:
    """One message, as the channel's service gave it."""

    chat: object  # the chat id, in the channel's own type
    who: dict = field(default_factory=dict)  # username / first_name, for the log and the owner's name
    text: str = ''  # the message text (commands live here)
    caption: str = ''  # an attachment's caption
    attachment: Attachment | None = None


@dataclass(frozen=True)
class Choice:
    label: str
    data: str


@dataclass(frozen=True)
class Decision:
    """Someone picked a choice (a button tap, a numbered reply)."""

    chat: object
    data: str
    ref: object = None  # what acknowledge() answers (a callback query id)
    who: dict = field(default_factory=dict)


def new_pairing(store, channel: str, now: datetime | None = None) -> dict:
    """A fresh pairing code for the channel, replacing any earlier one."""
    now = now or datetime.now(UTC)
    pairing = {'code': f'{secrets.randbelow(1_000_000):06d}',
               'expires_at': (now + timedelta(minutes=PAIRING_MINUTES)).isoformat(timespec='microseconds'),
               'tries': 0}
    store.set_setting(f'{channel}.pairing', json.dumps(pairing))
    return pairing


def approval_prompt(data: dict) -> tuple[str, list[Choice]]:
    """The card for an approval_required callback: tool, arguments, why, third-party warnings; Allow and Deny."""
    lines = [f'Allow {data["tool"]}?']
    lines += [f'  {key}: {value if isinstance(value, str) else json.dumps(value)}'
              for key, value in data['arguments'].items()]
    if data['reason']:
        lines.append(data['reason'])
    lines += [f'! {r["value"]} was not written by you (it came from {r["source"]})'
              for r in data['suspicious_recipients']]
    if data['untrusted_sources'] and not data['suspicious_recipients']:
        lines.append(f'! This run read third-party content first: {", ".join(data["untrusted_sources"])}')
    return '\n'.join(lines), [Choice('Allow', f'{ALLOW}:{data["approval_id"]}'),
                              Choice('Deny', f'{DENY}:{data["approval_id"]}')]


class Channel:
    name = ''  # settings prefix, conversation kind and run source
    title = ''  # how replies name the service
    ignored_event = ''  # the between-runs event for a stranger
    max_file_bytes = MAX_FILE_BYTES
    max_photo_bytes = MAX_PHOTO_BYTES

    def __init__(self, rt, mcp=None, clock=lambda: datetime.now(UTC)):
        self.rt, self.mcp, self.clock = rt, mcp, clock
        self.tasks: set[asyncio.Task] = set()
        self.cards: dict[str, object] = {}  # approval id -> where its prompt is (what close_prompt needs)
        # stranger chat -> when it was last logged. In memory: a daemon restart logs each one again, which is fine
        self.ignored_logged: dict[object, datetime] = {}

    # --- the transport: each channel ---------------------------------------------------------------------

    async def send(self, chat, text: str, choices: list[Choice] | None = None):
        """Sends text (and choices) to a chat; returns where the last part is, for close_prompt."""
        raise NotImplementedError

    async def fetch(self, attachment: Attachment, limit: int) -> bytes:
        """The attachment's bytes, at most limit (FileTooLarge past it, ChannelError when it fails)."""
        raise NotImplementedError

    async def acknowledge(self, decision: Decision, text: str) -> None:
        """Tells whoever picked a choice what came of it."""
        raise NotImplementedError

    async def close_prompt(self, where, status: str) -> None:
        """Replaces a decided prompt's choices with its outcome."""
        raise NotImplementedError

    def chat_from_setting(self, value: str):
        return value

    # --- the owner -----------------------------------------------------------------------------------------

    def setting(self, key: str) -> str:
        return f'{self.name}.{key}'

    @property
    def owner_chat(self):
        value = self.rt.store.get_setting(self.setting('owner_chat'))
        return self.chat_from_setting(value) if value else None

    async def pair(self, chat, who: dict, code: str) -> None:
        key = self.setting('pairing')
        pairing = json.loads(self.rt.store.get_setting(key) or 'null')
        if not pairing or datetime.fromisoformat(pairing['expires_at']) < self.clock():
            self.rt.store.set_setting(key, None)
            return await self.send(chat, 'There is no pairing code waiting. Make one with '
                                         f'`local-agent {self.name} pair`.')
        if not secrets.compare_digest(code, pairing['code']):
            pairing['tries'] += 1
            self.rt.store.set_setting(key, json.dumps(pairing) if pairing['tries'] < PAIRING_TRIES else None)
            return await self.send(chat, 'That code is not right.')
        self.rt.store.set_setting(key, None)
        self.rt.store.set_setting(self.setting('owner_chat'), str(chat))
        self.rt.store.set_setting(self.setting('owner_name'), who.get('username') and f'@{who["username"]}'
                                  or who.get('first_name') or str(chat))
        await self.send(chat, 'Paired: this chat is now the owner\'s.')

    def ignore(self, chat, who: dict) -> None:
        """A stranger: nothing is sent back and no model sees it, so it costs nothing and leaks nothing.
        Logged by chat id and username only: the text is a stranger's, and the log is the owner's."""
        now, last = self.clock(), self.ignored_logged.get(chat)
        if last is not None and now - last < IGNORED_LOG_EVERY:
            return
        self.ignored_logged[chat] = now
        ev.record_event(self.rt.store, self.ignored_event, chat=chat, username=who.get('username') or '')

    # --- receiving -----------------------------------------------------------------------------------------

    async def receive(self, message: Incoming) -> None:
        text, chat = message.text.strip(), message.chat
        if text.startswith('/pair'):
            return await self.pair(chat, message.who, text.removeprefix('/pair').strip())
        if chat != self.owner_chat:
            return self.ignore(chat, message.who)
        if not text and message.attachment is None:
            return
        if text == '/start':
            return await self.send(chat, 'Hi. This is a personal assistant. Its owner pairs it with '
                                         f'`local-agent {self.name} pair`.')
        task = asyncio.create_task(self.answer(message))  # so choices keep being handled while it runs
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def answer(self, message: Incoming) -> None:
        """The owner's message (receive() never gets here for anyone else)."""
        chat = message.chat
        try:
            text, images = await self.for_run(message)
        except Unusable as e:
            return await self.send(chat, str(e))
        conversation = (self.rt.store.find_conversation(self.name, str(chat))
                        or self.rt.store.start_conversation(self.name, external=str(chat)))
        result = await reply(self.rt, conversation.id, text, source=self.name,
                             callback=self.approval_callback(chat), mcp=self.mcp, images=images)
        await self.send(chat, result.answer)

    async def settle(self) -> None:
        """Waits for the messages being answered (tests, and a clean shutdown)."""
        while self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)

    # --- attachments ---------------------------------------------------------------------------------------

    async def for_run(self, message: Incoming) -> tuple[str, list[str]]:
        """The run's message and images, attachments downloaded. Raises Unusable."""
        text = (message.text or message.caption).strip()
        item = message.attachment
        if item is None:
            return text, []
        if item.kind == 'photo':
            return await self._photo(item, text)
        if item.kind == 'voice':
            return await self._voice(item, text), []
        return await self._file(item, text), []

    async def _download(self, item: Attachment, limit: int) -> bytes:
        too_large = f'That is too large for me to download from {self.title} ({limit // 1_000_000} MB at most).'
        if (item.size or 0) > limit:
            raise Unusable(too_large)
        try:
            return await self.fetch(item, limit)
        except FileTooLarge:
            raise Unusable(too_large) from None
        except ChannelError as e:
            logger.warning(str(e))
            raise Unusable(f'I couldn\'t download that from {self.title}. Please send it again.') from None

    def _save(self, name: str, data: bytes) -> str:
        try:
            name = f'{self.clock().astimezone():%Y-%m-%d}-{files.safe_name(name)}'
            return files.save_new(self.rt.home.files_dir, INBOX, name, data)
        except (files.OutsideWorkspace, OSError) as e:
            logger.warning(f'Saving a {self.title} file failed: {e}')
            raise Unusable('I couldn\'t save that file in your workspace.') from None

    async def _photo(self, item: Attachment, caption: str) -> tuple[str, list[str]]:
        """Image input; saved instead when the model can't see. The channel picks the size (the largest that fits)."""
        if (item.size or 0) > self.max_photo_bytes:
            raise Unusable(f'That photo is too large for me ({self.max_photo_bytes // 1_000_000} MB at most).')
        data = await self._download(item, self.max_photo_bytes)
        if not providers.can_see_images(self.rt.config):
            saved = self._save('photo.jpg', data)
            raise Unusable(f'Your model ({self.rt.config.model.default}) can\'t see images, so I saved the photo to '
                           f'your workspace files as {saved}.')
        image = f'data:{item.mime or "image/jpeg"};base64,' + base64.b64encode(data).decode()
        return (f'[photo] {caption}' if caption else '[photo, no caption]'), [image]

    async def _voice(self, item: Attachment, caption: str) -> str:
        transcriber = voice.transcriber(self.rt.config, self.rt.voice_transport)
        if transcriber is None:
            why = voice.choose_transcriber(self.rt.config).why
            raise Unusable(f'I can\'t listen to voice notes yet: transcription is {why}.')
        data = await self._download(item, self.max_file_bytes)
        try:
            words = await transcriber.transcribe(data, 'voice.ogg', item.mime or 'audio/ogg')
        except voice.TranscriptionFailed as e:
            raise Unusable(f'I couldn\'t transcribe that voice note: {e}') from None
        if not words:
            raise Unusable('I couldn\'t hear any words in that voice note.')
        return f'[voice note, transcribed] {words}' + (f'\n[caption] {caption}' if caption else '')

    async def _file(self, item: Attachment, caption: str) -> str:
        data = await self._download(item, self.max_file_bytes)
        saved = self._save(item.name or 'file', data)
        note = f'[The user sent a file, saved in the workspace files as {saved}. Read it with files_read if needed.]'
        return f'{caption}\n{note}' if caption else note

    # --- approvals -----------------------------------------------------------------------------------------

    def approval_callback(self, chat):
        async def callback(event_type: str, data: dict) -> None:
            if event_type == 'approval_required':
                text, choices = approval_prompt(data)
                self.cards[data['approval_id']] = await self.send(chat, text, choices)
            elif event_type == 'approval_resolved':
                await self._close(data['approval_id'], data['status'])
        return callback

    async def decide(self, decision: Decision) -> None:
        """A choice picked: only the paired owner's chat decides; anyone else is refused (and logged as ignored)."""
        action, _, approval_id = decision.data.partition(':')
        owner = self.owner_chat
        if decision.chat != owner or action not in (ALLOW, DENY):
            if decision.chat != owner and decision.chat is not None:
                self.ignore(decision.chat, decision.who)
            return await self.acknowledge(decision, 'Only the owner can decide.')
        decision_ = store_base.APPROVED if action == ALLOW else store_base.DENIED
        decided = self.rt.store.decide_approval(approval_id, decision_)
        approval = self.rt.store.get_approval(approval_id)
        status = approval_card(approval)['status'] if approval else 'unknown'
        await self.acknowledge(decision, status.capitalize() if decided else f'Already {status}.')
        await self._close(approval_id, status)

    async def _close(self, approval_id: str, status: str) -> None:
        where = self.cards.pop(approval_id, None)
        if where is not None:
            await self.close_prompt(where, status)
