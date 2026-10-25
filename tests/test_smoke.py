import subprocess
import sys

import pytest

import local_agent
from local_agent import cli


def test_version_is_set():
    assert local_agent.__version__


def test_cli_version(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"local-agent {local_agent.__version__}"


def test_cli_help(capsys):
    assert cli.main([]) == 0
    assert "usage: local-agent" in capsys.readouterr().out


def test_module_entry_point():
    # Runs the real interpreter so packaging problems show up, not just import ones.
    result = subprocess.run(
        [sys.executable, "-m", "local_agent.cli", "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.startswith("local-agent ")
