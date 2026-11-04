"""
The terminal's look: a monochrome theme with a violet accent, pickers, tables, the chat view (docs/cli.md).

Plain mode is the terminal as it always was, byte for byte: whenever stdin or stdout isn't a terminal, NO_COLOR is
set, WRAITHD_PLAIN=1, or `wraithd --plain`. Scripts, pipes, --json, the daemon, Telegram and the web UI never
see any of this.
"""

import os
import sys

PLAIN_ENV = 'WRAITHD_PLAIN'


def plain(stdin=None, stdout=None, environ=None) -> bool:
    """True when the terminal should look exactly as it always did."""
    environ = os.environ if environ is None else environ
    if environ.get('NO_COLOR'):  # no-color.org: set and not empty
        return True
    if environ.get(PLAIN_ENV, '') not in ('', '0'):
        return True
    for stream in (stdin or sys.stdin, stdout or sys.stdout):
        try:
            if not stream.isatty():
                return True
        except (AttributeError, ValueError):  # replaced or closed streams
            return True
    return False


def set_plain() -> None:
    """`--plain`: for this process and anything it starts."""
    os.environ[PLAIN_ENV] = '1'
