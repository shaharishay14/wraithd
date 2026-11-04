"""The picker: arrows and Enter, digits, typing to jump, free text for model names, Ctrl-D."""

import asyncio

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from tests.cli.conftest import CTRL_D, DOWN, ENTER, UP
from wraithd.tui.pick import Option, Picker, pick

ENGINES = ['subscription', 'api-key', 'local']


async def picked(keys, options=ENGINES, default=None, free=False):
    with create_pipe_input() as pipe:
        pipe.send_text(keys)
        return await asyncio.wait_for(pick('How?', options, default, free=free, input=pipe, output=DummyOutput()), 5)


async def test_arrows_and_enter():
    assert await picked(ENTER, default='api-key') == 'api-key'  # Enter keeps the default
    assert await picked(DOWN + ENTER, default='api-key') == 'local'
    assert await picked(DOWN + DOWN + ENTER, default='api-key') == 'subscription'  # wraps around
    assert await picked(UP + ENTER) == 'local'


async def test_digits_pick_at_once_and_typing_jumps():
    assert await picked('3') == 'local'
    assert await picked('l' + ENTER) == 'local'
    assert await picked('api-key' + ENTER) == 'api-key'  # typed in full, as in plain mode
    assert await picked('s\x7f' + 'l' + ENTER) == 'local'  # backspace


async def test_a_letter_never_picks_by_itself_so_a_typed_word_does_not_spill():
    with create_pipe_input() as pipe:
        pipe.send_text('anthropic' + ENTER + ENTER)
        first = await pick('Provider', ['openai', 'anthropic'], 'openai', input=pipe, output=DummyOutput())
        second = await pick('Again', ['yes', 'no'], 'no', input=pipe, output=DummyOutput())
    assert (first, second) == ('anthropic', 'no')


async def test_yes_and_no_by_their_letters_or_their_labels():
    options = [Option('yes', 'Allow once'), Option('no', 'Deny')]
    assert await picked('y' + ENTER, options, 'no') == 'yes'
    assert await picked('a' + ENTER, options, 'no') == 'yes'
    assert await picked('n' + ENTER, options, 'yes') == 'no'
    assert await picked(ENTER, options, 'no') == 'no'


async def test_free_text_for_names_that_are_not_listed():
    models = ['gpt-4o-mini', 'gpt-4o', 'other…']
    assert await picked('my-model' + ENTER, models, free=True) == 'my-model'
    assert await picked('gpt-4o' + ENTER, models, free=True) == 'gpt-4o'  # the exact one, not the first match
    assert await picked('o' + ENTER, models, free=True) == 'other…'
    # not free: Enter waits while what's typed matches nothing
    assert await picked('zz' + ENTER + '\x7f\x7f' + ENTER, ENGINES, 'local') == 'local'


async def test_ctrl_d_is_no_answer_and_ctrl_c_interrupts():
    assert await picked(CTRL_D) is None
    with pytest.raises(KeyboardInterrupt):
        await picked('\x03')


def test_the_drawing_marks_the_highlighted_option_and_nothing_when_typing_matches_none():
    state = Picker([Option('yes', 'Allow once', 'hint'), Option('no', 'Deny')], 'no')
    text = ''.join(part for _, part in state.fragments('Allow?'))
    assert '? Allow?\n' in text and '    Allow once  hint\n' in text and '  ❯ Deny' in text
    state.type('z')
    assert '❯' not in ''.join(part for _, part in state.fragments('Allow?'))
