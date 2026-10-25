"""
The agent loop, as the harness runs it: one streamed SDK run with the harness hooks attached, and one honest
final answer (no tools) when a limit or stop condition ends the run early.
"""

import logging

from agents import Agent, Runner
from agents.exceptions import MaxTurnsExceeded
from openai.types.responses import ResponseTextDeltaEvent

from local_agent.core import events as ev
from local_agent.core.hooks import HarnessHooks
from local_agent.core.stop import RunStopped, StopPolicy, final_answer_instruction

logger = logging.getLogger(__name__)

# Max model calls with tools per user message before we force a final answer
MAX_AGENT_STEPS = 8

STEP_LIMIT_INSTRUCTION = (
    'You have reached the maximum number of tool steps. '
    'Answer the user now using only the information gathered so far. '
    'If the task is not complete, say what is missing.'
)


class RunAlreadyStartedError(Exception):
    """A run failed after it streamed text or called a tool. Retrying would show the answer twice or repeat a
    tool (e.g. send an email twice), so it is never retried."""


async def run_with_step_limit(agent: Agent, chat_history: list[dict], callback=None, log: ev.EventLog | None = None,
                              policy: StopPolicy | None = None, max_steps: int = MAX_AGENT_STEPS):
    """
    Runs the agent loop for at most max_steps model calls, stopping earlier if a stop condition fires. Either
    way the model gets one more call without tools, with everything it did so far, and must answer with what
    it has. Without this, an early stop could end in an answer that claims actions that never happened.
    """
    log = log or ev.EventLog()  # direct callers (tests) don't keep the log
    policy = policy or StopPolicy.default()
    progress = {'started': False, 'index': 0}
    try:
        try:
            return await stream_run(agent, chat_history, max_steps, callback, progress, HarnessHooks(log, policy))
        except (MaxTurnsExceeded, RunStopped) as e:
            if e.run_data is None:
                raise
            if isinstance(e, RunStopped):
                instruction = final_answer_instruction(e.reason)
            else:
                logger.warning(f'Agent hit step limit ({max_steps}), forcing final answer')
                log.append(ev.STOP, reason='max_turns', message=f'Reached {max_steps} model calls.')
                instruction = STEP_LIMIT_INSTRUCTION
            run_input = e.run_data.input
            history = list(run_input) if isinstance(run_input, list) else [{'role': 'user', 'content': run_input}]
            history += [item.to_input_item() for item in e.run_data.new_items]
            history.append({'role': 'system', 'content': instruction})
            # No stop policy on the final call: it has no tools, and it must be allowed to answer
            return await stream_run(agent.clone(tools=[]), history, 1, callback, progress,
                                    HarnessHooks(log, policy=None))
    except Exception as e:
        if progress['started'] and not isinstance(e, RunAlreadyStartedError):
            raise RunAlreadyStartedError(f'{type(e).__name__}: {e}') from e
        raise


async def stream_run(agent: Agent, run_input, max_turns: int, callback, progress, hooks: HarnessHooks | None = None):
    """
    One streamed Runner run. Text is sent to the channel as it is written (content_delta events, word by
    word). Marks progress as started once text or a tool call appears.
    """
    result = Runner.run_streamed(agent, run_input, max_turns=max_turns, hooks=hooks)
    async for event in result.stream_events():
        if event.type == 'raw_response_event' and isinstance(event.data, ResponseTextDeltaEvent):
            progress['started'] = True
            if callback and event.data.delta:
                await callback('content_delta', {'content': event.data.delta, 'index': progress['index']})
                progress['index'] += 1
        elif event.type == 'run_item_stream_event' and event.name == 'tool_called':
            progress['started'] = True
    return result
