"""
Telegram, by long polling over httpx: no public URL, no webhook, no extra library.

- Pairing: `wraithd telegram pair` (or the web UI) makes a 6-digit code; the owner sends `/pair <code>` to the
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
- `/mcp_add` (the owner's own typed message only, never a forwarded one) adds an MCP server behind a confirm card:
  channels/base.py and mcp/remote_add.py.
- While a run answers the owner, the chat shows "typing…" (sendChatAction, again every 4 s, as it lasts 5 s),
  until the answer is sent or the run fails. An answer that is exactly NO_REPLY sends nothing (core/silent.py).
- Quick actions: [telegram.commands] (config.py; five by default) are registered with setMyCommands when polling
  starts, so they're in the "/" menu, and the owner's "/news" is sent as its message ("run news-feed").
- Draft cards: a notification whose first line is a draft card's (`✍️ D<n> · <type>`, the slabbed-x format) gets
  Post D<n> · Rewrite · Skip buttons, fixed here, never chosen by the model (channels/base.py, draft_choice).
- Replies are the model's Markdown turned into Telegram's HTML (bold, italics, code, links, lists, quotes), split
  between messages only at line ends so a tag is never cut; if Telegram still rejects the entities, that part goes
  again as plain text. Approval and confirm cards go plain, so their arguments show exactly as they are.
- The bot token is in the keychain (`wraithd key set telegram`) and registered for redaction; errors never
  include the API URL, since the token is in it.
"""

import asyncio
import html
import logging
import re
import time
from datetime import UTC, datetime

import httpx

from wraithd import providers, voice  # noqa: F401 (used by channels/base.py; tests patch them through here)
from wraithd.channels import base
from wraithd.core import events as ev
from wraithd.tools.notify import StoreNotifier

logger = logging.getLogger(__name__)

API = 'https://api.telegram.org'
POLL_SECONDS = 30
MESSAGE_CHARS = 4000  # of Markdown per message
TELEGRAM_CHARS = 4096  # Telegram's limit
MAX_FILE_BYTES = 20_000_000  # the Bot API's own limit for downloads
MAX_PHOTO_BYTES = base.MAX_PHOTO_BYTES
FILE_KINDS = {'document': 'file', 'audio': 'audio', 'video': 'video.mp4', 'video_note': 'video-note.mp4',
              'animation': 'animation.mp4'}  # saved to the inbox; the value is the name when Telegram gives none
# A forwarded message carries one of these (forward_origin since Bot API 7.0; the others before it)
FORWARD_KEYS = ('forward_origin', 'forward_from', 'forward_from_chat', 'forward_sender_name', 'forward_date')
DRAFT_CARD = re.compile(r'^✍\ufe0f? D([0-9]{1,2}) · \S')  # a draft card's first line: ✍️ D2 · reply
COMMAND_DESCRIPTION = 256  # Telegram's limit on a command's description
OWNER_CHAT, OWNER_NAME, PAIRING, OFFSET = 'telegram.owner_chat', 'telegram.owner_name', 'telegram.pairing', \
    'telegram.offset'


class TelegramError(base.ChannelError):
    """A Bot API call failed. The message never includes the URL (it has the token in it)."""


class FileTooLarge(TelegramError, base.FileTooLarge):
    """A download is over its cap."""


def new_pairing(store, now: datetime | None = None) -> dict:
    """A fresh pairing code, replacing any earlier one."""
    return base.new_pairing(store, 'telegram', now)


def _chunks(text: str, limit: int = MESSAGE_CHARS) -> list[str]:
    """Plain text in parts of at most limit characters."""
    text = text or '(no answer)'
    return [text[i:i + limit] for i in range(0, len(text), limit)]


# --- Markdown to Telegram HTML -------------------------------------------------------------------------------
# Telegram's HTML takes <b> <i> <s> <code> <pre> <a href> <blockquote>, and & < > must be escaped. Each block is
# converted on its own and every tag the converter writes is closed in the same line, so the result is always
# balanced; a part that Telegram still refuses is sent again as plain text (TelegramBot.send).

FENCE = re.compile(r'^\s*(```|~~~)\s*([\w+#.-]*)\s*$')
HEADING = re.compile(r'^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$')
BULLET = re.compile(r'^(\s*)[-*+]\s+(.*)$')
QUOTE = re.compile(r'^\s{0,3}>\s?(.*)$')
RULE = re.compile(r'^\s{0,3}([-*_])(\s*\1){2,}\s*$')
TABLE = re.compile(r'^\s*\|.*\|\s*$')
INLINE_CODE = re.compile(r'(`+)(.+?)\1')
LINK = re.compile(r'\[([^\]\n]+)\]\(((?:https?://|mailto:|tg://)[^\s)]+)\)')
BOLD = re.compile(r'\*\*(?=\S)(.+?)(?<=\S)\*\*')
STRIKE = re.compile(r'~~(?=\S)([^<>~\n]+?)(?<=\S)~~')
ITALIC = re.compile(r'(?<![\w*])\*(?=[^\s*])([^<>*\n]+?)(?<=[^\s*])\*(?![\w*])'
                    r'|(?<![\w_])_(?=[^\s_])([^<>_\n]+?)(?<=[^\s_])_(?![\w_])')
SLOT = '\x00'  # placeholders for code spans and links while the rest is converted


def _inline(text: str) -> str:
    """One line's inline Markdown (code, links, bold, italics, strikethrough) as escaped Telegram HTML."""
    slots: list[str] = []

    def keep(markup: str) -> str:
        slots.append(markup)
        return f'{SLOT}{len(slots) - 1}{SLOT}'

    text = text.replace(SLOT, '')
    text = INLINE_CODE.sub(lambda m: keep(f'<code>{html.escape(m.group(2).strip(), quote=False)}</code>'), text)
    text = LINK.sub(lambda m: keep(f'<a href="{html.escape(m.group(2))}">'
                                   f'{_emphasis(html.escape(m.group(1), quote=False))}</a>'), text)
    text = _emphasis(html.escape(text, quote=False))
    return re.sub(f'{SLOT}(\\d+){SLOT}', lambda m: slots[int(m.group(1))], text)


def _emphasis(text: str) -> str:
    text = BOLD.sub(r'<b>\1</b>', text)  # first, so what it wraps has no tags yet
    text = STRIKE.sub(r'<s>\1</s>', text)
    return ITALIC.sub(lambda m: f'<i>{m.group(1) or m.group(2)}</i>', text)


def markdown_to_html(text: str) -> str:
    """The model's Markdown as Telegram HTML. Text that isn't Markdown comes out the same, only escaped."""
    out: list[str] = []
    lines = (text or '').replace(SLOT, '').split('\n')
    i = 0
    while i < len(lines):
        line = lines[i]
        if fence := FENCE.match(line):
            body, i = [], i + 1
            closing = re.compile(re.escape(fence.group(1)[0]) + '{3,}')
            while i < len(lines) and not closing.fullmatch(lines[i].strip()):
                body.append(lines[i])
                i += 1
            language = f' class="language-{fence.group(2)}"' if fence.group(2) else ''
            out.append(f'<pre><code{language}>{html.escape(chr(10).join(body), quote=False)}</code></pre>')
            i += 1  # the closing fence (or the end)
            continue
        if TABLE.match(line):
            rows = []
            while i < len(lines) and TABLE.match(lines[i]):
                if not re.fullmatch(r'[\s|:-]+', lines[i]):  # the |---|---| line
                    rows.append(lines[i].strip())
                i += 1
            out.append(f'<pre>{html.escape(chr(10).join(rows), quote=False)}</pre>')
            continue
        if QUOTE.match(line):
            quoted = []
            while i < len(lines) and (match := QUOTE.match(lines[i])):
                quoted.append(_inline(match.group(1)))
                i += 1
            out.append(f'<blockquote>{chr(10).join(quoted)}</blockquote>')
            continue
        if heading := HEADING.match(line):
            out.append(f'<b>{_inline(heading.group(1).replace("**", ""))}</b>')
        elif RULE.match(line):
            out.append('──────────')
        elif bullet := BULLET.match(line):
            out.append(f'{bullet.group(1)}• {_inline(bullet.group(2))}')
        else:
            out.append(_inline(line))
        i += 1
    return '\n'.join(out)


def _markdown_parts(text: str, limit: int = MESSAGE_CHARS) -> list[str]:
    """The Markdown split at line ends into parts of at most limit characters, never inside a code block: a block
    that has to be split is closed at the end of one part and opened again at the start of the next."""
    parts, current, fence = [], '', None

    def flush():
        nonlocal current
        if current.strip():
            parts.append(current.rstrip('\n') + (f'\n{fence[0]}' if fence else ''))
        current = f'{fence[0]}{fence[1]}\n' if fence else ''

    for line in (text or '(no answer)').split('\n'):
        reserve = 4 if fence or FENCE.match(line) else 0  # room to close a code block
        pieces = [line[j:j + limit - 8] for j in range(0, len(line), limit - 8)] or ['']
        for piece in pieces:
            if len(current) + len(piece) + 1 + reserve > limit:
                flush()
            current += piece + '\n'
        if match := FENCE.match(line):
            fence = None if fence else (match.group(1), match.group(2))
    flush()
    return parts or ['(no answer)']


def formatted_parts(text: str, limit: int = MESSAGE_CHARS) -> list[tuple[str, str]]:
    """(html, markdown) per message: the HTML to send and the Markdown it came from, the plain fallback."""
    out = []
    for part in _markdown_parts(text, limit):
        converted = markdown_to_html(part)
        if len(converted) > TELEGRAM_CHARS and limit > 200:  # escaping made it too long: smaller parts
            out += formatted_parts(part, limit // 2)
        else:
            out.append((converted, part))
    return out


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

    async def send(self, chat_id, text: str, buttons=None, markdown: bool = True, stats: dict | None = None
                   ) -> int | None:
        """Sends text (split as needed; buttons go on each part) and returns the last message's id. Markdown is
        sent as Telegram HTML, and a part Telegram can't parse goes again as the plain Markdown. stats, if given, is
        filled with chunks, parse_mode ('HTML' if any part went as HTML, else None) and fallback (a part went plain
        after Telegram refused its HTML), for telegram_sent."""
        message_id = None
        parts = formatted_parts(text) if markdown else [(None, part) for part in _chunks(text)]
        stats = stats if stats is not None else {}
        stats.update(chunks=len(parts), parse_mode=None, fallback=False)
        for converted, plain in parts:
            params = {'chat_id': chat_id, 'text': plain}
            if buttons:
                params['reply_markup'] = {'inline_keyboard': buttons}
            if converted is not None and converted != plain:
                try:
                    message_id = (await self.call('sendMessage', **{**params, 'text': converted,
                                                                     'parse_mode': 'HTML'}))['message_id']
                    stats['parse_mode'] = 'HTML'
                    continue
                except TelegramError as e:
                    if 'entit' not in str(e).lower() and 'tag' not in str(e).lower():
                        raise
                    logger.info('Telegram refused the formatting; sending it plain')
                    stats['fallback'] = True
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

    def quick_commands(self) -> dict[str, str]:
        return dict(self.rt.config.telegram_commands)

    async def register_commands(self) -> None:
        """The quick actions in Telegram's "/" menu (setMyCommands replaces the bot's list, so it matches config)."""
        commands = [{'command': name, 'description': message[:COMMAND_DESCRIPTION]}
                    for name, message in self.quick_commands().items()]
        try:
            await self.bot.call('setMyCommands', commands=commands)
        except TelegramError as e:
            logger.warning(f'Registering the Telegram commands failed: {e}')

    async def delete(self, chat, ref) -> None:
        await self.bot.call('deleteMessage', chat_id=chat, message_id=ref)

    async def busy(self, chat) -> None:
        await self.bot.call('sendChatAction', chat_id=chat, action='typing')

    # --- the transport -------------------------------------------------------------------------------------

    async def send(self, chat, text: str, choices: list[base.Choice] | None = None) -> tuple[int, int | None]:
        buttons = [[{'text': c.label, 'callback_data': c.data} for c in choices]] if choices else None
        # cards (with choices) go plain: their arguments must show exactly as they are
        return chat, await self.bot.send(chat, text, buttons, markdown=not choices)

    async def send_answer(self, chat, result, ended: float) -> None:
        """The answer, then telegram_sent: how long it took from the run's end until Telegram replied."""
        stats: dict = {}
        await self.bot.send(chat, result.answer, markdown=True, stats=stats)
        record_sent(self.rt.store, result.run_id, ended, stats)

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
        For `wraithd onboard` when no daemon is polling: one getUpdates call that handles only `/pair` and stops
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
        await self.register_commands()
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
                                         caption=message.get('caption') or '', attachment=_attachment(message),
                                         forwarded=any(key in message for key in FORWARD_KEYS),
                                         ref=message.get('message_id')))


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


def draft_buttons(text: str) -> list | None:
    """Post D<n> · Rewrite · Skip under a draft card (its first line names the draft), else None."""
    match = DRAFT_CARD.match((text or '').lstrip())
    if not match:
        return None
    number = int(match.group(1))
    return [[{'text': f'Post D{number}', 'callback_data': f'draft:{number}:post'},
             {'text': 'Rewrite', 'callback_data': f'draft:{number}:rewrite'},
             {'text': 'Skip', 'callback_data': f'draft:{number}:skip'}]]


def record_sent(store, run_id: str | None, since: float, stats: dict, kind: str = 'answer') -> None:
    """telegram_sent, between runs: ms from since (time.monotonic(): the run's end, or a notify's send) until
    Telegram replied, and how it went (TelegramBot.send's stats)."""
    ev.record_event(store, ev.TELEGRAM_SENT, kind=kind, run_id=run_id, ms=int((time.monotonic() - since) * 1000),
                    **stats)


class TelegramNotifier:
    """notify_owner (and schedules' and the heartbeat's messages) through Telegram when there's a paired owner chat.
    Every message is also kept in the store's inbox (Notifications), sent or not, once: the web UI lists it, and the
    heartbeat's "Already told them lately" reads it."""

    channel = 'telegram'

    def __init__(self, store, bot: TelegramBot):
        self.store, self.bot, self.inbox = store, bot, StoreNotifier(store)

    async def deliver(self, text: str) -> str:
        owner = self.store.get_setting(OWNER_CHAT)
        delivered = self.inbox.channel
        if owner:
            try:
                stats, started = {}, time.monotonic()
                await self.bot.send(int(owner), text, draft_buttons(text), stats=stats)
                record_sent(self.store, None, started, stats, kind='notify')
                delivered = self.channel
            except TelegramError as e:
                logger.warning(f'{e}; kept in the inbox instead')
        await self.inbox.deliver(text)
        return delivered
