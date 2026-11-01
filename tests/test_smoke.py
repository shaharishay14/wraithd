import subprocess
import sys

import pytest

import wraithd
from wraithd import cli


def test_version_is_set():
    assert wraithd.__version__


def test_cli_version(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(['--version'])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f'wraithd {wraithd.__version__}'


def test_cli_help(capsys):
    assert cli.main([]) == 0
    assert 'usage: wraithd' in capsys.readouterr().out


def test_module_entry_point():
    # Runs the real interpreter so packaging problems show up, not just import ones.
    result = subprocess.run(
        [sys.executable, '-m', 'wraithd.cli', '--version'],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.startswith('wraithd ')
