"""
One message in a conversation, the same way for every channel: store it, compact the history, run, store the
answer. Conversations run one message at a time, so a second message waits for the first answer instead of
racing it (the web API refuses it instead of waiting, see daemon/api.py).
"""

import asyncio

from local_agent.core import compaction
from local_agent.run import RunResult, Runtime, run

_locks: dict[str, asyncio.Lock] = {}


def lock_for(conversation_id: str) -> asyncio.Lock:
    return _locks.setdefault(conversation_id, asyncio.Lock())


async def reply(rt: Runtime, conversation_id: str, message: str, **run_options) -> RunResult:
    """Answers one message in the conversation. run_options go to run() (callback, source, owner, mcp...)."""
    async with lock_for(conversation_id):
        rt.store.add_message(conversation_id, 'user', message)
        history = await compaction.history_for(rt.store, conversation_id, rt.complete)
        result = await run(rt, message, history=history, **run_options)
        rt.store.add_message(conversation_id, 'assistant', result.answer, run_id=result.run_id)
        return result
