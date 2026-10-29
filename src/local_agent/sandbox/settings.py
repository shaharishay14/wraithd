"""
The [sandbox] settings and what makes them broken. One function, problems(), is used both by the code that builds
a sandbox (which refuses to run anything when there's a problem, never falling back to no sandbox) and by
`local-agent audit` (which names each problem as a FAIL).

    [sandbox]
    backend = "none"                  # none (the shell runs as you) | docker
    image = "debian:bookworm-slim"    # what the shell and sandboxed MCP servers run in
    network = "none"                  # none | bridge (the audit fails on bridge for the shell)
"""

import os
import re
from pathlib import Path

BACKENDS = ('none', 'docker')
NETWORKS = ('none', 'bridge')
DEFAULTS = {'backend': 'none', 'image': 'debian:bookworm-slim', 'network': 'none'}
# A docker image reference: no leading dash (it would be read as a flag), no spaces, nothing a shell would care about
IMAGE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}$')


def image_problem(image: str) -> str:
    if not IMAGE.match(image or ''):
        return f'[sandbox] image {image!r} is not a docker image name, e.g. "debian:bookworm-slim"'
    return ''


def _contains(outer: Path, inner: Path) -> bool:
    return inner == outer or outer in inner.parents


def mount_problem(home) -> str:
    """The shell's one mount is the real path of workspace/files/. It must not expose more than that folder."""
    files = Path(os.path.realpath(home.files_dir))
    if ',' in str(files):
        return f'workspace/files is at {files}, whose "," would break docker\'s --mount'
    for wider, what in ((Path('/'), 'the whole disk'), (Path(os.path.realpath(Path.home())), 'your home folder'),
                        (Path(os.path.realpath(home.root)), 'the state folder (config, state.db, the API token)')):
        if _contains(files, wider):
            return f'workspace/files is really {files}, which would mount {what} into the sandbox'
    return ''


def problems(values: dict, home, mcp: bool = False) -> list[str]:
    """
    Why this sandbox can't run, or [] when it can. mcp=True checks the settings a sandboxed MCP server uses (always
    docker, nothing mounted); otherwise the shell's, which only matter when backend is docker.
    """
    backend = values.get('backend')
    if not mcp:
        if backend not in BACKENDS:
            return [f'[sandbox] backend {backend!r} is not one of {", ".join(BACKENDS)}']
        if backend == 'none':
            return []
    found = [image_problem(values.get('image'))]
    if values.get('network') not in NETWORKS:
        found.append(f'[sandbox] network {values.get("network")!r} is not one of {", ".join(NETWORKS)}')
    if not mcp:
        found.append(mount_problem(home))
    return [problem for problem in found if problem]
