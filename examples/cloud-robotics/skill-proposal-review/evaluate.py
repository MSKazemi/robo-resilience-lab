#!/usr/bin/env python3
"""Evaluate the SYNTHETIC skill-proposal corpus and print per-attack-class outcomes.

Usage (from the repository root, no install needed):
    python3 examples/cloud-robotics/skill-proposal-review/evaluate.py
    python3 examples/cloud-robotics/skill-proposal-review/evaluate.py --json
    python3 examples/cloud-robotics/skill-proposal-review/evaluate.py --case inj-01   # annotated audit trace

Exit codes: 0 every expectation met, 1 an expectation failed, 2 unreadable corpus.
Do not run with ``python3 -I``: isolated mode hides this script's directory from imports.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from skill_review import report  # noqa: E402


def main(argv):
    try:
        corpus = report.load_corpus()
    except (OSError, ValueError) as exc:
        print(f"cannot read corpus: {exc}", file=sys.stderr)
        return 2
    if "--case" in argv:
        i = argv.index("--case")
        if i + 1 >= len(argv):
            print("--case needs an id", file=sys.stderr)
            return 2
        wanted = [c for c in corpus["cases"] if c["id"] == argv[i + 1]]
        if not wanted:
            print(f"no such case: {argv[i + 1]}", file=sys.stderr)
            return 2
        case = wanted[0]
        res = report.run_case(case, corpus["contexts"])
        print(report.BANNER)
        print(json.dumps({"case": case, "result": res.as_dict()}, indent=2, sort_keys=True))
        return 0
    result = report.evaluate(corpus)
    if "--json" in argv:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(report.format_text(result))
    return 0 if report.exit_ok(result) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
