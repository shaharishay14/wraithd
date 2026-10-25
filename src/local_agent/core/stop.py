"""
Stop conditions, checked before every model call. Without them an agent runs until it hits an API limit,
or keeps calling a broken tool. The turn limit itself stays with the SDK (max_turns).
"""

from dataclasses import dataclass
from datetime import datetime

from agents.exceptions import AgentsException

from local_agent.core import views

# Defaults until config (Phase 2) sets them. A laptop pays per token with the owner's own key, so the
# per-run limits stay on; the daily budget is off unless the owner sets one.
RUN_TOKEN_BUDGET = 200_000
RUN_MAX_TOOL_CALLS = 40


@dataclass(frozen=True)
class StopReason:
    code: str  # stable name, stored in the event log: tool_errors, repeated_call, token_budget
    message: str  # told to the model, so its final answer can explain why it stopped


class RunStopped(AgentsException):
    """
    Raised from the run hooks when a stop condition fires. It's an AgentsException so the SDK attaches what
    the run did so far (run_data), exactly as it does for MaxTurnsExceeded.
    """

    def __init__(self, reason: StopReason):
        super().__init__(reason.message)
        self.reason = reason


@dataclass(frozen=True)
class StopPolicy:
    max_consecutive_errors: int = 3
    max_identical_calls: int = 3  # the same tool with the same arguments: the model is going in circles
    token_budget: int | None = None  # input + output tokens for the whole run; None means no budget
    max_tool_calls: int | None = None  # per run, including ones the gate blocked
    daily_token_budget: int | None = None  # across all of today's runs
    tokens_before_run: int = 0  # what today's earlier runs had used when this one started

    @classmethod
    def default(cls, tokens_before_run: int = 0, daily_token_budget: int | None = None) -> 'StopPolicy':
        return cls(token_budget=RUN_TOKEN_BUDGET, max_tool_calls=RUN_MAX_TOOL_CALLS,
                   daily_token_budget=daily_token_budget, tokens_before_run=tokens_before_run)

    def over_daily_budget(self, used_this_run: int = 0) -> bool:
        return self.daily_token_budget is not None and self.tokens_before_run + used_this_run >= self.daily_token_budget

    def check(self, events) -> StopReason | None:
        errors = views.consecutive_tool_errors(events)
        if errors >= self.max_consecutive_errors:
            return StopReason('tool_errors', f'The last {errors} tool calls failed in a row, so the run was stopped.')

        tool, times = views.most_repeated_call(events)
        if times >= self.max_identical_calls:
            return StopReason('repeated_call',
                              f'{tool} was called {times} times with the same arguments, so the run was stopped.')

        calls = len(views.tools_called(events))
        if self.max_tool_calls is not None and calls >= self.max_tool_calls:
            return StopReason('tool_calls', f'The run made {calls} tool calls, its limit.')

        used = views.tokens_used(events)
        if self.token_budget is not None and used >= self.token_budget:
            return StopReason('token_budget', f'The run used {used} tokens, over its budget of {self.token_budget}.')
        if self.over_daily_budget(used):
            return StopReason('daily_budget', 'The daily usage limit was reached.')
        return None


def tokens_used_today(store, now: datetime | None = None) -> int:
    """Tokens today's runs used since local midnight, from their run_end events."""
    now = (now or datetime.now()).astimezone()
    return store.tokens_since(now.replace(hour=0, minute=0, second=0, microsecond=0))


DAILY_BUDGET_MESSAGE = "You've reached today's usage limit, so I can't work on this now. It resets at midnight."


def final_answer_instruction(reason: StopReason) -> str:
    """The last instruction when a stop condition fires: answer from what was gathered, and say why."""
    return (
        f'{reason.message} Do not call any more tools. '
        'Answer the user now using only the information gathered so far. '
        'If the task is not complete, say what is missing and why you stopped.'
    )
