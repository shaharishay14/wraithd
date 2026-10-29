"""
A fake OpenAI-compatible server (what Ollama serves at /v1), on 127.0.0.1 with a free port, for the eval's local
model path: GET /v1/models and POST /v1/chat/completions, streamed or not.

It plays the golden scripts (evals/scripted.py) as Chat Completions. It keeps no state: the run is picked by the
owner's last message, and the turn by how many assistant messages follow it, so every request stands on its own.
Every request is kept in .requests, so a test can see what the harness really sent.
"""

import json
import threading
import time

import uvicorn
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from local_agent.core.delegate import CHILD_BRIEF
from local_agent.evals.scripted import SCRIPTS, say
from local_agent.evals.tasks import TASKS

REMINDER = '<system-reminder>'
USAGE = {'prompt_tokens': 1000, 'completion_tokens': 100, 'total_tokens': 1100}


CHILD, HEARTBEAT = 'subagent', 'heartbeat'  # runs that don't start from a task's message


def scripts_by_message() -> dict[str, list]:
    """
    The owner's message -> the turns that answer it. A task with a 'before' message plays up to its first answer.
    A subagent's child (its message is the child brief) and a heartbeat (the heartbeat prompt) get their own key.
    """
    found = {}
    for task in TASKS:
        turns = SCRIPTS[task['id']]
        if task['id'] == 'subagent-taint':  # parent: delegate, then (child: two turns), then parent again
            found[CHILD] = turns[1:3]
            turns = [turns[0], *turns[3:]]
        if task.get('kind') == 'heartbeat':
            found[HEARTBEAT] = turns
            continue
        if task.get('before'):
            first_answer = next(i for i, turn in enumerate(turns) if isinstance(turn[-1], ResponseOutputMessage))
            found.setdefault(task['before'], turns[:first_answer + 1])
            turns = turns[first_answer + 1:]
        found.setdefault(task['message'], turns)
    return found


def _text(content) -> str:
    if isinstance(content, list):
        return ''.join(part.get('text', '') for part in content if isinstance(part, dict))
    return content or ''


def _message(turn: list, model: str) -> tuple[dict, str]:
    """A scripted turn as a Chat Completions message, and its finish reason."""
    calls = [item for item in turn if isinstance(item, ResponseFunctionToolCall)]
    if calls:
        return {'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': item.call_id, 'type': 'function', 'function': {'name': item.name, 'arguments': item.arguments}}
            for item in calls]}, 'tool_calls'
    text = ''.join(part.text for item in turn for part in item.content)
    return {'role': 'assistant', 'content': text}, 'stop'


class FakeOpenAI:
    def __init__(self, scripts: dict[str, list] | None = None, model: str = 'fake-qwen'):
        self.scripts = scripts if scripts is not None else scripts_by_message()
        self.model = model
        self.requests: list[dict] = []
        self.app = Starlette(routes=[Route('/v1/models', self.models),
                                     Route('/v1/chat/completions', self.complete, methods=['POST'])])
        self._server = None
        self._thread = None
        self.url = ''

    async def models(self, request: Request):
        return JSONResponse({'object': 'list', 'data': [{'id': self.model, 'object': 'model', 'owned_by': 'fake'}]})

    def turn_for(self, messages: list[dict]) -> list:
        owner = [i for i, m in enumerate(messages)
                 if m.get('role') == 'user' and not _text(m.get('content')).startswith(REMINDER)]
        if not owner:
            return [say('(no message)')]
        last = owner[-1]
        text = _text(messages[last].get('content'))
        key = CHILD if text.startswith(CHILD_BRIEF) else HEARTBEAT if text.startswith('Heartbeat check') else text
        turns = self.scripts.get(key, [])
        played = sum(m.get('role') == 'assistant' for m in messages[last + 1:])
        return turns[played] if played < len(turns) else [say('(the script ran out)')]

    async def complete(self, request: Request):
        body = await request.json()
        self.requests.append(body)
        turn = self.turn_for(body.get('messages', []))
        if callable(turn):  # a turn that reads what it was given (the spill path in big-page)
            turn = turn(body.get('messages', []))
        message, finish = _message(turn, self.model)
        created = int(time.time())
        if not body.get('stream'):
            return JSONResponse({'id': 'chatcmpl-fake', 'object': 'chat.completion', 'created': created,
                                 'model': self.model, 'usage': USAGE,
                                 'choices': [{'index': 0, 'message': message, 'finish_reason': finish}]})

        def chunk(delta, finish_reason=None, usage=None):
            data = {'id': 'chatcmpl-fake', 'object': 'chat.completion.chunk', 'created': created, 'model': self.model,
                    'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish_reason}] if delta is not None
                    else []}
            if usage:
                data['usage'] = usage
            return f'data: {json.dumps(data)}\n\n'

        def stream():
            yield chunk({'role': 'assistant', 'content': ''})
            if message.get('tool_calls'):
                yield chunk({'tool_calls': [{**call, 'index': i} for i, call in enumerate(message['tool_calls'])]})
            else:
                yield chunk({'content': message['content']})
            yield chunk({}, finish)
            if (body.get('stream_options') or {}).get('include_usage'):
                yield chunk(None, usage=USAGE)
            yield 'data: [DONE]\n\n'

        return StreamingResponse(stream(), media_type='text/event-stream')

    def __enter__(self):
        config = uvicorn.Config(self.app, host='127.0.0.1', port=0, log_level='error', lifespan='off')
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError('the fake OpenAI server did not start')
            time.sleep(0.01)
        port = self._server.servers[0].sockets[0].getsockname()[1]
        self.url = f'http://127.0.0.1:{port}/v1'
        return self

    def __exit__(self, *exc):
        self._server.should_exit = True
        self._thread.join(timeout=10)
