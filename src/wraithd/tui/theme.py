"""
Monochrome like the site: the terminal's own foreground, grey and dim. Violet is only the accent (the prompt
marker, the picker's highlight, the header). Status colors: green ok, yellow warnings, red errors and FAIL.

No background color is ever set, so it reads the same on a dark and a light terminal.
"""

from rich.console import Console
from rich.theme import Theme

ACCENT = '#a78bfa'  # violet; rich steps it down on 256- and 16-color terminals
# The same names for prompt_toolkit (the picker and the chat input)
PT_STYLE = {
    'accent': f'{ACCENT} bold',
    'muted': '#888888',
    'question': 'bold',
    'selected': f'{ACCENT} bold',
    'typed': f'{ACCENT}',
    'nomatch': 'ansired',
}

THEME = Theme({
    'accent': f'bold {ACCENT}',
    'muted': 'dim',
    'title': 'bold',
    'ok': 'green',
    'warn': 'yellow',
    'fail': 'bold red',
    'markdown.code': 'bold',
    'markdown.code_block': 'none',
    'markdown.h1': 'bold',
    'markdown.h2': 'bold',
    'markdown.link': f'{ACCENT}',
    'markdown.link_url': 'dim underline',
    'markdown.block_quote': 'dim',
    'markdown.item.bullet': f'{ACCENT}',
    'markdown.item.number': f'{ACCENT}',
    'repr.number': 'none',
    'rule.line': 'dim',
})


def console(file=None, **kwargs) -> Console:
    """A rich console with the theme. highlight is off: nothing gets colored unless it's asked for."""
    return Console(theme=THEME, file=file, highlight=False, **kwargs)
