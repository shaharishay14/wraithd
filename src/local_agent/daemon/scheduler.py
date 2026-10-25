"""
The scheduler: every minute, schedules.due(store, now) claims what's due, and each claimed schedule runs as an
unattended run (source 'schedule', no approval callback): no shell, and writes only if the owner pre-approved
them. The answer goes to the owner through the notifier (Telegram when paired, else the inbox).

Runs are at most once: a schedule is moved to its next time before it runs, so a crash mid-run loses that run
rather than repeating it (a repeat could send the same email twice).
"""

import asyncio
import logging
from datetime import UTC, datetime

from local_agent import schedules
from local_agent.run import run

logger = logging.getLogger(__name__)

TICK_SECONDS = 60


class Scheduler:
    def __init__(self, rt, mcp=None, clock=lambda: datetime.now(UTC)):
        self.rt, self.mcp, self.clock = rt, mcp, clock
        self.running = False

    async def tick(self, now: datetime | None = None) -> list[str]:
        """Runs everything due at `now` (concurrently). Returns the run ids."""
        due = schedules.due(self.rt.store, now or self.clock())
        results = await asyncio.gather(*(self._fire(schedule) for schedule in due))
        return [run_id for run_id in results if run_id]

    async def _fire(self, schedule) -> str | None:
        try:
            result = await run(self.rt, schedule.message, source='schedule', callback=None, mcp=self.mcp)
        except Exception:
            logger.exception(f'Schedule {schedule.id} failed')
            return None
        await self.rt.notifier.deliver(f'⏰ {schedule.message}\n\n{result.answer}')
        return result.run_id

    async def run_forever(self) -> None:
        self.running = True
        try:
            while True:
                await self.tick()
                # Wake at the start of the next minute, so "8:00" fires at 8:00:0x, not 8:00:59
                now = self.clock()
                await asyncio.sleep(TICK_SECONDS - now.second - now.microsecond / 1e6 + 0.05)
        finally:
            self.running = False
