"""A fake docker: records every argv (and the env the docker client would get) and runs nothing."""

from local_agent.sandbox.docker import Result


class FakeDocker:
    def __init__(self, *results):
        self.calls, self.results = [], list(results)

    async def __call__(self, argv, env, timeout):
        self.calls.append({'argv': argv, 'env': env, 'timeout': timeout})
        return self.results.pop(0) if self.results else Result(0, b'ran in the fake\n', b'')

    @property
    def argv(self):
        return self.calls[0]['argv']


def flag_values(argv, flag):
    """Every value given after `flag` in argv, e.g. every --mount."""
    return [argv[i + 1] for i, item in enumerate(argv[:-1]) if item == flag]
