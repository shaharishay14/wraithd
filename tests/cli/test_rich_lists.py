"""On a terminal: lists are tables, doctor's and audit's checks are colored, and a missing id is picked."""

import re
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from wraithd import cli, tui
from wraithd.config import Home, set_key
from wraithd.doctor import FAIL, INFO, OK, WARN, Check
from wraithd.store import SQLiteStore
from wraithd.tui import tables, theme


@pytest.fixture
def terminal(monkeypatch):
    """cli thinks it's on a terminal; its console writes plain text into capsys."""
    monkeypatch.setattr(tui, 'plain', lambda *a, **k: False)
    monkeypatch.setattr(theme, 'console', lambda file=None, **k: theme.Console(theme=theme.THEME, width=100,
                                                                               color_system=None, highlight=False))


def schedules(home):
    home.ensure()
    store = SQLiteStore(home.db_path)
    for message in ('water the plants', 'weekly review'):
        store.add_schedule(message, '0 8 * * 1', None, 'UTC', datetime(2026, 11, 9, 8, tzinfo=UTC))
    store.close()


def test_schedules_are_a_table(terminal, home, capsys):
    schedules(home)
    assert cli.main(['schedules', 'list']) == 0
    out = capsys.readouterr().out
    header, rule, first, second = out.splitlines()[:4]
    assert header.split() == ['id', 'when', 'state', 'message'] and set(rule.strip()) == {'─'}
    assert first.split()[0] == '1' and first.endswith('water the plants') and ' on ' in first
    assert first.index('water') == second.index('weekly')  # aligned


def test_a_missing_id_is_picked_and_the_command_goes_on(terminal, home, capsys):
    schedules(home)
    asked = []

    async def pick(question, options, default):
        asked.append((question, [o.value for o in options], default))
        return '2'

    assert cli.fill_missing_id(['schedules', 'rm'], pick=pick) == ['schedules', 'rm', '2']
    assert asked == [('Cancel which schedule?', ['1', '2'], '1')]
    # given ids, flags and --reason values are left alone
    assert cli.fill_missing_id(['schedules', 'rm', '1'], pick=pick) == ['schedules', 'rm', '1']
    assert cli.fill_missing_id(['--plain', 'runs', 'list'], pick=pick) == ['--plain', 'runs', 'list']
    assert cli.fill_missing_id(['runs', 'show', '--json', 'abc'], pick=pick) == ['runs', 'show', '--json', 'abc']


def test_nothing_to_pick_or_no_answer(terminal, home, capsys):
    async def stop(question, options, default):
        return None

    assert cli.fill_missing_id(['runs', 'show'], pick=stop) == 0 and capsys.readouterr().out == 'No runs yet.\n'
    assert cli.fill_missing_id(['key', 'rm'], pick=stop) == 0
    assert cli.fill_missing_id(['review', 'reject', '--reason', 'spam'], pick=stop) == 0
    set_key('openai', 'sk-test-123')
    assert cli.fill_missing_id(['key', 'rm'], pick=stop) == 1  # Ctrl-D in the picker


def test_checks_are_a_colored_table_with_the_same_exit_code(terminal, capsys):
    found = [Check(OK, 'Settings: fine'), Check(FAIL, 'No openai key', 'wraithd key set openai'),
             Check(WARN, 'Old Python', 'upgrade'), Check(INFO, 'Telegram: off')]
    module = SimpleNamespace(checks=lambda home: found, main=lambda home: pytest.fail('plain path'))
    assert cli.checks(Home.locate(), module) == 1
    out = capsys.readouterr().out
    assert re.search(r'FAIL +No openai key', out) and 'fix: wraithd key set openai' in out and '1 problem found.' in out
    console = theme.Console(theme=theme.THEME, record=True, color_system='truecolor', width=100)
    console.print(tables.checks(found))
    styled = console.export_text(styles=True)
    assert '\x1b[1;31mFAIL' in styled and '\x1b[32mok' in styled and '\x1b[33mwarn' in styled


def test_checks_in_plain_mode_are_the_module_s_own_lines(capsys):
    module = SimpleNamespace(checks=lambda home: pytest.fail('rich path'), main=lambda home: print('  ok    x') or 0)
    assert cli.checks(Home.locate(), module) == 0 and capsys.readouterr().out == '  ok    x\n'
