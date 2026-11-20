"""Command line: ``python3 -m triage <command>`` (run from the casebook directory).

Exit codes: 0 success, 1 problems found in the cases / drift / (with --strict) misclassified cases,
2 bad command line or unreadable single input.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import evaluate as E
from . import fixtures_io as F
from . import rules as R
from . import taxonomy as T
from . import validator as V

DEFAULT_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def _print_diags(rep):
    for d in rep.diags:
        print(f"{d.file}: {d.code} {d.path} {d.message}".replace("  ", " "), file=sys.stderr)


def cmd_validate(a):
    rep = V.validate_dir(a.dir)
    _print_diags(rep)
    print(f"{rep.entries_seen} entries seen, {rep.case_files} case files, {len(rep.valid_cases)} valid, {len(rep.diags)} diagnostics")
    return 0 if rep.ok else 1


def cmd_fixtures(a):
    if a.write:
        changed, problems = F.write(a.dir)
        for p in problems:
            print(p, file=sys.stderr)
        if problems:
            print("fixtures --write refused: nothing was written", file=sys.stderr)
            return 1
        print(f"wrote {len(changed)} file(s): {', '.join(changed) if changed else '(already canonical)'}")
        return 0
    problems = F.check(a.dir)
    for p in problems:
        print(p, file=sys.stderr)
    if problems:
        print(f"fixtures --check FAILED: {len(problems)} problem(s)", file=sys.stderr)
        return 1
    print("fixtures --check OK: every case file is canonical and MANIFEST.json is current")
    return 0


def cmd_evaluate(a):
    rep = V.validate_dir(a.dir)
    if not rep.ok:
        _print_diags(rep)
        print("evaluate refused: the cases do not validate", file=sys.stderr)
        return 1
    cases = [c for c in rep.valid_cases if a.split is None or c["split"] == a.split]
    result = E.evaluate(cases)
    sys.stdout.write(E.render_json(result) if a.json else E.render_text(result))
    return 1 if (a.strict and result["misclassified"]) else 0


def cmd_coverage(a):
    rep = V.validate_dir(a.dir)
    if not rep.ok:
        _print_diags(rep)
        return 1
    sys.stdout.write(E.render_coverage(E.coverage_table(rep.valid_cases)))
    return 0


def cmd_triage(a):
    obj, _text, diags = V.read_json_file(a.file)
    if diags:
        for d in diags:
            print(f"{a.file}: {d.code} {d.message}", file=sys.stderr)
        return 2
    evidence = obj.get("evidence") if type(obj) is dict and "evidence" in obj and "schema" in obj else obj
    t = R.triage(evidence)
    print(json.dumps(R.triage_to_dict(t), indent=2, sort_keys=True))
    print(T.BANNER, file=sys.stderr)
    return 0


def cmd_taxonomy(_a):
    print(T.BANNER)
    for l in T.LABELS:
        print(f"{l}\n    {T.LABEL_DESCRIPTIONS[l]}\n    next step: {T.LABEL_NEXT_STEP[l]}")
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="python3 -m triage", description=T.BANNER)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn, helptext in (("validate", cmd_validate, "validate every case file"),
                               ("fixtures", cmd_fixtures, "check or rewrite canonical fixtures + MANIFEST.json"),
                               ("evaluate", cmd_evaluate, "run the rule baseline over the cases and report")):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("--dir", type=Path, default=DEFAULT_DIR, help="case directory (default: fixtures/)")
        sp.set_defaults(fn=fn)
        if name == "fixtures":
            g = sp.add_mutually_exclusive_group(required=True)
            g.add_argument("--check", action="store_true", help="verify only; exit 1 on any drift")
            g.add_argument("--write", action="store_true", help="rewrite canonical files and the manifest")
        if name == "evaluate":
            sp.add_argument("--split", choices=T.SPLITS, default=None, help="only cases in this split")
            sp.add_argument("--json", action="store_true", help="machine-readable output")
            sp.add_argument("--strict", action="store_true", help="exit 1 if any case is misclassified")
    sp = sub.add_parser("coverage", help="count cases per expected label and role")
    sp.add_argument("--dir", type=Path, default=DEFAULT_DIR, help="case directory (default: fixtures/)")
    sp.set_defaults(fn=cmd_coverage)
    sp = sub.add_parser("triage", help="triage one case file (or a bare evidence object)")
    sp.add_argument("file", type=Path)
    sp.set_defaults(fn=cmd_triage)
    sp = sub.add_parser("taxonomy", help="print the label taxonomy")
    sp.set_defaults(fn=cmd_taxonomy)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
