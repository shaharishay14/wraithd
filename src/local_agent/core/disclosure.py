"""
Progressive disclosure of connected-app actions: Search -> View -> Use.

Giving the model every action's JSON schema up front costs context on every call, and tool choice gets worse
as the list grows. Instead, past a few actions, the model gets three tools:

- find_app_actions(query):     a shallow list (action, app, one-line summary, access). No schemas.
- describe_app_action(action): one action's arguments, plus the app's guide the first time it's needed.
- run_app_action(action, arguments): runs it, only after it was described in this run.

The order is enforced here, in code: the prompt only explains it. Running an action goes through the same
guarded tool as a direct call, so the permission gate and the event log see the real action.
"""

import difflib
import json
import re
from typing import Any

from agents import FunctionTool

from local_agent.core import events as ev
from local_agent.core.gate import ToolAccess, classify_tool, parse_arguments

# Up to this many actions, schemas go to the model directly: discovery costs round trips, and with a
# handful of tools the schemas are cheap. Past it, the long tail is discovered on demand.
DIRECT_ACTIONS_LIMIT = 12

FIND_LIMIT = 8
SUMMARY_CHARS = 120
DESCRIPTION_CHARS = 600
PARAMETER_DESCRIPTION_CHARS = 160

FIND = 'find_app_actions'
DESCRIBE = 'describe_app_action'
RUN = 'run_app_action'
DISCOVERY_TOOLS = {FIND, DESCRIBE}  # read-only: they only read the catalog below

ACCESS_LABELS = {ToolAccess.READ: 'read', ToolAccess.WRITE: 'needs approval', ToolAccess.DENY: 'blocked'}


def app_of(action: str) -> str:
    return action.split('_', 1)[0]


def _words(text: str) -> list[str]:
    """Lowercase words, with a plural 's' dropped so 'emails' finds EMAIL."""
    return [word[:-1] if len(word) > 3 and word.endswith('s') else word
            for word in re.findall(r'[a-z0-9]+', (text or '').lower().replace('_', ' '))]


def _summary(description: str) -> str:
    first = re.split(r'(?<=[.!?])\s', (description or '').strip(), maxsplit=1)[0]
    return first if len(first) <= SUMMARY_CHARS else first[:SUMMARY_CHARS - 3] + '...'


class AppCatalog:
    """
    The connected-app actions of one run, by name. Holds the guarded tools that actually run them, and each
    app's guide (how to use it well), shown with the app's first describe.
    """

    def __init__(self, guarded_tools: list[FunctionTool], guides: dict[str, str] | None = None):
        self.tools: dict[str, FunctionTool] = {tool.name: tool for tool in guarded_tools}
        self.guides = {app.upper(): guide for app, guide in (guides or {}).items()}

    def __len__(self):
        return len(self.tools)

    @property
    def apps(self) -> list[str]:
        return sorted({app_of(name) for name in self.tools})

    def find(self, query: str, app: str | None = None, limit: int = FIND_LIMIT) -> list[str]:
        """Action names that match the query, best first. Words in the name count 3x the description."""
        wanted = set(_words(query))
        scored = []
        for position, (name, tool) in enumerate(self.tools.items()):
            if app and app_of(name) != app.upper():
                continue
            name_words, description_words = set(_words(name)), set(_words(tool.description))
            score = 3 * len(wanted & name_words) + len(wanted & description_words)
            if score or not wanted:
                scored.append((-score, position, name))
        return [name for _, _, name in sorted(scored)[:limit]]

    def suggestions(self, action: str) -> list[str]:
        return difflib.get_close_matches((action or '').upper(), self.tools, n=3, cutoff=0.5)


def _parameters(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """The arguments of an action in a compact form: name, type, required, short description, choices."""
    required = set(schema.get('required') or [])
    parameters = []
    for name, spec in (schema.get('properties') or {}).items():
        description = spec.get('description') or ''
        parameter = {'name': name, 'type': spec.get('type', 'any'), 'required': name in required}
        if description:
            parameter['description'] = description[:PARAMETER_DESCRIPTION_CHARS]
        if spec.get('enum'):
            parameter['choices'] = spec['enum']
        if 'default' in spec:
            parameter['default'] = spec['default']
        parameters.append(parameter)
    # required first, so the model sees what it must fill in before the optional extras
    return sorted(parameters, key=lambda parameter: not parameter['required'])


def _validation_error(schema: dict[str, Any], arguments: dict[str, Any]) -> str | None:
    properties = schema.get('properties') or {}
    missing = [name for name in schema.get('required') or [] if arguments.get(name) in (None, '')]
    unknown = [name for name in arguments if properties and name not in properties]
    problems = []
    if missing:
        problems.append(f'missing required arguments: {", ".join(missing)}')
    if unknown:
        problems.append(f'unknown arguments: {", ".join(unknown)} (valid: {", ".join(properties)})')
    return '; '.join(problems) or None


def described_actions(events) -> set:
    """Actions the model has viewed with describe_app_action in this run."""
    return {event.payload.get('arguments', {}).get('action', '').upper()
            for event in events if event.type == ev.TOOL_CALL and event.payload.get('tool') == DESCRIBE}


def discovery_tools(catalog: AppCatalog, log: ev.EventLog) -> list[FunctionTool]:
    """
    The three tools that replace the catalog's schemas. find and describe still go through the
    permission gate like any tool (they're read-only); run is a dispatcher whose check is the
    guarded action it calls.
    """
    guides_shown = set()  # apps whose guide this run's model has already seen

    async def find(ctx, input_json: str) -> str:
        arguments = parse_arguments(input_json)
        names = catalog.find(arguments.get('query', ''), app=arguments.get('app'))
        if not names:
            return json.dumps({
                'actions': [],
                'hint': f'Nothing matched. Connected apps: {", ".join(catalog.apps)}. '
                        f'Try other words, or call {FIND} with only "app" to list that app\'s actions.',
            })
        # Ordered by relevance, but no scores: the model should judge the summaries, not anchor on numbers
        return json.dumps({
            'actions': [{'action': name, 'app': app_of(name), 'summary': _summary(catalog.tools[name].description),
                         'access': ACCESS_LABELS[classify_tool(name)]} for name in names],
            'hint': f'Call {DESCRIBE} with the action you want, to see its arguments before running it.',
        })

    async def describe(ctx, input_json: str) -> str:
        action = (parse_arguments(input_json).get('action') or '').upper()
        tool = catalog.tools.get(action)
        if tool is None:
            return json.dumps({'error': f'There is no action {action!r}.', 'did_you_mean': catalog.suggestions(action),
                               'hint': f'Use {FIND} to search for actions.'})
        description = {
            'action': action,
            'app': app_of(action),
            'access': ACCESS_LABELS[classify_tool(action)],
            'description': (tool.description or '')[:DESCRIPTION_CHARS],
            'parameters': _parameters(tool.params_json_schema or {}),
            'hint': f'Call {RUN} with "action": "{action}" and "arguments" as an object.',
        }
        if app_of(action) not in guides_shown:
            guides_shown.add(app_of(action))
            guide = catalog.guides.get(app_of(action))
            if guide:
                description['app_guide'] = guide.strip()  # shown once per app per run, when first needed
        return json.dumps(description)

    async def run(ctx, input_json: str) -> str:
        arguments = parse_arguments(input_json)
        action = (arguments.get('action') or '').upper()
        action_arguments = arguments.get('arguments') or {}
        if isinstance(action_arguments, str):  # models sometimes send the object as a JSON string
            action_arguments = parse_arguments(action_arguments)

        tool = catalog.tools.get(action)
        if tool is None:
            problem = json.dumps({'error': f'There is no action {action!r}.',
                                  'did_you_mean': catalog.suggestions(action),
                                  'hint': f'Use {FIND} to search for actions.'})
        elif action not in described_actions(log.events):
            problem = json.dumps({'error': f'{action} has not been described in this run.',
                                  'hint': f'Call {DESCRIBE} with "action": "{action}" first, then run it.'})
        else:
            invalid = _validation_error(tool.params_json_schema or {}, action_arguments)
            problem = json.dumps({'error': f'{action} was not run: {invalid}.',
                                  'hint': f'Check the arguments from {DESCRIBE} and try again.'}) if invalid else None

        if problem:
            # Rejected by the harness before reaching the gate: logged so the stop conditions count it
            log.append(ev.TOOL_CALL, tool=action or RUN, arguments=action_arguments, access='unknown', allowed=False,
                       rejected_by='harness')
            log.append(ev.TOOL_RESULT, tool=action or RUN, outcome='error', chars=len(problem), truncated=False,
                       untrusted=False, preview=problem)
            return problem
        # The guarded tool checks the policy, logs the real action, caps and marks its output
        return await tool.on_invoke_tool(ctx, json.dumps(action_arguments))

    connected = ', '.join(catalog.apps)
    return [
        FunctionTool(
            name=FIND,
            description=(f'Search the actions available in the connected apps ({connected}). Returns a short list '
                         'of matching actions with a one-line summary each, and whether they need approval. '
                         f'Next, call {DESCRIBE} on the one you want.'),
            params_json_schema={'type': 'object', 'properties': {
                'query': {'type': 'string',
                          'description': 'What you want to do, e.g. "send email", "list unread messages"'},
                'app': {'type': 'string', 'description': f'Optional: only search one app ({connected})'},
            }, 'required': ['query']},
            on_invoke_tool=find,
            strict_json_schema=False,
        ),
        FunctionTool(
            name=DESCRIBE,
            description=('Show the arguments of one app action (from find_app_actions), and how to use the app. '
                         f'Required before {RUN}.'),
            params_json_schema={'type': 'object', 'properties': {
                'action': {'type': 'string', 'description': 'Action name, e.g. GMAIL_SEND_EMAIL'},
            }, 'required': ['action']},
            on_invoke_tool=describe,
            strict_json_schema=False,
        ),
        FunctionTool(
            name=RUN,
            description=(f'Run an app action you described with {DESCRIBE}. Actions that change data still need '
                         "the user's approval, like any other tool."),
            params_json_schema={'type': 'object', 'properties': {
                'action': {'type': 'string', 'description': 'Action name, e.g. GMAIL_SEND_EMAIL'},
                'arguments': {'type': 'object', 'description': 'The action\'s arguments, as described'},
            }, 'required': ['action', 'arguments']},
            on_invoke_tool=run,
            strict_json_schema=False,
        ),
    ]


def discovery_instructions(apps: list[str], action_count: int) -> str:
    """The short part of the system prompt that explains the flow (the tools enforce it)."""
    return (
        f"\nCONNECTED APPS: {', '.join(apps)} ({action_count} actions).\n"
        f"Their actions are not listed here. To use one: {FIND} (search) -> {DESCRIBE} (see its arguments) "
        f"-> {RUN} (do it). Search with what you want to do, in plain words.\n"
    )


def schema_chars(tools: list[FunctionTool]) -> int:
    """Roughly what the tool definitions cost in the model's context, in characters."""
    return sum(len(tool.name) + len(tool.description or '') + len(json.dumps(tool.params_json_schema or {}))
               for tool in tools)
