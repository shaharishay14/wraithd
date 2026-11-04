"""
The wraithd icon (the site's: a ghost inside a round speech bubble), in half blocks: 40 columns, 20 rows.

Drawn in the terminal's own foreground, so the bubble is solid and the ghost is the background showing through,
on a dark terminal and a light one alike. Onboarding opens with it; plain mode never shows it.
"""

ICON = """\
             ▄▄▄▄██████▄▄▄▄
         ▄▄██████████████████▄▄
       ▄████████████████████████▄
     ▄█████████▀▀      ▀▀█████████▄
   ▄█████████▀            ▀█████████▄
  ▄█████████                █████████▄
 ▄█████████▀     ▄    ▄     ▀█████████▄
 ██████████      ██  ██      ██████████
▄██████████      ▀    ▀      ██████████▄
███████████                  ███████████
██████████                    ██████████
▀█████████▄▄                ▄▄█████████▀
 ███████████                ▀██████████
 ▀███████████                ▀████████▀
  ▀███████████▄                ▀▀▀███▀
   ▀████████████▄                  ▀█▄
     ▀█████████████▄▄▄▄     ▄▄▄▄▄    ▀
       ▀████████████████████████▀▀█▄
         ▀▀██████████████████▀▀
             ▀▀▀▀██████▀▀▀▀"""


def draw(console, subtitle: str = '') -> None:
    """The icon, then the name in the accent."""
    console.print(ICON, style='none', markup=False)
    console.print()
    console.print(' ' * 16 + '[accent]wraithd[/accent]')
    if subtitle:
        console.print(f'[muted]{subtitle}[/muted]', justify='left')
