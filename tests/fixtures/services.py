"""
A fake service manager: launchctl and systemctl as `runner` sees them. It keeps whether the service is loaded and
running, answers `print`/`show` from that, and records every command, so tests check what would have been run
without touching the real launchd or systemd. Fake installers (uv, pipx) are answered too.
"""

import subprocess


class FakeServiceManager:
    def __init__(self, pid: int = 4242):
        self.calls: list[list[str]] = []
        self.loaded = self.running = False
        self.pid = pid
        self.fail: dict[str, str] = {}  # verb -> stderr: make that command fail
        self.upgrade_result = (0, 'Updated local-agent v0.0.1 -> v0.0.2', '')

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess:
        self.calls.append(list(argv))
        words = [word for word in argv[1:] if word != '--user']
        verb = words[0] if words else ''
        if verb in self.fail:
            return subprocess.CompletedProcess(argv, 1, '', self.fail[verb])
        if argv[0] in ('uv', 'pipx'):
            code, out, err = self.upgrade_result
            return subprocess.CompletedProcess(argv, code, out, err)
        if argv[0] == 'launchctl':
            return self._launchctl(argv, verb, words)
        if argv[0] == 'systemctl':
            return self._systemctl(argv, verb)
        raise AssertionError(f'unexpected command: {argv}')

    def _ok(self, argv, stdout=''):
        return subprocess.CompletedProcess(argv, 0, stdout, '')

    def _launchctl(self, argv, verb, words):
        if verb == 'print':
            if not self.loaded:
                return subprocess.CompletedProcess(argv, 113, '', 'Could not find service')
            state = f'\tstate = running\n\tpid = {self.pid}\n' if self.running else '\tstate = not running\n'
            return self._ok(argv, f'{words[1]} = {{\n\tactive count = 1\n{state}}}\n')
        if verb == 'bootstrap':
            self.loaded = self.running = True
        elif verb == 'bootout':
            self.loaded = self.running = False
        elif verb == 'kickstart':
            self.running = True
        return self._ok(argv)

    def _systemctl(self, argv, verb):
        if verb in ('start', 'restart'):
            self.running = True
        elif verb == 'stop' or (verb == 'disable' and '--now' in argv):
            self.running = False
        elif verb == 'show':
            return self._ok(argv, f'ActiveState={"active" if self.running else "inactive"}\n'
                                  f'MainPID={self.pid if self.running else 0}\n')
        return self._ok(argv)

    def commands(self, program: str) -> list[list[str]]:
        """The calls to one program, without the read-only status queries."""
        return [call for call in self.calls if call[0] == program and 'print' not in call and 'show' not in call]
