"""
python -m local_agent.evals: the golden set on one or more models, as a JSON report and a markdown table.

    uv run python -m local_agent.evals --scripted          # offline: scripted models, no key, no cost
    uv run python -m local_agent.evals --model openai:gpt-4o --model anthropic:claude-sonnet-5-5 \\
        --model ollama:qwen2.5:7b --runs 3 --max-cost 3

Real models are called only when named with --model. docs/evals.md has the whole procedure.
"""

import argparse
import asyncio
import sys
from pathlib import Path

from local_agent.evals.runner import DEFAULT_JUDGES, SetupError, check_setup, parse_price, parse_spec, run_evals
from local_agent.evals.tasks import TASK_IDS
from local_agent.providers import OLLAMA_URL


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='python -m local_agent.evals', description=__doc__.split('\n\n')[0])
    parser.add_argument('--model', action='append', default=[], metavar='PROVIDER:NAME',
                        help='a model to run the set on (repeat for more): openai:gpt-4o, '
                             'anthropic:claude-sonnet-5-5, ollama:qwen2.5:7b, scripted')
    parser.add_argument('--scripted', action='store_true',
                        help='offline run: scripted models and judge, no key, no network, no cost')
    parser.add_argument('--judge', action='append', default=[], metavar='PROVIDER:NAME|none',
                        help='a model that grades answers against the rubrics (repeat for more; default '
                             f'{" and ".join(DEFAULT_JUDGES)}; a rubric passes only if every judge passes it; '
                             '"none" skips the rubrics)')
    parser.add_argument('--runs', type=int, default=1, metavar='N',
                        help='run each task N times on each model (default 1); the table shows passes out of N')
    parser.add_argument('--ollama-url', default=OLLAMA_URL, metavar='URL',
                        help=f'where ollama: models are served: Ollama or any OpenAI-compatible API '
                             f'(default {OLLAMA_URL})')
    parser.add_argument('--max-cost', type=float, default=3.00, metavar='DOLLARS',
                        help='stop before the next call could take the total past this (default 3.00)')
    parser.add_argument('--price', action='append', default=[], metavar='NAME=IN,OUT',
                        help='$ per million input,output tokens for a model LiteLLM has no price for')
    parser.add_argument('--task', action='append', default=[], choices=TASK_IDS, help='only these tasks')
    parser.add_argument('--out', type=Path, default=Path('eval-results'),
                        help='where report.json, report.md and each run\'s home go (default ./eval-results)')
    args = parser.parse_args(argv)

    if args.runs < 1:
        parser.error('--runs must be 1 or more')
    try:
        if args.scripted:
            specs = [parse_spec('scripted')]
            judges = [parse_spec('scripted:judge-a'), parse_spec('scripted:judge-b')]  # two, as a paid run has
        elif not args.model:
            parser.error('name at least one --model (or --scripted for the offline run)')
        else:
            specs = [parse_spec(text) for text in args.model]
            judges = [parse_spec(text) for text in args.judge or DEFAULT_JUDGES if text != 'none']
        prices = dict(parse_price(text) for text in args.price)
        check_setup(specs, judges, prices, ollama_url=args.ollama_url)
    except SetupError as e:
        print(e, file=sys.stderr)
        return 2

    print(f'Running {len(args.task) or len(TASK_IDS)} tasks{f" x{args.runs}" if args.runs > 1 else ""} on '
          f'{", ".join(s.label for s in specs)}; judges: {", ".join(j.label for j in judges) or "none"}; '
          f'cap ${args.max_cost:.2f}.')
    report = asyncio.run(run_evals(specs, judges, args.out, max_cost=args.max_cost, runs=args.runs, prices=prices,
                                   task_ids=args.task or None, ollama_url=args.ollama_url))
    print()
    print((args.out / 'report.md').read_text(), end='')
    print(f'\nWrote {args.out / "report.json"} and {args.out / "report.md"}.')
    return 1 if report.stopped else 0


if __name__ == '__main__':
    sys.exit(main())
