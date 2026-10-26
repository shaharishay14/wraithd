"""
python -m local_agent.evals: the golden set on one or more models, as a JSON report and a markdown table.

    uv run python -m local_agent.evals --scripted          # offline: scripted models, no key, no cost
    uv run python -m local_agent.evals --model openai:gpt-4o --model anthropic:claude-sonnet-5-5 \\
        --model ollama:qwen2.5:7b --max-cost 3

Real models are called only when named with --model. docs/evals.md has the whole procedure.
"""

import argparse
import asyncio
import sys
from pathlib import Path

from local_agent.evals.runner import DEFAULT_JUDGE, SetupError, check_setup, parse_price, parse_spec, run_evals
from local_agent.evals.tasks import TASK_IDS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='python -m local_agent.evals', description=__doc__.split('\n\n')[0])
    parser.add_argument('--model', action='append', default=[], metavar='PROVIDER:NAME',
                        help='a model to run the set on (repeat for more): openai:gpt-4o, '
                             'anthropic:claude-sonnet-5-5, ollama:qwen2.5:7b, scripted')
    parser.add_argument('--scripted', action='store_true',
                        help='offline run: scripted models and judge, no key, no network, no cost')
    parser.add_argument('--judge', default=None, metavar='PROVIDER:NAME|none',
                        help=f'the model that grades answers against the rubrics (default {DEFAULT_JUDGE}; '
                             '"none" skips the rubrics)')
    parser.add_argument('--max-cost', type=float, default=3.00, metavar='DOLLARS',
                        help='stop before the next call could take the total past this (default 3.00)')
    parser.add_argument('--price', action='append', default=[], metavar='NAME=IN,OUT',
                        help='$ per million input,output tokens for a model LiteLLM has no price for')
    parser.add_argument('--task', action='append', default=[], choices=TASK_IDS, help='only these tasks')
    parser.add_argument('--out', type=Path, default=Path('eval-results'),
                        help='where report.json, report.md and each run\'s home go (default ./eval-results)')
    args = parser.parse_args(argv)

    try:
        if args.scripted:
            specs, judge = [parse_spec('scripted')], parse_spec('scripted')
        elif not args.model:
            parser.error('name at least one --model (or --scripted for the offline run)')
        else:
            specs = [parse_spec(text) for text in args.model]
            judge_text = args.judge or DEFAULT_JUDGE
            judge = None if judge_text == 'none' else parse_spec(judge_text)
        prices = dict(parse_price(text) for text in args.price)
        check_setup(specs, judge, prices)
    except SetupError as e:
        print(e, file=sys.stderr)
        return 2

    print(f'Running {len(args.task) or len(TASK_IDS)} tasks on {", ".join(s.label for s in specs)}; judge: '
          f'{judge.label if judge else "none"}; cap ${args.max_cost:.2f}.')
    report = asyncio.run(run_evals(specs, judge, args.out, max_cost=args.max_cost, prices=prices,
                                   task_ids=args.task or None))
    print()
    print((args.out / 'report.md').read_text(), end='')
    print(f'\nWrote {args.out / "report.json"} and {args.out / "report.md"}.')
    return 1 if report.stopped else 0


if __name__ == '__main__':
    sys.exit(main())
