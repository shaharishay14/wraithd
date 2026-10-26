"""
What the evals spend, call by call, and the cap on it.

Every model the runner uses (the ones under test and the judge) is wrapped in a MeteredModel. Before each call it
estimates what the call could cost (the input it's about to send, plus up to OUTPUT_ALLOWANCE output tokens) and
refuses it if that would take the total past --max-cost. After the call it adds the real cost, from the usage the
provider reported and LiteLLM's price list. Ollama costs 0.
"""

import dataclasses
import json
import os
from dataclasses import dataclass

from agents import Model
from agents.items import ModelResponse

OUTPUT_ALLOWANCE = 1024  # output tokens assumed for a call that hasn't happened yet
CHARS_PER_TOKEN = 3  # on the safe side: real text is closer to 4


class BudgetReached(Exception):
    """The next call could take the total past the cap, so it wasn't made."""


@dataclass(frozen=True)
class Price:
    input: float  # $ per input token
    output: float  # $ per output token

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return input_tokens * self.input + output_tokens * self.output


FREE = Price(0.0, 0.0)


def price_for(provider: str, name: str, overrides: dict[str, Price] | None = None) -> Price | None:
    """
    The price of one model: an override (--price), else free for local models, else LiteLLM's bundled price list.
    None: a paid model with no known price, which the runner refuses to run (it couldn't keep the cap).
    """
    overrides = overrides or {}
    if name in overrides:
        return overrides[name]
    if provider in ('ollama', 'scripted'):
        return FREE
    os.environ.setdefault('LITELLM_LOCAL_MODEL_COST_MAP', 'True')  # the bundled list: no download
    import litellm

    for key in (name, f'{provider}/{name}'):
        entry = litellm.model_cost.get(key) or {}
        if entry.get('input_cost_per_token') is not None and entry.get('output_cost_per_token') is not None:
            return Price(entry['input_cost_per_token'], entry['output_cost_per_token'])
    return None


class Meter:
    """The running total for one eval run, shared by every metered model."""

    def __init__(self, max_cost: float):
        self.max_cost = max_cost
        self.spent = 0.0
        self.calls = 0
        self.estimated_calls = 0  # calls whose provider reported no usage: charged at the estimate
        self.stopped = ''  # why the run stopped early, for the report

    def check(self, label: str, estimate: float) -> None:
        if self.spent + estimate > self.max_cost:
            self.stopped = (f'Stopped before a {label} call that could cost up to ${estimate:.4f}: '
                            f'${self.spent:.4f} spent of the ${self.max_cost:.2f} cap.')
            raise BudgetReached(self.stopped)

    def add(self, cost: float, estimated: bool = False) -> None:
        self.spent += cost
        self.calls += 1
        self.estimated_calls += estimated


def _chars(value) -> int:
    if isinstance(value, str):
        return len(value)
    return len(json.dumps(value, default=str))


class MeteredModel(Model):
    """A model behind the meter. Everything else is the wrapped model's own behaviour."""

    def __init__(self, inner: Model, meter: Meter, price: Price, label: str):
        self.inner, self.meter, self.price, self.label = inner, meter, price, label
        self.model = getattr(inner, 'model', label)  # what the event log records as the model

    def _estimate(self, system_instructions, input, tools) -> tuple[int, float]:
        schemas = [getattr(tool, 'params_json_schema', None) or {} for tool in tools or []]
        descriptions = [getattr(tool, 'description', '') or '' for tool in tools or []]
        chars = _chars(system_instructions or '') + _chars(input) + _chars(schemas) + _chars(descriptions)
        input_tokens = chars // CHARS_PER_TOKEN + 1
        return input_tokens, self.price.cost(input_tokens, OUTPUT_ALLOWANCE)

    def _charge(self, usage, estimate: float) -> None:
        input_tokens = getattr(usage, 'input_tokens', 0) or 0
        output_tokens = getattr(usage, 'output_tokens', 0) or 0
        if input_tokens + output_tokens == 0 and self.price != FREE:
            self.meter.add(estimate, estimated=True)  # no usage reported: assume the worst we allowed
        else:
            self.meter.add(self.price.cost(input_tokens, output_tokens))

    async def get_response(self, system_instructions, input, model_settings, tools, output_schema, handoffs,
                           tracing, **kwargs) -> ModelResponse:
        _, estimate = self._estimate(system_instructions, input, tools)
        self.meter.check(self.label, estimate)
        response = await self.inner.get_response(system_instructions, input, model_settings, tools, output_schema,
                                                 handoffs, tracing, **kwargs)
        self._charge(response.usage, estimate)
        return response

    async def stream_response(self, system_instructions, input, model_settings, tools, output_schema, handoffs,
                              tracing, **kwargs):
        _, estimate = self._estimate(system_instructions, input, tools)
        self.meter.check(self.label, estimate)
        # Streaming providers behind LiteLLM only report usage when asked
        model_settings = dataclasses.replace(model_settings, include_usage=True)
        usage = None
        async for event in self.inner.stream_response(system_instructions, input, model_settings, tools,
                                                      output_schema, handoffs, tracing, **kwargs):
            if getattr(event, 'type', '') == 'response.completed':
                usage = getattr(event.response, 'usage', None)
            yield event
        self._charge(usage, estimate)
