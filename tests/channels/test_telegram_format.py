"""The model's Markdown reaches Telegram as HTML that renders: balanced tags, escaped text, parts that never cut a
tag or a code block, and a plain resend when Telegram refuses the entities. Cards stay plain."""

import json
import re

import httpx
import pytest

from tests.daemon.test_telegram import BOT_TOKEN, OWNER, FakeTelegram
from tests.scripted import ScriptedModel
from wraithd.channels.telegram import (
    OWNER_CHAT,
    TELEGRAM_CHARS,
    TelegramBot,
    TelegramNotifier,
    formatted_parts,
    markdown_to_html,
)

TAG = re.compile(r'<(/?)(b|i|s|code|pre|a|blockquote)\b[^>]*>')


def balanced(markup: str) -> bool:
    stack = []
    for match in TAG.finditer(markup):
        closing, name = match.groups()
        if not closing:
            stack.append(name)
        elif not stack or stack.pop() != name:
            return False
    return not stack and '<' not in TAG.sub('', markup)


@pytest.mark.parametrize('markdown, expected', [
    ('**bold** and *it* and _also_', '<b>bold</b> and <i>it</i> and <i>also</i>'),
    ('~~old~~ `a<b>`', '<s>old</s> <code>a&lt;b&gt;</code>'),
    ('[docs](https://x.dev/?a=1&b=2)', '<a href="https://x.dev/?a=1&amp;b=2">docs</a>'),
    ('## Plan', '<b>Plan</b>'),
    ('- one\n* two\n1. three', '• one\n• two\n1. three'),
    ('> quoted', '<blockquote>quoted</blockquote>'),
    ('```python\nif a < b:\n    x = "**no**"\n```', '<pre><code class="language-python">if a &lt; b:\n'
                                                     '    x = "**no**"</code></pre>'),
    ('Tom & Jerry <3', 'Tom &amp; Jerry &lt;3'),
    ('my_var and 2 * 3 * 4 stay', 'my_var and 2 * 3 * 4 stay'),
    ('[evil](javascript:alert(1))', '[evil](javascript:alert(1))'),
    ("I couldn't hear that.", "I couldn't hear that."),
])
def test_markdown_becomes_telegram_html(markdown, expected):
    assert markdown_to_html(markdown) == expected


@pytest.mark.parametrize('markdown', [
    '**a *b***', '*a **b** c*', '~~a **b~~ c**', '**unclosed', '`code *x*` *y*', '[**x**](https://a.b) _y_',
    '| a | b |\n|---|---|\n| <1> | 2 |', '```\nnever closed <x>',
])
def test_tags_are_always_balanced(markdown):
    assert balanced(markdown_to_html(markdown))


def test_long_replies_split_between_lines_and_keep_code_blocks_whole():
    text = 'Intro **bold**\n```sh\n' + '\n'.join(f'echo {i} <&>' for i in range(900)) + '\n```\nDone *now*'
    parts = formatted_parts(text)
    assert len(parts) > 1
    for converted, plain in parts:
        assert len(converted) <= TELEGRAM_CHARS and balanced(converted)
        assert plain.count('```') % 2 == 0  # a split block is closed in one part and opened in the next
    assert parts[0][0].startswith('Intro <b>bold</b>') and parts[-1][0].endswith('Done <i>now</i>')
    lines = [line for _, plain in parts for line in plain.split('\n') if line.startswith('echo')]
    assert lines == [f'echo {i} <&>' for i in range(900)]


@pytest.fixture
async def bot_with():
    bots = []

    def make(handler):
        bot = TelegramBot(BOT_TOKEN, transport=httpx.MockTransport(handler))
        bots.append(bot)
        return bot

    yield make
    for bot in bots:
        await bot.close()


async def test_replies_go_as_html(bot_with):
    fake = FakeTelegram()
    await bot_with(fake.handler).send(OWNER, 'Done: **3 files**')
    assert fake.sent(OWNER) == [{'chat_id': OWNER, 'text': 'Done: <b>3 files</b>', 'parse_mode': 'HTML'}]


async def test_plain_text_needs_no_parse_mode(bot_with):
    fake = FakeTelegram()
    await bot_with(fake.handler).send(OWNER, 'Saved.')
    assert fake.sent(OWNER) == [{'chat_id': OWNER, 'text': 'Saved.'}]


async def test_refused_entities_are_sent_again_as_plain_text(bot_with):
    fake, calls = FakeTelegram(), []

    def handler(request):
        params = json.loads(request.content)
        calls.append(params)
        if params.get('parse_mode'):
            return httpx.Response(400, json={'ok': False, 'description': "Bad Request: can't parse entities"})
        return fake.handler(request)

    message_id = await bot_with(handler).send(OWNER, 'A **bold** claim')
    assert [c.get('parse_mode') for c in calls] == ['HTML', None]
    assert calls[-1]['text'] == 'A **bold** claim' and message_id is not None


async def test_other_errors_are_not_retried(bot_with):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(400, json={'ok': False, 'description': 'Bad Request: chat not found'})

    with pytest.raises(Exception, match='chat not found'):
        await bot_with(handler).send(OWNER, 'A **bold** claim')
    assert len(calls) == 1


async def test_cards_with_buttons_stay_plain(bot_with):
    fake = FakeTelegram()
    buttons = [[{'text': 'Allow', 'callback_data': 'a:1'}]]
    await bot_with(fake.handler).send(OWNER, 'Allow write?\n  path: notes/**draft**.md', buttons, markdown=False)
    (sent,) = fake.sent(OWNER)
    assert sent['text'] == 'Allow write?\n  path: notes/**draft**.md' and 'parse_mode' not in sent
    assert sent['reply_markup'] == {'inline_keyboard': buttons}


async def test_notify_owner_messages_are_formatted_too(isolated, make_runtime, bot_with):
    fake = FakeTelegram()
    rt = make_runtime(ScriptedModel([]))
    rt.store.set_setting(OWNER_CHAT, str(OWNER))
    await TelegramNotifier(rt.store, bot_with(fake.handler)).deliver('**Reminder:** call Sam')
    assert fake.sent(OWNER)[-1] == {'chat_id': OWNER, 'text': '<b>Reminder:</b> call Sam', 'parse_mode': 'HTML'}
