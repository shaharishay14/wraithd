"""
The harness's place inside the SDK loop: run hooks that log every model call and check the stop conditions
before the next one starts.
"""

import logging

from agents import RunHooks
from agents.exceptions import AgentsException

from local_agent.core import events as ev
from local_agent.core.stop import RunStopped, StopPolicy

logger = logging.getLogger(__name__)


class ModelUpgrade(AgentsException):
    """
    Raised before a model call when the run should go on with a stronger model (a skill that says
    `model: strong` was loaded). Like RunStopped, the SDK attaches the run so far, so the loop can continue it.
    """

    def __init__(self, model, skill: str):
        super().__init__(f'{skill} asks for the strong model')
        self.model, self.skill = model, skill


class HarnessHooks(RunHooks):
    def __init__(self, log: ev.EventLog, policy: StopPolicy | None, upgrade=None):
        self.log = log
        self.policy = policy  # None: log only, never stop (the forced final answer)
        self.upgrade = upgrade  # upgrade(events) -> (model, skill) or None; None: never switch

    async def on_llm_start(self, context, agent, system_prompt, input_items) -> None:
        # Checked here, before the call, so a stopped run never pays for one more model call
        reason = self.policy.check(self.log.events) if self.policy else None
        if reason:
            logger.warning(f'Stop condition {reason.code}: {reason.message}')
            self.log.append(ev.STOP, reason=reason.code, message=reason.message)
            raise RunStopped(reason)
        switch = self.upgrade(self.log.events) if self.upgrade else None
        if switch:
            model, skill = switch
            self.log.append(ev.MODEL_UPGRADE, skill=skill, model=str(getattr(model, 'model', model)))
            raise ModelUpgrade(model, skill)
        self.log.append(ev.TURN_START, turn=len(self.log.of_type(ev.TURN_START)) + 1, input_items=len(input_items))

    async def on_llm_end(self, context, agent, response) -> None:
        usage = getattr(response, 'usage', None)
        tool_calls = [item.name for item in response.output if getattr(item, 'type', None) == 'function_call']
        text_chars = sum(len(part.text) for item in response.output if getattr(item, 'type', None) == 'message'
                         for part in item.content if hasattr(part, 'text'))
        self.log.append(
            ev.MODEL_OUTPUT,
            input_tokens=getattr(usage, 'input_tokens', 0) or 0,
            output_tokens=getattr(usage, 'output_tokens', 0) or 0,
            tool_calls=tool_calls,
            text_chars=text_chars,
        )
