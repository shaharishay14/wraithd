"""
What every built-in tool shares: errors the model can act on, and one way to turn an async function into an SDK
tool. Tools never raise into the loop: a failure is a result that says what went wrong and what to do next.
"""

import json
import logging
from typing import Any

from agents import FunctionTool

from local_agent.core.gate import parse_arguments

logger = logging.getLogger(__name__)


def tool_error(problem: str, next_step: str) -> dict[str, Any]:
    """A tool error the model can act on: what went wrong, and what to do next."""
    return {'error': f'{problem} Next step: {next_step}'}


def clamp_int(value, low: int, high: int, default: int) -> int:
    """Keeps a model-provided number within limits (models sometimes ask for 100 results, or pass "5")."""
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def function_tool(name: str, description: str, parameters: dict, fn) -> FunctionTool:
    """
    An SDK tool around `async fn(**arguments) -> dict | str`. Unexpected exceptions become an error result, so a bug
    in one tool can't end the run (the guard logs the error, and three in a row stop the run).
    """
    async def invoke(ctx, input_json: str) -> str:
        arguments = parse_arguments(input_json)
        try:
            result = await fn(**{key: value for key, value in arguments.items() if key in parameters['properties']})
        except Exception as e:
            logger.exception(f'{name} failed')
            result = tool_error(f'{name} failed unexpectedly ({type(e).__name__}).',
                                'Do not retry with the same arguments. Tell the user what you were trying to do.')
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)

    return FunctionTool(name=name, description=description, params_json_schema={'type': 'object', **parameters},
                        on_invoke_tool=invoke, strict_json_schema=False)
