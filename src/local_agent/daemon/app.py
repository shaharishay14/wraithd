"""
The daemon: one async process with the local API (and the web UI), the scheduler, Telegram long polling, open MCP
connections and background distill. `local-agent daemon` runs it in the foreground; installing it as a service is
Phase 6.

Everything is opened once in start() and shared: MCP sessions, the Telegram bot, the notifier. Runs from every
channel use the same Runtime, so they share the store, memory and skills.
"""

import asyncio
import logging
import os
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from pathlib import Path

import uvicorn

from local_agent.channels.telegram import TelegramBot, TelegramChannel, TelegramNotifier
from local_agent.config import get_key
from local_agent.daemon.api import build_api
from local_agent.daemon.scheduler import Scheduler
from local_agent.daemon.security import Guard, load_or_create_token
from local_agent.mcp.client import McpConnections
from local_agent.memory import distill

logger = logging.getLogger(__name__)

HOST = '127.0.0.1'  # never 0.0.0.0: nothing else on the network may reach the agent
UI_ENV = 'LOCAL_AGENT_UI_DIR'
DISTILL_EVERY_SECONDS = 3600


def ui_dir() -> Path | None:
    """The built web UI: $LOCAL_AGENT_UI_DIR, else the ui/ folder in the package, if it has an index.html."""
    for candidate in (os.environ.get(UI_ENV), Path(__file__).resolve().parent.parent / 'ui'):
        if candidate and (Path(candidate) / 'index.html').is_file():
            return Path(candidate).resolve()
    return None


async def distill_all(rt) -> list[str]:
    """Background distill: every conversation with enough new messages, from any channel. Returns notes written."""
    written = []
    for conversation_id in rt.store.undistilled_conversations():
        conversation = rt.store.get_conversation(conversation_id)
        if conversation.source == 'notify':  # the agent's own notifications aren't the owner's words
            continue
        try:
            result = await distill.distill_conversation(rt.store, rt.memory, rt.complete, conversation_id)
        except Exception as e:
            logger.warning(f'Distill of {conversation_id} failed: {type(e).__name__}')
            continue
        written += result.get('notes', [])
    return written


class Daemon:
    def __init__(self, rt, telegram_transport=None, clock=lambda: datetime.now(UTC)):
        self.rt, self.clock = rt, clock
        self.telegram_transport = telegram_transport
        self.token = load_or_create_token(rt.home.root)
        self.port = rt.config.daemon_port
        self.mcp = None
        self.telegram: TelegramChannel | None = None
        self.scheduler = Scheduler(rt, clock=clock)
        self.background: set[asyncio.Task] = set()  # web chat runs that outlive their request
        self._stack = AsyncExitStack()

    @property
    def url(self) -> str:
        return f'http://{HOST}:{self.port}/#token={self.token}'

    async def start(self) -> None:
        await self._stack.__aenter__()
        # Owner-only, long-lived: every owner run in the daemon shares these sessions
        self.mcp = await self._stack.enter_async_context(McpConnections(self.rt.config.mcp_servers))
        self.scheduler.mcp = self.mcp
        token = get_key('telegram')
        if token:
            bot = TelegramBot(token, self.telegram_transport)
            self._stack.push_async_callback(bot.close)
            self.telegram = TelegramChannel(self.rt, bot, mcp=self.mcp, clock=self.clock)
            self.rt.notifier = TelegramNotifier(self.rt.store, bot)

    async def stop(self) -> None:
        if self.telegram:
            await self.telegram.settle()
        if self.background:
            await asyncio.gather(*list(self.background), return_exceptions=True)
        await self._stack.__aexit__(None, None, None)

    def asgi(self):
        """The API and UI behind the Host, Origin and token checks."""
        return Guard(build_api(self, ui_dir()), self.token, self.port, self.rt.config.dev_origin)

    def uvicorn_config(self) -> uvicorn.Config:
        return uvicorn.Config(self.asgi(), host=HOST, port=self.port, lifespan='off', log_level='warning',
                              server_header=False, date_header=False)

    async def _distill_forever(self) -> None:
        while True:
            await asyncio.sleep(DISTILL_EVERY_SECONDS)
            await distill_all(self.rt)

    async def serve(self, announce=lambda text: print(text, flush=True)) -> None:
        await self.start()
        try:
            server = uvicorn.Server(self.uvicorn_config())
            tasks = [asyncio.create_task(server.serve()), asyncio.create_task(self.scheduler.run_forever()),
                     asyncio.create_task(self._distill_forever())]
            if self.telegram:
                tasks.append(asyncio.create_task(self.telegram.run_forever()))
            announce(f'Local Agent is running. Open {self.url}')
            for name, problem in (self.mcp.errors if self.mcp else {}).items():
                announce(f'(MCP server {name} is not available: {problem})')
            announce('Telegram: ' + ('on' if self.telegram else 'off (local-agent key set telegram)'))
            # The server ends on Ctrl-C; then stop everything else with it
            await tasks[0]
            for task in tasks[1:]:
                task.cancel()
            await asyncio.gather(*tasks[1:], return_exceptions=True)
        finally:
            await self.stop()
