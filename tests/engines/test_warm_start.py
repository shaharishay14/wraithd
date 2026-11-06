"""
`wraithd chat` starts its conversation's warm Claude Code session as it opens, so Claude Code starts while the owner
types. Offline: claude_agent_sdk is the fake (fake_sdk.py), whose connect() takes start_seconds.
"""

import asyncio
import os
import time

import pytest

from tests.engines.fake_sdk import init, result, text_delta
from tests.engines.test_claude_engine import claude_rt  # noqa: F401 (a fixture)
from tests.scripted import ScriptedModel, ScriptedTerminal
from wraithd.channels import terminal as terminal_module
from wraithd.config import Home
from wraithd.core import events as ev
from wraithd.run import Runtime, run, start_warm_session

# The measurement's clock: 1.0 plays the brief's numbers (a 2 s start, sends at 0.5 s and 3 s); the suite runs it
# at a tenth. WRAITHD_MEASURE_SCALE=1 uv run pytest tests/engines/test_warm_start.py -s -k measure prints them.
SCALE = float(os.environ.get('WRAITHD_MEASURE_SCALE', '0.1'))
START, FAST, SLOW = 2.0 * SCALE, 0.5 * SCALE, 3.0 * SCALE


class TypingTerminal(ScriptedTerminal):
    """The owner takes `typing` seconds to type the first message; keeps when it was sent and when the answer's
    first token was on screen."""

    def __init__(self, *lines, typing: float = 0.0):
        super().__init__(*lines)
        self.typing, self.sent, self.first_token = typing, None, None

    async def message(self):
        if not self.prompts:  # typing the first message (or waiting before Ctrl-D)
            await asyncio.sleep(self.typing)
            self.sent = time.monotonic() if self.lines else None
        return await super().message()

    def say(self, text='', end='\n'):
        if self.sent is not None and self.first_token is None and end == '' and text:
            self.first_token = time.monotonic()
        super().say(text, end)


def engine_connects(rt, run_id):
    return [e.payload for e in rt.store.run_events(run_id) if e.type == ev.ENGINE_CONNECT]


def first_run(rt):
    return rt.store.recent_runs(10)[-1].run_id


async def chat(rt, terminal, problems=None):
    if problems is not None:
        asyncio.get_running_loop().set_exception_handler(lambda loop, context: problems.append(context))
    return await terminal_module.chat(rt, terminal)


async def test_chat_starts_claude_code_before_the_first_message(claude_rt, claude_sdk):  # noqa: F811
    fake = claude_sdk(start_seconds=0.05)
    rt = claude_rt()
    terminal = TypingTerminal(typing=0.2)  # types nothing: Ctrl-D after a while
    await chat(rt, terminal)
    (client,) = fake.ClaudeSDKClient.instances
    assert client.connects == 1 and client.disconnects == 1 and client.texts == []  # started, never asked


async def test_the_first_message_reuses_the_early_session(claude_rt, claude_sdk):  # noqa: F811
    """No second connect: the early start used the very tools, prompt and model the first message's run needs."""
    fake = claude_sdk([init(), text_delta('hi'), result('hi')], [result('two')], start_seconds=0.05)
    rt = claude_rt()
    await chat(rt, TypingTerminal('hello', 'again', typing=0.1))
    (client,) = fake.ClaudeSDKClient.instances
    assert client.connects == 1 and len(client.texts) == 2
    assert 'USER: hello' in client.texts[0] and 'USER: again' in client.texts[1]
    # engine_connect once, on the first message, with its real time: the connect, not the owner's typing
    runs = [info.run_id for info in reversed(rt.store.recent_runs(10)) if engine_connects(rt, info.run_id)]
    [connect] = [p for run_id in runs for p in engine_connects(rt, run_id) if p['kind'] == 'session']
    assert connect['early'] is True and 50 <= connect['ms'] < 100


async def test_a_fast_typist_waits_for_the_start(claude_rt, claude_sdk):  # noqa: F811
    fake = claude_sdk([init(), text_delta('hi'), result('hi')], start_seconds=0.2)
    rt = claude_rt()
    terminal = TypingTerminal('hello', typing=0.0)
    await chat(rt, terminal)
    assert len(fake.ClaudeSDKClient.instances) == 1 and fake.ClaudeSDKClient.instances[0].connects == 1
    assert terminal.first_token - terminal.sent >= 0.15  # it waited for the start that was under way


@pytest.mark.parametrize('typed', [(), ('/quit',)], ids=['ctrl-d', 'quit'])
@pytest.mark.parametrize('typing', [0.0, 0.1], ids=['while-starting', 'after-start'])
def test_quitting_before_the_first_message_closes_it_cleanly(claude_rt, claude_sdk, capfd, typed,  # noqa: F811
                                                            typing):
    fake = claude_sdk(start_seconds=0.05)
    rt = claude_rt()
    problems = []

    async def main():
        await chat(rt, TypingTerminal(*typed, typing=typing), problems)
        assert rt.sessions.sessions == {}

    asyncio.run(main())
    assert problems == [] and capfd.readouterr() == ('', '')
    clients = fake.ClaudeSDKClient.instances
    assert all(client.texts == [] for client in clients)
    if typing:
        assert [client.disconnects for client in clients] == [1]
    else:  # cut short while starting (or before it began): it never connected, so there is nothing to disconnect
        assert [client.disconnects for client in clients] in ([], [0])


def test_ctrl_c_while_starting_closes_it_cleanly(claude_rt, claude_sdk, capfd):  # noqa: F811
    fake = claude_sdk(start_seconds=5)
    rt = claude_rt()
    problems = []

    async def main():
        task = asyncio.create_task(chat(rt, TypingTerminal('hello', typing=5), problems))
        await asyncio.sleep(0.05)
        task.cancel()  # what asyncio.run does on Ctrl-C
        with pytest.raises(asyncio.CancelledError):
            await task
        assert rt.sessions.sessions == {}

    asyncio.run(main())
    assert problems == [] and capfd.readouterr() == ('', '')
    assert [client.texts for client in fake.ClaudeSDKClient.instances] == [[]]


async def test_a_failed_early_start_falls_back_to_starting_on_the_first_message(claude_rt, claude_sdk):  # noqa: F811
    fake = claude_sdk([init(), text_delta('hi'), result('hi')])
    fake.ClaudeSDKClient.connect_errors.append(RuntimeError('no claude'))
    rt = claude_rt()
    terminal = TypingTerminal('hello', typing=0.05)
    await chat(rt, terminal)
    first, second = fake.ClaudeSDKClient.instances
    assert first.texts == [] and second.connects == 1 and 'USER: hello' in second.texts[0]
    assert 'hi' in terminal.text and 'no claude' not in terminal.text
    [connect] = engine_connects(rt, first_run(rt))
    assert 'early' not in connect  # started by the message, as before


async def test_changed_tools_after_the_early_start_open_a_new_session(claude_rt, claude_sdk):  # noqa: F811
    """The guard the early start relies on: a run whose tools differ never uses the early session."""
    from tests.engines.test_claude_engine import TOOLS

    fake = claude_sdk()
    rt = claude_rt()
    conversation = rt.store.start_conversation('terminal').id
    assert await start_warm_session(rt, conversation)
    await run(rt, 'hello', conversation=conversation, callback=lambda *a: asyncio.sleep(0), tools=TOOLS)
    early, used = fake.ClaudeSDKClient.instances
    assert early.texts == [] and early.disconnects == 1 and used.texts


async def test_nothing_starts_early_off_claude_code(home):
    rt = Runtime.open(Home.locate(), models={'default': ScriptedModel([])})
    try:
        assert await start_warm_session(rt, rt.store.start_conversation('terminal').id) is False
        assert rt.sessions.sessions == {}
    finally:
        rt.close()


# --- the measurement: message sent -> first token, before and after ----------------------------------------------

async def _wait(claude_rt, claude_sdk, typing: float) -> float:  # noqa: F811
    claude_sdk([init(), text_delta('hi'), result('hi')], start_seconds=START)
    rt = claude_rt()
    terminal = TypingTerminal('hello', typing=typing)
    await chat(rt, terminal)
    return terminal.first_token - terminal.sent


@pytest.mark.parametrize('typist,typing', [('fast', FAST), ('slow', SLOW)])
async def test_measure_the_first_message_wait(claude_rt, claude_sdk, monkeypatch, typist, typing):  # noqa: F811
    early = {'on': False}  # before: Claude Code starts on the first message, as it did

    async def maybe(rt, conversation, **options):
        return await start_warm_session(rt, conversation, **options) if early['on'] else False

    monkeypatch.setattr(terminal_module, 'start_warm_session', maybe)
    before = await _wait(claude_rt, claude_sdk, typing)
    early['on'] = True
    after = await _wait(claude_rt, claude_sdk, typing)
    print(f'\n{typist} typist (sends at {typing:.2f}s, start {START:.2f}s): '
          f'sent -> first token before {before:.3f}s, after {after:.3f}s')
    assert before >= START
    expected = max(START - typing, 0.0)
    assert expected - 0.01 <= after < expected + 0.1 * START + 0.05
