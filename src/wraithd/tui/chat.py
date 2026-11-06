"""
The chat's look: answers as live Markdown, tool calls as one dim line, approvals as a card, a header and a footer.

The answer re-renders as Markdown while it streams (rich Live). Before a tool call or an approval card the text so
far stays on screen as it is, and the next text starts a new block under it.
"""

import json
import time

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown, TextElement
from rich.markup import escape
from rich.panel import Panel
from rich.spinner import Spinner
from rich.text import Text

SPINNER_REFRESH = 12  # frames a second


class _Code(TextElement):
    """A code block: the text as is, behind a dim bar. No background, no colors: right on a dark or light terminal."""

    style_name = 'markdown.code_block'

    @classmethod
    def create(cls, markdown, token):
        return cls()

    def __rich_console__(self, console, options):
        code = str(self.text).rstrip('\n')
        out = Text()
        for line in code.split('\n'):
            out.append('│ ', style='dim')
            out.append(line + '\n')
        out.rstrip()
        yield out


class Answer(Markdown):
    elements = {**Markdown.elements, 'fence': _Code, 'code_block': _Code}


class _Streaming:
    """What Live draws: the Markdown of the text so far, parsed only when the screen refreshes."""

    def __init__(self):
        self.text = ''

    def __rich__(self):
        return Answer(self.text)


class AnswerView:
    """One answer on screen. delta() for text, tool_started() / tool_denied() for tools, close() at the end."""

    def __init__(self, console: Console, live: bool = True):
        self.console = console
        self.live = live  # False: print as it comes, no redraws (tests read the output as plain text)
        self.block: _Streaming | None = None
        self.block_live: Live | None = None
        self.tool: str | None = None
        self.tool_live: Live | None = None
        self.decisions: dict[str, str] = {}  # tool -> what the owner decided on its card
        self.outcome: str = 'ok'  # the running tool's outcome, from its tool_result
        self.denied_shown: set[str] = set()  # tools whose ✗ line decided() already printed
        self.started = time.monotonic()
        self.streamed = False

    def delta(self, text: str) -> None:
        self._end_tool()
        self.streamed = True
        if self.block is None:
            self.block = _Streaming()
            if self.live:
                self.block_live = Live(self.block, console=self.console, refresh_per_second=SPINNER_REFRESH,
                                       vertical_overflow='ellipsis')  # drawn in full when it stops
                self.block_live.start()
        self.block.text += text

    def tool_started(self, name: str) -> None:
        self.pause()
        self.tool, self.outcome = name, 'ok'
        if self.live:
            spinner = Spinner('dots', text=Text(f' {name}', style='dim'), style='accent')
            self.tool_live = Live(spinner, console=self.console, refresh_per_second=SPINNER_REFRESH, transient=True)
            self.tool_live.start()

    def decided(self, tool: str, approved: bool) -> None:
        if approved:
            self.decisions[tool] = 'approved by you'
        else:
            self.console.print(f'  [fail]✗[/fail] [dim]{escape(tool)}  denied by you[/dim]')
            self.denied_shown.add(tool)

    def finished(self, tool: str, outcome: str) -> None:
        """A tool's result (ok, error or denied): the running tool's line gets ✓ or ✗; a call that never started
        (denied by a rule, or refused before it ran) gets its own ✗ line."""
        if tool == self.tool:
            self.outcome = outcome
            self._end_tool()
        elif outcome == 'denied' and tool in self.denied_shown:
            self.denied_shown.discard(tool)
        elif outcome != 'ok':
            self.pause()
            self.console.print(f'  [fail]✗[/fail] [dim]{escape(tool)}  {escape(outcome)}[/dim]')

    def pause(self) -> None:
        """Leaves what's on screen as it is (before an approval card or a tool line)."""
        self._end_text()
        self._end_tool()

    def close(self) -> None:
        self.pause()

    def _end_text(self) -> None:
        if self.block is None:
            return
        if self.block_live:
            self.block_live.stop()
        else:
            self.console.print(Answer(self.block.text))
        self.block, self.block_live = None, None

    def _end_tool(self) -> None:
        if self.tool is None:
            return
        if self.tool_live:
            self.tool_live.stop()
        note = self.decisions.pop(self.tool, 'allowed')
        if self.outcome == 'ok':
            self.console.print(f'  [ok]✓[/ok] [dim]{escape(self.tool)}  {note}[/dim]')
        else:
            self.console.print(f'  [fail]✗[/fail] [dim]{escape(self.tool)}  {note} · {escape(self.outcome)}[/dim]')
        self.tool, self.tool_live, self.outcome = None, None, 'ok'


def _value(value) -> str:
    return value if isinstance(value, str) else json.dumps(value)


def approval_card(data: dict) -> Panel:
    """The tool and its arguments in a box; warnings about strangers and third-party text in red at the top."""
    rows = []
    for recipient in data.get('suspicious_recipients') or []:
        rows.append(Text(f'! {recipient["value"]} was not written by you (it came from {recipient["source"]})',
                         style='fail'))
    if data.get('untrusted_sources') and not data.get('suspicious_recipients'):
        rows.append(Text(f'! This run read third-party content first: {", ".join(data["untrusted_sources"])}',
                         style='fail'))
    if rows:
        rows.append(Text(''))
    width = max((len(key) for key in data['arguments']), default=0)
    for key, value in data['arguments'].items():
        line = Text(f'{key:<{width}}  ', style='dim')
        line.append(_value(value))
        rows.append(line)
    if data.get('reason'):
        rows.append(Text(''))
        rows.append(Text(data['reason'], style='dim'))
    title = Text.assemble(('The agent wants to run ', ''), (data['tool'], 'accent'))
    return Panel(Group(*rows), title=title, title_align='left', box=box.ROUNDED, border_style='dim',
                 padding=(0, 1), expand=False)


def footer(tokens: int | None, seconds: float) -> str:
    parts = [f'{tokens:,} tokens'] if tokens else []
    parts.append(f'{seconds:.1f}s')
    return '[dim]' + ' · '.join(parts) + '[/dim]'
