"""
The tool-result hook: one place every tool result passes through before the model sees it.

guard_function_tools (gate.py) calls the run's hooks with each result of a tool that ran, after secrets are
redacted and the result is logged, and before it's capped, wrapped as untrusted and given reminders. A hook
returns the text the model gets instead (say, a pointer to a file holding the full output). The gate still
tracks the full text for taint, and wraps a hook's text as untrusted when the result was, so a hook can't launder
third-party content. Denials and precheck answers don't pass through: they're the harness's own words.

Each worker registers its hooks inside its own block below. Hooks run in block order, each on the previous one's
text. A hook that raises is skipped (the text goes on unchanged) and logged: it never breaks the tool call.
"""

import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)

# hook(tool_name, text, untrusted) -> the text the model gets
Hook = Callable[[str, str, bool], str]


def result_hooks(rt, log) -> list[Hook]:
    """This run's hooks, in order. rt is the Runtime; log is the run's EventLog (for a hook's own events)."""
    hooks: list[Hook] = []
    # >>> security
    # <<< security
    # >>> efficiency
    # <<< efficiency
    # >>> capabilities
    # <<< capabilities
    # >>> reach
    # <<< reach
    # >>> evals
    # <<< evals
    # >>> sandbox
    # <<< sandbox
    return hooks


def apply(hooks, tool_name: str, text: str, untrusted: bool) -> str:
    """Runs the hooks in order. One that raises or returns a non-string is skipped, with a warning."""
    for hook in hooks:
        try:
            changed = hook(tool_name, text, untrusted)
        except Exception as e:
            logger.warning(f'Tool-result hook {getattr(hook, "__name__", hook)!r} failed on {tool_name}: '
                           f'{type(e).__name__}: {e}')
            continue
        if not isinstance(changed, str):
            logger.warning(f'Tool-result hook {getattr(hook, "__name__", hook)!r} returned {type(changed).__name__}')
            continue
        text = changed
    return text
