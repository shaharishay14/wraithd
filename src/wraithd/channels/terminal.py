"""
Chat in the terminal: the owner types, the answer streams back, and write actions ask y/N first.

Two looks (docs/cli.md). Plain (Terminal): lines in, text out, exactly as it always was; for pipes, scripts,
NO_COLOR and --plain. Rich (RichTerminal, on a real terminal): line editing and history, /commands with Tab,
answers as live Markdown, tool calls as one line each, approvals as a card with a picker.

One session is one conversation. Its messages are stored, so long sessions are compacted, and when the session
ends, distill turns it into memory: that's how "I prefer short replies" holds the next time.
"""

import asyncio
import getpass
import json
import sys
import time

from wraithd.core import compaction
from wraithd.mcp.client import McpConnections
from wraithd.memory import distill
from wraithd.run import Runtime, run
from wraithd.store import base as store_base

QUIT = {'/quit', '/exit', 'quit', 'exit', ':q'}
DISTILL_MIN_MESSAGES = 2  # at the end of a session, one exchange is enough to learn from


class Terminal:
    """Reading and writing, so tests can play the owner."""

    rich = False

    async def ask(self, prompt: str) -> str | None:
        """A line from the owner, or None when input ends (Ctrl-D)."""
        try:
            return await asyncio.to_thread(input, prompt)
        except EOFError:
            return None

    async def secret(self, prompt: str) -> str | None:
        """A line typed without echo (keys, tokens), or None when input ends."""
        try:
            return await asyncio.to_thread(getpass.getpass, prompt)
        except EOFError:
            return None

    def say(self, text: str = '', end: str = '\n') -> None:
        sys.stdout.write(text + end)
        sys.stdout.flush()

    # --- chat: the plain look --------------------------------------------------------------------------------

    def welcome(self, rt: Runtime) -> None:
        self.say('wraithd. Type /quit to end; what you share is remembered for next time.')

    async def message(self) -> str | None:
        return await self.ask('\n> ')

    def answered(self, rt: Runtime, result, state: dict) -> None:
        # A streamed answer is already on screen; a refusal or failure message never streamed
        self.say('' if state['streamed'] else result.answer)


def _approval_card(data: dict) -> str:
    lines = [f'\nThe agent wants to run {data["tool"]}:']
    lines += [f'  {key}: {json.dumps(value) if not isinstance(value, str) else value}'
              for key, value in data['arguments'].items()]
    if data.get('reason'):
        lines.append(f'  {data["reason"]}')
    for recipient in data.get('suspicious_recipients') or []:
        lines.append(f'  ! {recipient["value"]} was not written by you (it came from {recipient["source"]})')
    if data.get('untrusted_sources') and not data.get('suspicious_recipients'):
        lines.append(f'  ! This run read third-party content first: {", ".join(data["untrusted_sources"])}')
    return '\n'.join(lines)


def make_callback(rt: Runtime, terminal: Terminal, state: dict):
    """Streams text as it's written, shows tool calls, and turns approval cards into a y/N question."""
    if terminal.rich:
        return terminal.callback(rt, state)

    async def callback(event_type: str, data: dict) -> None:
        if event_type == 'content_delta':
            state['streamed'] = True
            terminal.say(data['content'], end='')
        elif event_type == 'tool_call_start':
            terminal.say(f'  · {data["tool"]}')
        elif event_type == 'approval_required':
            terminal.say(_approval_card(data))
            answer = (await terminal.ask('Allow? [y/N] ') or '').strip().lower()
            decision = store_base.APPROVED if answer in ('y', 'yes') else store_base.DENIED
            rt.store.decide_approval(data['approval_id'], decision)

    return callback


async def chat(rt: Runtime, terminal: Terminal | None = None) -> str:
    """One session. Returns the conversation id."""
    terminal = terminal or Terminal()
    async with McpConnections(rt.config.mcp_servers, store=rt.store) as mcp:  # once per session, not once per message
        for name, problem in mcp.errors.items():
            terminal.say(f'(MCP server {name} is not available this session: {problem})')
        try:
            return await _session(rt, terminal, mcp)
        finally:
            # /quit, Ctrl-D or Ctrl-C: the warm Claude Code sessions close now, while the event loop still runs
            await rt.close_sessions()


async def _session(rt: Runtime, terminal: Terminal, mcp) -> str:
    conversation = rt.store.start_conversation('terminal')
    terminal.welcome(rt)
    try:
        while True:
            message = await terminal.message()
            if message is None or message.strip().lower() in QUIT:
                break
            if not message.strip():
                continue
            if terminal.rich and (command := terminal.command_of(message)):
                if command == '/new':  # this conversation is remembered now, and the next one starts fresh
                    await _remember(rt, terminal, conversation.id)
                    conversation = rt.store.start_conversation('terminal')
                    terminal.new_conversation()
                else:
                    terminal.slash(rt, command)
                continue
            rt.store.add_message(conversation.id, 'user', message)
            history = await compaction.history_for(rt.store, conversation.id, rt.completer('compaction'))
            state = {'streamed': False}
            result = await run(rt, message, history=history, callback=make_callback(rt, terminal, state),
                               source='chat', mcp=mcp, conversation=conversation.id)
            terminal.answered(rt, result, state)
            rt.store.add_message(conversation.id, 'assistant', result.answer, run_id=result.run_id)
    finally:
        await _remember(rt, terminal, conversation.id)
    return conversation.id


async def _remember(rt: Runtime, terminal: Terminal, conversation_id: str) -> None:
    """Distill: what the owner shared in this conversation becomes memory."""
    try:
        outcome = await distill.distill_conversation(rt.store, rt.memory, rt.completer('distill'),
                                                     conversation_id, min_messages=DISTILL_MIN_MESSAGES)
        if outcome.get('notes'):
            terminal.say(f'Remembered: {", ".join(outcome["notes"])}')
    except Exception as e:
        terminal.say(f'(Could not save what I learned this time: {type(e).__name__})')


# --- the rich look ------------------------------------------------------------------------------------------

COMMANDS = {
    '/help': 'these commands, and the keys',
    '/new': 'end this conversation (it is remembered) and start a fresh one',
    '/runs': 'the latest runs',
    '/schedules': 'schedules and reminders',
    '/model': 'what wraithd thinks with',
    '/clear': 'clear the screen',
    '/quit': 'end the chat (Ctrl-D too)',
}
KEYS = (('Enter', 'send'), ('Esc Enter', 'a new line'), ('↑ ↓', 'earlier messages'), ('Tab', 'complete a /command'),
        ('Ctrl-D', 'quit'))
RECENT_RUNS = 5


def thinks_with(rt: Runtime) -> str:
    """'Claude subscription · sonnet', or 'openai · gpt-4o-mini'."""
    if rt.engine.get('name') == 'claude-code':
        return f'Claude subscription · {rt.engine.get("chat_model")}'
    return f'{rt.config.model.provider} · {rt.config.model.default}'


class RichTerminal(Terminal):
    """The terminal on a real TTY. input/output are prompt_toolkit's (tests pass a pipe and a DummyOutput);
    live=False prints answers once instead of redrawing them (tests read the output as text)."""

    rich = True

    def __init__(self, console=None, input=None, output=None, live: bool = True):
        from wraithd.tui import theme

        self.console = console or theme.console()
        self.input, self.output, self.live = input, output, live
        self._chat = None  # the chat's PromptSession, with its history: made on the first message

    def _session(self, **kwargs):
        from prompt_toolkit import PromptSession
        from prompt_toolkit.styles import Style

        from wraithd.tui.theme import PT_STYLE

        return PromptSession(input=self.input, output=self.output, style=Style.from_dict(PT_STYLE), **kwargs)

    async def ask(self, prompt: str) -> str | None:
        prompt = prompt.lstrip('\n')
        try:
            return await self._session().prompt_async([('class:accent', '? '), ('class:question', prompt)])
        except EOFError:
            return None

    async def secret(self, prompt: str) -> str | None:
        try:
            return await self._session().prompt_async([('class:accent', '? '), ('class:question', prompt)],
                                                      is_password=True)
        except EOFError:
            return None

    def say(self, text: str = '', end: str = '\n') -> None:
        self.console.print(text, end=end, markup=False)

    def show(self, renderable='', **kwargs) -> None:
        """Markup and rich renderables (say() prints text as it is)."""
        self.console.print(renderable, **kwargs)

    async def pick(self, question: str, options, default: str | None = None, free: bool = False) -> str | None:
        """A picker; the answer stays on screen in one line. None on Ctrl-D."""
        from rich.markup import escape

        from wraithd.tui.pick import options_of, pick

        options = options_of(options)
        value = await pick(question, options, default, free=free, input=self.input, output=self.output)
        if value is not None:
            shown = next((o.shown for o in options if o.value == value), value)
            self.console.print(f'[accent]✓[/accent] {escape(question)} [accent]{escape(shown)}[/accent]')
        return value

    def section(self, title: str) -> None:
        from rich.rule import Rule

        self.console.print()
        self.console.print(Rule(f'[title]{title}[/title]', align='left', style='dim'))

    # --- chat --------------------------------------------------------------------------------------------------

    def welcome(self, rt: Runtime) -> None:
        from rich.markup import escape

        self.show(f'[accent]◆ wraithd[/accent]  {escape(thinks_with(rt))}  [dim]{escape(short_path(rt.home.workspace))}'
                  '[/dim]', overflow='ellipsis', no_wrap=True)
        self.show('[dim]/help for commands · Esc Enter for a new line · Ctrl-D or /quit to end[/dim]')

    async def message(self) -> str | None:
        from prompt_toolkit.completion import WordCompleter
        from prompt_toolkit.history import InMemoryHistory
        from prompt_toolkit.key_binding import KeyBindings

        if self._chat is None:
            keys = KeyBindings()

            @keys.add('escape', 'enter')
            def _newline(event):
                event.current_buffer.insert_text('\n')

            self._chat = self._session(history=InMemoryHistory(), key_bindings=keys,
                                       completer=WordCompleter(list(COMMANDS), meta_dict=COMMANDS, sentence=True),
                                       complete_while_typing=True, prompt_continuation='  ')
        self.console.print()
        try:
            return await self._chat.prompt_async([('class:accent', '❯ ')])
        except EOFError:
            return None

    def command_of(self, message: str) -> str | None:
        """'/runs' for a /command; None for a message to the agent (a path like /etc/hosts goes to the agent)."""
        word = message.strip().split()[0].lower() if message.strip() else ''
        if word in COMMANDS:
            return word
        if word.startswith('/') and word[1:].isalpha() and len(message.split()) == 1:
            return word  # a typo of a command: slash() says it isn't one
        return None

    def new_conversation(self) -> None:
        self.show('[dim]New conversation.[/dim]')

    def slash(self, rt: Runtime, command: str) -> None:
        from rich.markup import escape

        from wraithd.tui import tables

        if command == '/help':
            table = tables.table('command:nowrap', 'what:grow')
            for name, what in COMMANDS.items():
                table.add_row(f'[accent]{name}[/accent]', what)
            for key, what in KEYS:
                table.add_row(f'[dim]{key}[/dim]', what)
            self.show(table)
        elif command == '/runs':
            found = rt.store.recent_runs(RECENT_RUNS)
            self.show(tables.runs([(info, run_outcome(rt.store, info.run_id)) for info in found]) if found
                      else '[dim]No runs yet.[/dim]')
        elif command == '/schedules':
            from wraithd.schedules import describe

            found = rt.store.list_schedules(active_only=True)
            self.show(tables.schedules([(s, describe(s)) for s in found]) if found else '[dim]No schedules.[/dim]')
        elif command == '/model':
            self.show(f'{escape(thinks_with(rt))}  [dim]change it with `wraithd onboard`[/dim]')
        elif command == '/clear':
            self.console.clear()
            self.welcome(rt)
        else:
            self.show(f'[warn]{escape(command)} isn\'t a command.[/warn] [dim]/help lists them.[/dim]')

    def callback(self, rt: Runtime, state: dict):
        from wraithd.tui.chat import AnswerView, approval_card
        from wraithd.tui.pick import Option

        view = state['view'] = AnswerView(self.console, live=self.live)

        async def callback(event_type: str, data: dict) -> None:
            if event_type == 'content_delta':
                state['streamed'] = True
                view.delta(data['content'])
            elif event_type == 'tool_call_start':
                view.tool_started(data['tool'])
            elif event_type == 'tool_result':
                view.finished(data['tool'], data['outcome'])
            elif event_type == 'approval_required':
                view.pause()
                self.console.print(approval_card(data))
                answer = await self.pick('Allow?', [Option('yes', 'Allow once'), Option('no', 'Deny')], 'no')
                approved = answer == 'yes'
                view.decided(data['tool'], approved)
                rt.store.decide_approval(data['approval_id'], store_base.APPROVED if approved else store_base.DENIED)

        return callback

    def answered(self, rt: Runtime, result, state: dict) -> None:
        from wraithd.core import views
        from wraithd.tui.chat import Answer, footer

        view = state.get('view')
        if view:
            view.close()
        if not state['streamed']:
            self.console.print(Answer(result.answer))
        events = rt.store.run_events(result.run_id)
        seconds = time.monotonic() - view.started if view else 0.0
        self.show(footer(views.tokens_used(events), seconds))


def short_path(path) -> str:
    """~/.wraithd/workspace rather than the whole path."""
    from pathlib import Path

    try:
        return '~/' + str(Path(path).relative_to(Path.home()))
    except ValueError:
        return str(path)


def run_outcome(store, run_id: str) -> str:
    from wraithd.core import inspector

    return inspector.summarize(store.run_events(run_id))['outcome']


def terminal_for() -> Terminal:
    """The rich terminal on a real TTY, the plain one everywhere else (wraithd.tui.plain)."""
    from wraithd import tui

    return Terminal() if tui.plain() else RichTerminal()
