"""
Periodic jobs: work the daemon does on its own every so often, between runs. Background distill is one; a module
registers its own (name, interval, coroutine) inside its block in jobs().

The daemon runs each job in its own loop: wait the interval, run it, repeat. A job that fails is logged and runs
again next time: one bad round never stops it or the others.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from local_agent.memory import distill

logger = logging.getLogger(__name__)

DISTILL_EVERY_SECONDS = 3600


@dataclass(frozen=True)
class Job:
    name: str
    every_seconds: float
    run: Callable[[Any], Awaitable[Any]]  # async run(rt)


async def distill_all(rt) -> list[str]:
    """Background distill: every conversation with enough new messages, from any channel. Returns notes written."""
    written = []
    for conversation_id in rt.store.undistilled_conversations():
        conversation = rt.store.get_conversation(conversation_id)
        if conversation.source == 'notify':  # the agent's own notifications aren't the owner's words
            continue
        try:
            result = await distill.distill_conversation(rt.store, rt.memory, rt.completer('distill'),
                                                        conversation_id)
        except Exception as e:
            logger.warning(f'Distill of {conversation_id} failed: {type(e).__name__}')
            continue
        written += result.get('notes', [])
    return written


def jobs(rt) -> list[Job]:
    """The daemon's periodic jobs. rt.config.section(...) can set a job's interval, or leave it out when off."""
    found = [Job('distill', DISTILL_EVERY_SECONDS, distill_all)]
    # >>> security
    from local_agent.review import queue as review_queue

    found.append(Job('review_notify', 60, review_queue.notify_job))  # tells the owner what waits for review
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
    return found


async def run_forever(rt, job: Job, sleep=asyncio.sleep) -> None:
    """One job's loop, until cancelled."""
    while True:
        await sleep(job.every_seconds)
        try:
            await job.run(rt)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f'Periodic job {job.name} failed: {type(e).__name__}: {e}')
