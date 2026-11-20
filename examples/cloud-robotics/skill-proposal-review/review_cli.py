#!/usr/bin/env python3
"""Review one proposal read from stdin (student exercise). SYNTHETIC, non-actuating.

    echo '{"proposal_version":"skill-proposal/v1","skill":"get_status","robot_id":"sim_robot_1","args":{}}' \
      | python3 examples/cloud-robotics/skill-proposal-review/review_cli.py --context idle_fresh

Prints the typed result as JSON. Exit code: 0 ACCEPT, 3 NEEDS_HUMAN, 4 REJECT, 2 usage error.
Nothing is executed; an ACCEPT is not an authorisation.
Do not run with ``python3 -I``: isolated mode hides this script's directory from imports.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from skill_review import report  # noqa: E402
from skill_review.reviewer import ACCEPT, NEEDS_HUMAN, review  # noqa: E402
from skill_review.strict_json import MAX_INPUT_BYTES  # noqa: E402

EXIT = {ACCEPT: 0, NEEDS_HUMAN: 3}
# Read one byte more than the reviewer's cap so that oversized input is always seen as oversized
# (reading exactly the cap would silently truncate an oversized proposal into a valid-looking one).
READ_BYTES = MAX_INPUT_BYTES + 1


def build_parser():
    p = argparse.ArgumentParser(
        prog="review_cli.py",
        description="Review ONE simulated skill proposal read from stdin. SYNTHETIC, non-actuating; "
                    "ACCEPT is not an authorisation.",
        epilog="Exit codes: 0 ACCEPT, 3 NEEDS_HUMAN, 4 REJECT, 2 usage error.",
    )
    p.add_argument("--context", default="idle_fresh",
                   help="named simulated context from fixtures/corpus.json (default: idle_fresh)")
    return p


def main(argv, stdin=None):
    args = build_parser().parse_args(argv)  # --help exits 0; unknown flag / missing value exits 2
    contexts = report.load_corpus()["contexts"]
    if args.context not in contexts:
        print(f"unknown context {args.context!r}; choose from {sorted(contexts)}", file=sys.stderr)
        return 2
    stream = stdin if stdin is not None else sys.stdin
    if stream.isatty():
        print("no input: pipe one JSON proposal on stdin, e.g.  echo '{...}' | review_cli.py", file=sys.stderr)
        return 2
    data = stream.buffer.read(READ_BYTES)
    result = review(data, contexts[args.context])
    print(report.BANNER)
    print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
    return EXIT.get(result.verdict, 4)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
