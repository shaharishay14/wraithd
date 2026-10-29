"""
Telegram, by long polling over httpx: no public URL, no webhook, no extra library.

- Pairing: `local-agent telegram pair` (or the web UI) makes a 6-digit code; the owner sends `/pair <code>` to the
  bot within 10 minutes, and that chat becomes the owner's. Five wrong tries and the code is gone.
- The owner's messages are runs like the terminal's (memory, files, apps), and writes ask with inline buttons,
  decided through the same approval store as everywhere else.
- The bot is personal: anyone else's message gets no run, no model call and no reply, except `/pair <code>` (the
  owner's first contact comes from a chat that isn't paired yet). Each such chat is logged as a telegram_ignored
  event, at most once an hour, so the owner can see someone tried without strangers filling the log.
- Attachments from the owner: a photo goes to the model as image input with its caption (saved to the workspace
  instead when the model can't see images), a voice note is transcribed (voice.py) and the transcript is the
  message, and any other file is saved to workspace/files/inbox/ with a line in the message saying where. A
  stranger's attachment is never downloaded. Downloads are capped (Telegram lets bots download 20 MB at most).
- The bot token is in the keychain (`local-agent key set telegram`) and registered for redaction; errors never
  include the API URL, since the token is in it.
"""

import asyncio
import logging
from datetime import UTC, datetime

import httpx

from local_agent import providers, voice  # noqa: F401 (used by channels/base.py; tests patch them through here)
from local_agent.channels import base
from local_agent.core import events as ev
from local_agent.tools.notify import StoreNotifier

logger = logging.getLogger(__name__)

API = 'https://api.telegram.org'
POLL_SECONDS = 30
MESSAGE_CHARS = 4000  # Telegram's limit is 4096
MAX_FILE_BYTES = 20_000_000  # the Bot API's own limit for downloads
MAX_PHOTO_BYTES = base.MAX_PHOTO_BYTES
FILE_KINDS = {'document': 'file', 'audio': 'audio', 'video': 'video.mp4', 'video_note': 'video-note.mp4',
              'animation': 'animation.mp4'}  # saved to the inbox; the value is the name when Telegram gives none
OWNER_CHAT, OWNER_NAME, PAIRING, OFFSET = 'telegram.owner_chat', 'telegram.owner_name', 'telegram.pairing', \
    'telegram.offset'


class TelegramError(base.ChannelError):
    """A Bot API call failed. The message never includes the URL (it has the token in it)."""


class FileTooLarge(TelegramError, base.FileTooLarge):
    """A download is over its cap."""


def new_pairing(store, now: datetime | None = None) -> dict:
    """A fresh pairing code, replacing any earlier one."""
    return base.new_pairing(store, 'telegram', now)


def _chunks(text: str) -> list[str]:
    text = text or '(no answer)'
    return [text[i:i + MESSAGE_CHARS] for i in range(0, len(text), MESSAGE_CHARS)]


class TelegramBot:
    """The Bot API calls the channel needs."""

    def __init__(self, token: str, transport: httpx.AsyncBaseTransport | None = None):
        self._client = httpx.AsyncClient(base_url=f'{API}/bot{token}/', transport=transport,
                                         timeout=POLL_SECONDS + 10)
        self._files = f'{API}/file/bot{token}/'  # has the token in it too: never logged or shown

    async def call(self, method: str, **params) -> dict | list:
        try:
            response = await self._client.post(method, json=params)
            data = response.json()
        except (httpx.HTTPError, ValueError) as e:
            raise TelegramError(f'Telegram {method} failed ({type(e).__name__}).') from None
        if not data.get('ok'):
            raise TelegramError(f'Telegram {method} failed: {data.get("description", "unknown error")}')
        return data['result']

    async def download(self, file_id: str, limit: int) -> bytes:
        """A file someone sent, through getFile, read up to limit bytes (FileTooLarge past it)."""
        info = await self.call('getFile', file_id=file_id)
        if (info.get('file_size') or 0) > limit:
            raise FileTooLarge('The file is too large.')
        if not info.get('file_path'):
            raise TelegramError('Telegram getFile gave no file path.')
        data = bytearray()
        try:
            async with self._client.stream('GET', self._files + info['file_path']) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    data += chunk
                    if len(data) > limit:
                        raise FileTooLarge('The file is too large.')
        except httpx.HTTPError as e:
            raise TelegramError(f'Telegram file download failed ({type(e).__name__}).') from None
        return bytes(data)

    async def send(self, chat_id, text: str, buttons=None) -> int | None:
        message_id = None
        for part in _chunks(text):
            params = {'chat_id': chat_id, 'text': part}
            if buttons:
                params['reply_markup'] = {'inline_keyboard': buttons}
            message_id = (await self.call('sendMessage', **params))['message_id']
        return message_id

    async def close(self) -> None:
        await self._client.aclose()


class TelegramChannel(base.Channel):
    """The channel interface (channels/base.py) over the Bot API: updates in, messages and inline buttons out."""

    name, title, ignored_event = 'telegram', 'Telegram', ev.TELEGRAM_IGNORED
    # read when used, so the module's limits can be changed (tests do)
    max_file_bytes = property(lambda self: MAX_FILE_BYTES)
    max_photo_bytes = property(lambda self: MAX_PHOTO_BYTES)

    def __init__(self, rt, bot: TelegramBot, mcp=None, clock=lambda: datetime.now(UTC)):
        super().__init__(rt, mcp=mcp, clock=clock)
        self.bot = bot

    def chat_from_setting(self, value: str) -> int:
        return int(value)

    # --- the transport -------------------------------------------------------------------------------------

    async def send(self, chat, text: str, choices: list[base.Choice] | None = None) -> tuple[int, int | None]:
        buttons = [[{'text': c.label, 'callback_data': c.data} for c in choices]] if choices else None
        return chat, await self.bot.send(chat, text, buttons)

    async def fetch(self, attachment: base.Attachment, limit: int) -> bytes:
        return await self.bot.download(attachment.ref, limit)

    async def acknowledge(self, decision: base.Decision, text: str) -> None:
        await self.bot.call('answerCallbackQuery', callback_query_id=decision.ref, text=text)

    async def close_prompt(self, where: tuple[int, int | None], status: str) -> None:
        await self.bot.call('editMessageReplyMarkup', chat_id=where[0], message_id=where[1],
                            reply_markup={'inline_keyboard': [[{'text': status.capitalize(),
                                                                'callback_data': 'done'}]]})

    # --- polling -------------------------------------------------------------------------------------------

    async def poll_once(self, timeout: int = POLL_SECONDS) -> int:
        """One getUpdates call; each update is handled (messages in their own task). Returns how many arrived."""
        offset = int(self.rt.store.get_setting(OFFSET) or 0)
        updates = await self.bot.call('getUpdates', offset=offset, timeout=timeout,
                                      allowed_updates=['message', 'callback_query'])
        for update in updates:
            # Move past it first: an update that crashes the handler must not come back forever
            self.rt.store.set_setting(OFFSET, str(update['update_id'] + 1))
            try:
                await self.handle(update)
            except Exception:
                logger.exception('Telegram update failed')
        return len(updates)

    async def poll_for_pairing(self, timeout: int = 5) -> bool:
        """
        For `local-agent onboard` when no daemon is polling: one getUpdates call that handles only `/pair` and stops
        at the first pairing that works. Updates after it are left for the daemon, so the owner's first message
        isn't lost; other messages before it (from chats that aren't the owner's yet) are passed over.
        """
        offset = int(self.rt.store.get_setting(OFFSET) or 0)
        updates = await self.bot.call('getUpdates', offset=offset, timeout=timeout, allowed_updates=['message'])
        for update in updates:
            self.rt.store.set_setting(OFFSET, str(update['update_id'] + 1))
            message = update.get('message') or {}
            text, chat = (message.get('text') or '').strip(), message.get('chat') or {}
            if 'id' in chat and text.startswith('/pair'):
                await self.pair(chat['id'], chat, text.removeprefix('/pair').strip())
                if self.owner_chat is not None:
                    return True
        return self.owner_chat is not None

    async def run_forever(self) -> None:
        while True:
            try:
                await self.poll_once()
            except TelegramError as e:
                logger.warning(str(e))
                await asyncio.sleep(5)

    # --- updates -------------------------------------------------------------------------------------------

    async def handle(self, update: dict) -> None:
        if 'callback_query' in update:
            query = update['callback_query']
            chat = ((query.get('message') or {}).get('chat') or {})
            return await self.decide(base.Decision(chat=chat.get('id'), data=query.get('data') or '',
                                                   ref=query['id'], who=query.get('from') or chat))
        message = update.get('message') or {}
        chat = message.get('chat') or {}
        if 'id' not in chat:
            return
        await self.receive(base.Incoming(chat=chat['id'], who=chat, text=message.get('text') or '',
                                         caption=message.get('caption') or '', attachment=_attachment(message)))


def _attachment(message: dict) -> base.Attachment | None:
    """The message's photo (the largest size that fits the cap), voice note or file, as the channel sees it."""
    if message.get('photo'):
        sizes = message['photo']
        fitting = [size for size in sizes if (size.get('file_size') or 0) <= MAX_PHOTO_BYTES]
        best = max(fitting, key=lambda size: (size.get('width', 0) * size.get('height', 0),
                                              size.get('file_size', 0))) if fitting else \
            min(sizes, key=lambda size: size.get('file_size') or 0)  # none fits: too large, said without a download
        return base.Attachment('photo', best['file_id'], best.get('file_size'), mime='image/jpeg')  # always JPEG
    if message.get('voice'):
        note = message['voice']
        return base.Attachment('voice', note['file_id'], note.get('file_size'), mime=note.get('mime_type') or '')
    kind = next((kind for kind in FILE_KINDS if message.get(kind)), None)
    if kind:
        item = message[kind]
        return base.Attachment('file', item['file_id'], item.get('file_size'),
                               name=item.get('file_name') or FILE_KINDS[kind], mime=item.get('mime_type') or '')
    return None


class TelegramNotifier:
    """notify_owner through Telegram when there's a paired owner chat; otherwise into the store's inbox."""

    channel = 'telegram'

    def __init__(self, store, bot: TelegramBot):
        self.store, self.bot, self.inbox = store, bot, StoreNotifier(store)

    async def deliver(self, text: str) -> str:
        owner = self.store.get_setting(OWNER_CHAT)
        if not owner:
            return await self.inbox.deliver(text)
        try:
            await self.bot.send(int(owner), text)
            return self.channel
        except TelegramError as e:
            logger.warning(f'{e}; kept in the inbox instead')
            return await self.inbox.deliver(text)
