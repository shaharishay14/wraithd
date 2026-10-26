"""
Telegram, by long polling over httpx: no public URL, no webhook, no extra library.

- Pairing: `local-agent telegram pair` (or the web UI) makes a 6-digit code; the owner sends `/pair <code>` to the
  bot within 10 minutes, and that chat becomes the owner's. Five wrong tries and the code is gone.
- The owner's messages are runs like the terminal's (memory, files, apps), and writes ask with inline buttons,
  decided through the same approval store as everywhere else.
- The bot is personal: anyone else's message gets no run, no model call and no reply, except `/pair <code>` (the
  owner's first contact comes from a chat that isn't paired yet). Each such chat is logged as a telegram_ignored
  event, at most once an hour, so the owner can see someone tried without strangers filling the log.
- The bot token is in the keychain (`local-agent key set telegram`) and registered for redaction; errors never
  include the API URL, since the token is in it.
"""

import asyncio
import json
import logging
import secrets
from datetime import UTC, datetime, timedelta

import httpx

from local_agent.channels.conversation import reply
from local_agent.core import events as ev
from local_agent.core.gate import approval_card
from local_agent.store import base as store_base
from local_agent.tools.notify import StoreNotifier

logger = logging.getLogger(__name__)

API = 'https://api.telegram.org'
POLL_SECONDS = 30
MESSAGE_CHARS = 4000  # Telegram's limit is 4096
PAIRING_MINUTES = 10
PAIRING_TRIES = 5
IGNORED_LOG_EVERY = timedelta(hours=1)  # per stranger chat
OWNER_CHAT, OWNER_NAME, PAIRING, OFFSET = 'telegram.owner_chat', 'telegram.owner_name', 'telegram.pairing', \
    'telegram.offset'


class TelegramError(Exception):
    """A Bot API call failed. The message never includes the URL (it has the token in it)."""


def new_pairing(store, now: datetime | None = None) -> dict:
    """A fresh pairing code, replacing any earlier one."""
    now = now or datetime.now(UTC)
    pairing = {'code': f'{secrets.randbelow(1_000_000):06d}',
               'expires_at': (now + timedelta(minutes=PAIRING_MINUTES)).isoformat(timespec='microseconds'),
               'tries': 0}
    store.set_setting(PAIRING, json.dumps(pairing))
    return pairing


def _chunks(text: str) -> list[str]:
    text = text or '(no answer)'
    return [text[i:i + MESSAGE_CHARS] for i in range(0, len(text), MESSAGE_CHARS)]


class TelegramBot:
    """The Bot API calls the channel needs."""

    def __init__(self, token: str, transport: httpx.AsyncBaseTransport | None = None):
        self._client = httpx.AsyncClient(base_url=f'{API}/bot{token}/', transport=transport,
                                         timeout=POLL_SECONDS + 10)

    async def call(self, method: str, **params) -> dict | list:
        try:
            response = await self._client.post(method, json=params)
            data = response.json()
        except (httpx.HTTPError, ValueError) as e:
            raise TelegramError(f'Telegram {method} failed ({type(e).__name__}).') from None
        if not data.get('ok'):
            raise TelegramError(f'Telegram {method} failed: {data.get("description", "unknown error")}')
        return data['result']

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


class TelegramChannel:
    def __init__(self, rt, bot: TelegramBot, mcp=None, clock=lambda: datetime.now(UTC)):
        self.rt, self.bot, self.mcp, self.clock = rt, bot, mcp, clock
        self.tasks: set[asyncio.Task] = set()
        self.cards: dict[str, tuple[int, int]] = {}  # approval id -> (chat, message) of its buttons
        # stranger chat -> when it was last logged. In memory: a daemon restart logs each one again, which is fine
        self.ignored_logged: dict[int, datetime] = {}

    @property
    def owner_chat(self) -> int | None:
        value = self.rt.store.get_setting(OWNER_CHAT)
        return int(value) if value else None

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
                await self._pair(chat['id'], chat, text.removeprefix('/pair').strip())
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

    async def settle(self) -> None:
        """Waits for the messages being answered (tests, and a clean shutdown)."""
        while self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)

    # --- updates -------------------------------------------------------------------------------------------

    async def handle(self, update: dict) -> None:
        if 'callback_query' in update:
            return await self._decide(update['callback_query'])
        message = update.get('message') or {}
        text, chat = (message.get('text') or '').strip(), message.get('chat') or {}
        if 'id' not in chat:
            return
        chat_id = chat['id']
        if text.startswith('/pair'):
            return await self._pair(chat_id, chat, text.removeprefix('/pair').strip())
        if chat_id != self.owner_chat:
            return self._ignore(chat_id, chat)
        if not text:
            return
        if text == '/start':
            return await self.bot.send(chat_id, 'Hi. This is a personal assistant. Its owner pairs it with '
                                                '`local-agent telegram pair`.')
        task = asyncio.create_task(self._answer(chat_id, chat, text))  # so button presses keep being handled
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def _ignore(self, chat_id: int, chat: dict) -> None:
        """A stranger's message: nothing is sent back and no model sees it, so it costs nothing and leaks nothing.
        Logged by chat id and username only: the text is a stranger's, and the log is the owner's."""
        now, last = self.clock(), self.ignored_logged.get(chat_id)
        if last is not None and now - last < IGNORED_LOG_EVERY:
            return
        self.ignored_logged[chat_id] = now
        ev.record_event(self.rt.store, ev.TELEGRAM_IGNORED, chat=chat_id, username=chat.get('username') or '')

    async def _pair(self, chat_id: int, chat: dict, code: str) -> None:
        pairing = json.loads(self.rt.store.get_setting(PAIRING) or 'null')
        if not pairing or datetime.fromisoformat(pairing['expires_at']) < self.clock():
            self.rt.store.set_setting(PAIRING, None)
            return await self.bot.send(chat_id, 'There is no pairing code waiting. Make one with '
                                                '`local-agent telegram pair`.')
        if not secrets.compare_digest(code, pairing['code']):
            pairing['tries'] += 1
            self.rt.store.set_setting(PAIRING, json.dumps(pairing) if pairing['tries'] < PAIRING_TRIES else None)
            return await self.bot.send(chat_id, 'That code is not right.')
        self.rt.store.set_setting(PAIRING, None)
        self.rt.store.set_setting(OWNER_CHAT, str(chat_id))
        self.rt.store.set_setting(OWNER_NAME, chat.get('username') and f'@{chat["username"]}' or chat.get('first_name')
                                  or str(chat_id))
        await self.bot.send(chat_id, 'Paired: this chat is now the owner\'s.')

    async def _answer(self, chat_id: int, chat: dict, text: str) -> None:
        """The owner's message (handle() never gets here for anyone else)."""
        conversation = (self.rt.store.find_conversation('telegram', str(chat_id))
                        or self.rt.store.start_conversation('telegram', external=str(chat_id)))
        result = await reply(self.rt, conversation.id, text, source='telegram',
                             callback=self._callback(chat_id), mcp=self.mcp)
        await self.bot.send(chat_id, result.answer)

    # --- approvals as buttons ------------------------------------------------------------------------------

    def _callback(self, chat_id: int):
        async def callback(event_type: str, data: dict) -> None:
            if event_type == 'approval_required':
                lines = [f'Allow {data["tool"]}?']
                lines += [f'  {key}: {value if isinstance(value, str) else json.dumps(value)}'
                          for key, value in data['arguments'].items()]
                if data['reason']:
                    lines.append(data['reason'])
                lines += [f'! {r["value"]} was not written by you (it came from {r["source"]})'
                          for r in data['suspicious_recipients']]
                if data['untrusted_sources'] and not data['suspicious_recipients']:
                    lines.append(f'! This run read third-party content first: {", ".join(data["untrusted_sources"])}')
                buttons = [[{'text': 'Allow', 'callback_data': f'a:{data["approval_id"]}'},
                            {'text': 'Deny', 'callback_data': f'd:{data["approval_id"]}'}]]
                message_id = await self.bot.send(chat_id, '\n'.join(lines), buttons)
                self.cards[data['approval_id']] = (chat_id, message_id)
            elif event_type == 'approval_resolved':
                await self._close_card(data['approval_id'], data['status'])
        return callback

    async def _decide(self, query: dict) -> None:
        chat_id = ((query.get('message') or {}).get('chat') or {}).get('id')
        action, _, approval_id = (query.get('data') or '').partition(':')
        if chat_id != self.owner_chat or action not in ('a', 'd'):
            return await self.bot.call('answerCallbackQuery', callback_query_id=query['id'],
                                       text='Only the owner can decide.')
        decision = store_base.APPROVED if action == 'a' else store_base.DENIED
        decided = self.rt.store.decide_approval(approval_id, decision)
        approval = self.rt.store.get_approval(approval_id)
        status = approval_card(approval)['status'] if approval else 'unknown'
        await self.bot.call('answerCallbackQuery', callback_query_id=query['id'],
                            text=status.capitalize() if decided else f'Already {status}.')
        await self._close_card(approval_id, status)

    async def _close_card(self, approval_id: str, status: str) -> None:
        where = self.cards.pop(approval_id, None)
        if where:
            await self.bot.call('editMessageReplyMarkup', chat_id=where[0], message_id=where[1],
                                reply_markup={'inline_keyboard': [[{'text': status.capitalize(),
                                                                    'callback_data': 'done'}]]})


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
