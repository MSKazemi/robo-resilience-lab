#!/usr/bin/env python3
"""Run the deterministic simulated fault suite (issue #59). SIMULATION ONLY.

    python3 examples/cloud-robotics/action-gate/run_fault_suite.py            # print the report
    python3 examples/cloud-robotics/action-gate/run_fault_suite.py --write    # refresh reports/
    python3 examples/cloud-robotics/action-gate/run_fault_suite.py --check    # byte-compare with reports/

Exit code 0 only if every scenario matched its expectations (failure_count 0, no
fail-open dispatch) and, with --check, the committed report is byte-identical.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from harness import render, run_suite  # noqa: E402
from scenarios import build_scenarios  # noqa: E402

REPORT = HERE / "reports" / "fault-suite.simulation.json"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--write", action="store_true", help="write reports/fault-suite.simulation.json")
    g.add_argument("--check", action="store_true", help="fail if the committed report differs")
    args = ap.parse_args(argv)
    report = run_suite(build_scenarios())
    text = render(report)
    totals = report["totals"]
    ok = totals["failure_count"] == 0 and totals["fail_open"] == 0
    if args.write:
        REPORT.parent.mkdir(exist_ok=True)
        REPORT.write_text(text, encoding="utf-8")
        print(f"wrote {REPORT}")
    elif args.check:
        current = REPORT.read_text(encoding="utf-8") if REPORT.exists() else None
        if current != text:
            print("DRIFT: committed report differs from a fresh run", file=sys.stderr)
            ok = False
    else:
        sys.stdout.write(text)
    print(f"scenarios={totals['scenarios']} deliveries={totals['deliveries']} "
          f"dispatched={totals['dispatched']} fail_open={totals['fail_open']} "
          f"false_blocks={totals['false_blocks']} failure_count={totals['failure_count']}",
          file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
