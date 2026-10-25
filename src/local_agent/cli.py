"""The `local-agent` command. Subcommands arrive with later phases."""

import argparse

from local_agent import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="local-agent",
        description="A personal agent that runs on your own machine.",
    )
    parser.add_argument("--version", action="version", version=f"local-agent {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    # No subcommands yet, so a bare invocation just explains itself.
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
