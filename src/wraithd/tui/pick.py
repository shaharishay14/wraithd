"""
A picker: arrow keys and Enter, a letter shortcut, and typing still works.

    ↑ ↓ (or Ctrl-P, Ctrl-N)   move
    Enter                     pick the highlighted one (or, with free=True, what was typed)
    1-9                       pick that one
    typing                    jumps to the first option that starts with it, and picks it as soon as only one
                              option does ("y" picks yes, "openai-" picks openai-compatible). With free=True
                              (model names) it only jumps: Enter takes what was typed, or the exact option
    Backspace                 undo a typed letter
    Ctrl-D                    no answer (None); Ctrl-C raises KeyboardInterrupt, like everywhere else

Drawn inline under the question and erased when done; the caller prints the answer in one line.
"""

from dataclasses import dataclass

from prompt_toolkit.application import Application
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style

from wraithd.tui.theme import PT_STYLE


@dataclass(frozen=True)
class Option:
    value: str
    label: str = ''  # what's shown; the value if empty
    hint: str = ''  # a dim note after it

    @property
    def shown(self) -> str:
        return self.label or self.value


def options_of(choices) -> list[Option]:
    return [choice if isinstance(choice, Option) else Option(str(choice)) for choice in choices]


class Picker:
    """The picker's state, apart from the screen, so tests can drive it key by key."""

    def __init__(self, options, default: str | None = None, free: bool = False):
        self.options = options_of(options)
        if not self.options:
            raise ValueError('a picker needs at least one option')
        self.free = free
        self.typed = ''
        self.index = next((i for i, o in enumerate(self.options) if o.value == default), 0)

    def matches(self) -> list[int]:
        typed = self.typed.lower()
        return [i for i, o in enumerate(self.options)
                if o.value.lower().startswith(typed) or o.shown.lower().startswith(typed)]

    def move(self, step: int) -> None:
        self.typed = ''  # moving after typing means the owner is choosing from the list
        self.index = (self.index + step) % len(self.options)

    def type(self, char: str) -> str | None:
        """A typed character. Returns the value when that settles it."""
        if char.isdigit() and not self.typed and int(char) and int(char) <= min(len(self.options), 9):
            return self.options[int(char) - 1].value
        self.typed += char
        return self._jump()

    def backspace(self) -> None:
        self.typed = self.typed[:-1]
        self._jump(settle=False)

    def _jump(self, settle: bool = True) -> str | None:
        if not self.typed:
            return None
        found = self.matches()
        exact = [i for i in found if self.options[i].value.lower() == self.typed.lower()]
        if found:
            self.index = exact[0] if exact else found[0]
        if settle and len(found) == 1 and not self.free:  # free: the owner may be typing a name of their own
            return self.options[found[0]].value
        return None

    def enter(self) -> str | None:
        if self.typed and self.free:
            exact = [o.value for o in self.options if o.value.lower() == self.typed.lower()]
            return exact[0] if exact else self.typed
        if self.typed and not self.matches():
            return None
        return self.options[self.index].value

    def fragments(self, question: str) -> FormattedText:
        out = [('class:accent', '? '), ('class:question', question)]
        if self.typed:
            out.append(('class:typed' if self.matches() else 'class:nomatch', f'  {self.typed}'))
        out.append(('', '\n'))
        width = max(len(o.shown) for o in self.options)
        for i, option in enumerate(self.options):
            here = i == self.index
            out.append(('class:selected' if here else '', f'  {"❯" if here else " "} {option.shown:<{width}}'))
            if option.hint:
                out.append(('class:muted', f'  {option.hint}'))
            out.append(('', '\n'))
        out.append(('class:muted', '  ↑↓ move · Enter picks · or type' + (' any name' if self.free else '')))
        return FormattedText(out)


async def pick(question: str, options, default: str | None = None, *, free: bool = False,
               input=None, output=None) -> str | None:
    """The owner's pick (an option's value; with free=True, maybe something typed), or None on Ctrl-D."""
    state = Picker(options, default, free)
    keys = KeyBindings()

    @keys.add('up')
    @keys.add('c-p')
    def _up(event):
        state.move(-1)

    @keys.add('down')
    @keys.add('c-n')
    @keys.add('tab')
    def _down(event):
        state.move(1)

    @keys.add('enter')
    def _enter(event):
        value = state.enter()
        if value is not None:
            event.app.exit(result=value)

    @keys.add('backspace')
    def _back(event):
        state.backspace()

    @keys.add('c-d')
    def _eof(event):
        event.app.exit(result=None)

    @keys.add('c-c')
    def _interrupt(event):
        event.app.exit(exception=KeyboardInterrupt())

    @keys.add('<any>')
    def _char(event):
        char = event.data
        if len(char) == 1 and char.isprintable():
            value = state.type(char)
            if value is not None:
                event.app.exit(result=value)

    control = FormattedTextControl(lambda: state.fragments(question), focusable=True, show_cursor=False)
    app = Application(layout=Layout(HSplit([Window(control, dont_extend_height=True)])), key_bindings=keys,
                      style=Style.from_dict(PT_STYLE), erase_when_done=True, input=input, output=output)
    return await app.run_async()
