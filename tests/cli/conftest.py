import io
from contextlib import contextmanager

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from wraithd.channels.terminal import RichTerminal
from wraithd.tui import theme

UP, DOWN, ENTER, CTRL_D, ESC_ENTER, TAB = '\x1b[A', '\x1b[B', '\r', '\x04', '\x1b\r', '\t'


@contextmanager
def _rich(keys: str = ''):
    """The rich terminal on prompt_toolkit's pipe input (keys typed ahead) and a console that writes plain text."""
    with create_pipe_input() as pipe:
        if keys:
            pipe.send_text(keys)
        out = io.StringIO()
        console = theme.console(file=out, width=100, color_system=None)
        term = RichTerminal(console=console, input=pipe, output=DummyOutput(), live=False)
        term.pipe, term.out = pipe, out
        yield term


@pytest.fixture
def rich():
    return _rich
