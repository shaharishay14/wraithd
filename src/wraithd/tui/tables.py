"""
Lists as aligned tables: runs, events, schedules, and doctor's and audit's checks with ok / FAIL in color.

Only for a terminal; plain mode prints the lines it always did (cli.py).
"""

from rich import box
from rich.table import Table
from rich.text import Text

STATE_STYLE = {'ok': 'ok', 'FAIL': 'fail', 'warn': 'warn', 'info': 'dim'}
OUTCOME = {'answered': ('ok', 'ok'), 'refused': ('refused', 'warn'), 'failed': ('FAIL', 'fail'),
           'running': ('running', 'dim')}


def table(*columns, **kwargs) -> Table:
    out = Table(box=box.SIMPLE_HEAD, header_style='dim', show_edge=False, pad_edge=False, **kwargs)
    for column in columns:
        name, _, options = column.partition(':')
        out.add_column(name, no_wrap='nowrap' in options, overflow='fold',
                       ratio=1 if 'grow' in options else None, style='dim' if 'dim' in options else None)
    return out


def runs(found: list[tuple]) -> Table:
    """(RunInfo, outcome) pairs."""
    out = table('id:nowrap:dim', 'started:nowrap', 'source:nowrap', 'outcome:nowrap', 'message:grow')
    for info, outcome in found:
        label, style = OUTCOME.get(outcome, (outcome or '', 'dim'))
        out.add_row(info.run_id[:8], f'{info.started_at.astimezone():%Y-%m-%d %H:%M}', info.source,
                    Text(label, style=style), Text(info.message.replace('\n', ' ')))
    return out


def events(found: list[tuple]) -> Table:
    """(Event, its one-line text) pairs."""
    out = table('when:nowrap:dim', 'type:nowrap', 'what:grow')
    for event, text in found:
        out.add_row(f'{event.created_at.astimezone():%Y-%m-%d %H:%M}', event.type, Text(text))
    return out


def schedules(found: list[tuple]) -> Table:
    """(Schedule, its description) pairs."""
    out = table('id:nowrap:dim', 'when:nowrap', 'state:nowrap', 'message:grow')
    for schedule, described in found:
        out.add_row(str(schedule.id), described, Text('on', style='ok') if schedule.active else Text('off', 'dim'),
                    Text(schedule.message))
    return out


def checks(found) -> Table:
    """doctor's and audit's Check list: state, what, and the fix under anything that fails or warns."""
    out = table('state:nowrap', 'check:grow')
    for check in found:
        text = Text(check.text)
        if check.state in ('FAIL', 'warn') and check.fix:
            text.append('\nfix: ', style='dim')
            text.append(check.fix)
        out.add_row(Text(check.state, style=STATE_STYLE.get(check.state, '')), text)
    return out


def checks_summary(found) -> str:
    failed = sum(check.state == 'FAIL' for check in found)
    return ('[ok]All good.[/ok]' if not failed else
            f'[fail]{failed} problem{"s" if failed > 1 else ""} found.[/fail]')

