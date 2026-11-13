#!/usr/bin/env python3
"""Mutation check for the action gate (issue #59). Development tool; SIMULATION only.

Copies this directory (and the two files it reads from ``../mock-policy``) into a scratch
directory, flips ONE check at a time to fail-open (or removes one guard), runs the unit
tests against the scratch copy and expects them to FAIL. A mutant that leaves the tests
green SURVIVED, which is a hole in the tests. The working tree is never modified.

    python3 examples/cloud-robotics/action-gate/mutation_check.py            # run all
    python3 examples/cloud-robotics/action-gate/mutation_check.py --list     # show mutants
    python3 examples/cloud-robotics/action-gate/mutation_check.py --only M02 M16

Exit 0 only if the unmutated control passes and every mutant is killed.
The two report-drift tests are skipped in mutated runs (ACTION_GATE_MUTATION_RUN=1) so that a
mutant is only counted as killed by tests that check behaviour, not by a changed report.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
GC, SK = "gate_core.py", "sim_sink.py"

# (id, file, text that must occur exactly once, replacement, what the mutant breaks)
MUTANTS = [
    ("M01", GC, "    if mode is Mode.AUTONOMOUS:\n        return PASS", "    if mode is not None:\n        return PASS",
     "gate mode ignored (HOLD/INTERVENTION/SAFE_STOP/LOCKED no longer block)"),
    ("M02", GC, 'if msg["source_id"] not in cfg.allowed_sources:', "if False:", "source allow-list skipped"),
    ("M03", GC, 'if msg["model_revision"] not in revisions:', "if False:", "model revision approval skipped"),
    ("M04", GC, "if len(cmd) > 64 or cmd not in COMMANDS:", "if len(cmd) > 64:", "skill allow-list skipped"),
    ("M05", GC, "if len(domain) > 64 or domain not in cfg.allowed_domains:", "if len(domain) > 64:",
     "control-domain restriction skipped"),
    ("M06", GC, "if m != ctx.gate.mode.value:", "if False:", "message/gate mode mismatch ignored"),
    ("M07", GC, "if len(m) > 32 or m not in _MODE_VALUES:", "if len(m) > 32:", "unknown mode accepted"),
    ("M08", GC, "if not (lo <= v <= hi):", "if False:", "per-dimension bounds skipped"),
    ("M09", GC, "if type(v) is float and not math.isfinite(v):", "if False:", "NaN/inf check removed"),
    ("M10", GC, "def _is_int(v: Any) -> bool:\n    return type(v) is int",
     "def _is_int(v: Any) -> bool:\n    return isinstance(v, int)", "bool accepted as an integer"),
    ("M11", GC, "    return type(v) is int or type(v) is float",
     "    return isinstance(v, (int, float))", "bool accepted as a number"),
    ("M12", GC, "if seq == gate.last_sequence:", "if False:", "duplicate sequence accepted"),
    ("M13", GC, "if seq < gate.last_sequence:", "if False:", "out-of-order (older) sequence accepted"),
    ("M14", GC, 'if msg["epoch"] < gate.epoch:', "if False:", "stale epoch accepted (replay after restart)"),
    ("M15", GC, 'if msg["epoch"] > gate.epoch:', "if False:", "ungranted future epoch accepted"),
    ("M16", GC, "if now >= expires:", "if False:", "expiry not enforced"),
    ("M17", GC, "if created > now:", "if False:", "future timestamp accepted"),
    ("M18", GC, "if now - obs.tick >= cfg.observation_ttl_ticks:", "if False:", "observation age not enforced"),
    ("M19", GC, "    if obs is None:", "    if False:", "unknown observation not detected"),
    ("M20", GC, "if abs(targets[i] - obs.state[i]) > ctx.cfg.max_step[i]:", "if False:", "trajectory step bound skipped"),
    ("M21", GC, "# a crashing check blocks; it never allows\n                return Failure(",
     "# a crashing check blocks; it never allows\n                continue\n                return Failure(",
     "an exception inside a check is swallowed as PASS (fail-open)"),
    ("M22", GC, "            if res is PASS:\n                continue",
     "            if res is PASS or res is None:\n                continue", "a check returning None counts as PASS (fail-open)"),
    ("M23", GC, 'def check_args(ctx: _Ctx):\n    fn = _ARG_CHECKS.get(ctx.msg["command"])',
     'def check_args(ctx: _Ctx):\n    ctx.gate._last_seq = ctx.msg["sequence_id"]\n    fn = _ARG_CHECKS.get(ctx.msg["command"])',
     "a rejected action advances the accepted-sequence state"),
    ("M24", GC, "            self._watchdog(now)  # lazy: a late message cannot slip past a missed tick()", "            pass",
     "watchdog only runs on tick(); a late message slips through"),
    ("M25", GC, 'return self._blocked(msg, tick, Failure(Reason.AUDIT_FAILURE, "audit", "audit record not stored"))',
     "pass", "an unrecorded (unaudited) ALLOW is still allowed"),
    ("M26", GC, "        self._epoch = stored + 1\n        self._note(", "        self._epoch = stored\n        self._note(",
     "restart re-uses the old epoch (replay possible)"),
    ("M27", GC, "if not (_is_int(stored) and 0 <= stored < MAX_INT):", "if False:", "unreadable epoch store does not lock the gate"),
    ("M28", GC, 'if ctx.msg["simulated"] is not True:', "if False:", "non-simulated action accepted"),
    ("M29", GC, "    unknown = sorted(k for k in msg if k not in _FIELD_NAMES)\n    if unknown:",
     "    unknown = sorted(k for k in msg if k not in _FIELD_NAMES)\n    if False:", "unknown top-level fields ignored"),
    ("M30", GC, "del self._pending[decision.decision_id]  # single use, consumed even if refused below", "pass",
     "an ALLOW decision can be dispatched more than once"),
    ("M31", GC, "if issued is None or issued is not decision:", "if issued is None:",
     "a copied/forged decision with a valid id is accepted"),
    ("M32", GC, "self._pending.clear()  # decisions issued before a hold/stop/intervention are void", "pass",
     "decisions issued before a HOLD stay dispatchable after resume"),
    ("M33", SK, "if decision.verdict is not Verdict.ALLOW:", "if False:", "sink does not look at the verdict"),
    ("M34", SK, "or now >= decision.expires_tick:", "or False:", "sink dispatches an expired decision"),
    ("M35", SK, "if not _targets_ok(decision.targets):", "if False:", "sink second-line bounds check removed"),
    ("M36", SK, "        if not ok:\n            return self._refuse(decision, now, _CONFIRM_REFUSAL",
     "        if False:\n            return self._refuse(decision, now, _CONFIRM_REFUSAL", "sink ignores the gate's confirmation"),
    ("M37", GC, "    return Verdict.REJECT\n\n\n_MODE_REASON", "    return Verdict.ALLOW\n\n\n_MODE_REASON",
     "blocked reasons resolve to an ALLOW verdict"),
    ("M38", GC, "if now < self._last_now:", "if False:", "logical clock regression accepted"),
    ("M39", GC, "    if len(values) != ACTION_DIM:", "    if False:", "action dimension not enforced"),
    ("M40", GC, "        if u != JOINT_UNITS[i]:", "        if False:", "per-dimension units not enforced"),
    ("M41", GC, '    bad = _args_shape(ctx.msg["args"], ())', "    bad = None", "hold command accepts extra arguments"),
    ("M42", GC, '        if k in out:\n            raise ValueError("duplicate JSON key")', "        if False:\n            raise ValueError",
     "duplicate JSON keys accepted in text input"),
    ("M43", GC, "parse_constant=_no_constants", "parse_constant=None", "NaN/Infinity JSON literals accepted"),
    ("M44", GC, "_ID_RE.fullmatch(v) is not None", "_ID_RE.match(v) is not None",
     "identifier regex accepts a trailing newline / prefix match"),
    ("M45", GC, "            if oid in self._registry:", "            if False:", "duplicate observation id silently replaced"),
    ("M46", SK, "if self.mode is not Mode.AUTONOMOUS:", "if False:", "sink ignores its own non-autonomous mode"),
]


def _copy_tree(tmp: Path) -> Path:
    dst = tmp / "examples" / "cloud-robotics" / "action-gate"
    shutil.copytree(HERE, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    mp = tmp / "examples" / "cloud-robotics" / "mock-policy"
    mp.mkdir(parents=True)
    shutil.copy(HERE.parent / "mock-policy" / "contract.py", mp / "contract.py")
    (mp / "schema").mkdir()
    shutil.copy(HERE.parent / "mock-policy" / "schema" / "action.v1.schema.json",
                mp / "schema" / "action.v1.schema.json")
    return dst


def _run_tests(dst: Path) -> tuple:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", ACTION_GATE_MUTATION_RUN="1")
    p = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
                       cwd=dst, env=env, capture_output=True, text=True, timeout=300)
    tail = (p.stderr or "").strip().splitlines()[-1:] or [""]
    m = re.search(r"failures=(\d+)", p.stderr or "")
    e = re.search(r"errors=(\d+)", p.stderr or "")
    bad = int(m.group(1) if m else 0) + int(e.group(1) if e else 0)
    return p.returncode, bad, tail[0]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--only", nargs="*", default=None, metavar="ID")
    args = ap.parse_args(argv)
    if args.list:
        for mid, f, _old, _new, what in MUTANTS:
            print(f"{mid}  {f:13s} {what}")
        return 0
    chosen = [m for m in MUTANTS if args.only is None or m[0] in args.only]
    if not chosen:
        print("no mutants selected", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix="action-gate-mut-") as t:
        tmp = Path(t)
        dst = _copy_tree(tmp)
        code, bad, tail = _run_tests(dst)
        print(f"CONTROL (unmutated copy): exit={code} {tail}")
        if code != 0:
            print("control failed; mutation results would be meaningless", file=sys.stderr)
            return 1
        survived = []
        for mid, fname, old, new, what in chosen:
            path = dst / fname
            original = path.read_text(encoding="utf-8")
            if original.count(old) != 1:
                print(f"{mid}: pattern occurs {original.count(old)} times (need exactly 1): {old[:50]!r}", file=sys.stderr)
                return 2
            path.write_text(original.replace(old, new), encoding="utf-8")
            try:
                code, bad, tail = _run_tests(dst)
            finally:
                path.write_text(original, encoding="utf-8")
            status = "KILLED" if code != 0 else "SURVIVED"
            print(f"{mid} {status:8s} failing_tests={bad:<4d} {what}")
            if code == 0:
                survived.append(mid)
    print(f"mutants={len(chosen)} killed={len(chosen) - len(survived)} survived={len(survived)}"
          + (f" ({', '.join(survived)})" if survived else ""))
    return 0 if not survived else 1


if __name__ == "__main__":
    sys.exit(main())
