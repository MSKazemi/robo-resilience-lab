#!/usr/bin/env python3
"""Measure how long Kubernetes takes to replace and ready ONE deleted pod (live collector).

DRY RUN BY DEFAULT: without ``--yes`` this script only lists the pods that match
``--namespace`` + ``--selector`` and says which pod it WOULD delete; it never deletes.
It does run read-only ``kubectl`` calls (``config current-context``, ``get pods``).

To delete one pod and measure, ALL of the following are required:
  --namespace NS --selector key=value[,key=value]   (no defaults, no name-prefix guessing)
  --yes
  --context NAME            or, if you really mean the current context, --yes-context-is-correct
and, if the selector matches more than one pod, --target-pod NAME (never "the first one").

What is measured (see README.md): Kubernetes object existence and Ready condition of the
replacement pod, on the local monotonic clock. It does NOT measure ROS application
recovery. Parsing and arithmetic live in ``recovery_metrics.py``.

Exit status: 0 ok; 1 a primary measurement failed (no/ambiguous replacement, never Ready);
2 usage or safety-guard refusal; 3 a kubectl call failed; 4 a poll timed out; 5 could not
write output; 130 interrupted.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import math
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

import recovery_metrics as rm

TOOL = "RH.py"
DEFAULT_OUT_DIR = Path(__file__).resolve().parent / "output" / "recovery"
MIN_TIMEOUT_S, MAX_TIMEOUT_S = 1.0, 3600.0
MIN_INTERVAL_S, MAX_INTERVAL_S = 0.1, 60.0
MAX_LIVE_POLLS = 20_000          # hard cap independent of the clock
KUBECTL_CALL_TIMEOUT_S = 30.0
_ALLOWED_VERBS = ("get", "delete", "config")


class Kubectl:
    """The ONLY place that starts a ``kubectl`` process.

    * every call is a list (no shell), with a timeout, stdin closed;
    * the return code is always checked (``KubectlError`` otherwise);
    * ``delete`` is refused unless ``allow_delete=True`` (set only by ``--yes``);
    * tests inject ``runner`` (same signature as ``subprocess.run``).
    """

    def __init__(
        self,
        *,
        context: Optional[str] = None,
        allow_delete: bool = False,
        timeout_s: float = KUBECTL_CALL_TIMEOUT_S,
        runner: Callable[..., Any] = subprocess.run,
        binary: str = "kubectl",
    ) -> None:
        self.context = rm.validate_context(context) if context is not None else None
        self.allow_delete = allow_delete
        self.timeout_s = timeout_s
        self._runner = runner
        self._binary = binary

    def run(self, args: Sequence[str]) -> str:
        argv = list(args)
        if not argv or not all(isinstance(a, str) and a and "\x00" not in a for a in argv):
            raise rm.GuardError("kubectl arguments must be non-empty strings")
        verb = argv[0]
        if verb not in _ALLOWED_VERBS:
            raise rm.GuardError(f"kubectl verb {verb!r} is not allowed by this tool")
        if verb == "delete" and not self.allow_delete:
            raise rm.GuardError("refusing to run 'kubectl delete' without --yes")
        if verb == "config" and argv != ["config", "current-context"]:
            raise rm.GuardError("only 'kubectl config current-context' is allowed")
        cmd = [self._binary]
        if self.context is not None and verb != "config":
            cmd += ["--context", self.context]
        cmd += argv
        try:
            done = self._runner(
                cmd, capture_output=True, text=True, timeout=self.timeout_s,
                check=False, stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            raise rm.KubectlError(f"kubectl {verb} timed out after {self.timeout_s:g}s") from None
        except OSError as exc:
            raise rm.KubectlError(f"cannot run kubectl: {exc.strerror or type(exc).__name__}") from None
        code = getattr(done, "returncode", None)
        if isinstance(code, bool) or not isinstance(code, int) or code != 0:
            err = getattr(done, "stderr", "") or ""
            raise rm.KubectlError(
                f"kubectl {verb} returned {code!r}: {rm._clean(str(err).strip(), 300)}"
            )
        out = getattr(done, "stdout", None)
        if not isinstance(out, str):
            raise rm.KubectlError(f"kubectl {verb} produced no text output")
        return out


# ------------------------------------------------------------------ argument handling
def _finite_float(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError("must be finite")
    return value


def build_parser(prog: str = TOOL) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=prog,
        allow_abbrev=False,
        description="Measure pod replacement (Kubernetes object + Ready) after deleting ONE pod. "
        "Dry run unless --yes. Does not measure ROS application recovery.",
    )
    p.add_argument("--namespace", required=True, help="namespace of the workload (required, no default)")
    p.add_argument("--selector", required=True, help="equality label selector, e.g. app=talker (required)")
    p.add_argument("--target-pod", help="pod to delete; required if the selector matches more than one pod")
    p.add_argument("--owner-uid", help="only consider pods whose controller ownerReference has this UID")
    p.add_argument("--context", help="kubectl context to use for every call")
    p.add_argument("--yes-context-is-correct", action="store_true",
                   help="with --yes and no --context: confirm that the CURRENT kubectl context is the intended one")
    p.add_argument("--yes", action="store_true", help="really delete the pod (default: dry run, nothing is deleted)")
    p.add_argument("--timeout-replacement", type=_finite_float, default=120.0, metavar="S",
                   help="max seconds to wait for a replacement pod (default %(default)s)")
    p.add_argument("--timeout-ready", type=_finite_float, default=300.0, metavar="S",
                   help="max seconds to wait for Ready after the replacement appears (default %(default)s)")
    p.add_argument("--poll-interval", type=_finite_float, default=1.0, metavar="S",
                   help="seconds between polls (default %(default)s)")
    p.add_argument("--with-events", action="store_true",
                   help="also fetch the replacement pod's events (auxiliary Scheduled-event measurement)")
    p.add_argument("--sample-id", help="identifier for this sample (default: rh-<UTC time>)")
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR,
                   help="where observation/CSV/JSON are written (never overwritten); default %(default)s")
    return p


def validate_args(args: argparse.Namespace) -> None:
    """Safety guards; raises GuardError/MalformedInputError BEFORE any kubectl call."""
    rm.validate_namespace(args.namespace)
    rm.parse_selector(args.selector)
    if args.target_pod is not None:
        rm.validate_pod_name(args.target_pod, "--target-pod")
    if args.owner_uid is not None:
        rm._require_pattern(args.owner_uid, rm._UID_RE, "--owner-uid")
    if args.context is not None:
        rm.validate_context(args.context)
    if args.sample_id is not None:
        rm.validate_sample_id(args.sample_id)
    for name, value, lo, hi in (
        ("--timeout-replacement", args.timeout_replacement, MIN_TIMEOUT_S, MAX_TIMEOUT_S),
        ("--timeout-ready", args.timeout_ready, MIN_TIMEOUT_S, MAX_TIMEOUT_S),
        ("--poll-interval", args.poll_interval, MIN_INTERVAL_S, MAX_INTERVAL_S),
    ):
        if not lo <= value <= hi:
            raise rm.GuardError(f"{name} must be between {lo:g} and {hi:g}")
    if args.yes and args.context is None and not args.yes_context_is_correct:
        raise rm.GuardError(
            "--yes requires --context NAME, or --yes-context-is-correct to accept the current kubectl context"
        )


# ------------------------------------------------------------------ live collection
def list_pods(kubectl: Kubectl, namespace: str, selector: str) -> Dict[str, Any]:
    text = kubectl.run(["get", "pods", "-n", namespace, "-l", selector, "-o", "json"])
    return rm.sanitize_pod_list(rm.load_json_strict(text), "kubectl get pods output")


def choose_target(views: Sequence[rm.PodView], wanted: Optional[str]) -> rm.PodView:
    live = [v for v in views if not v.terminating]
    if wanted is not None:
        matches = [v for v in live if v.name == wanted]
        if not matches:
            raise rm.TargetPodNotFoundError(f"--target-pod {wanted} is not a live pod matching the selector")
        return matches[0]
    if not live:
        raise rm.TargetPodNotFoundError("no live pod matches the namespace and selector")
    if len(live) > 1:
        names = ", ".join(sorted(v.name for v in live)[:10])
        raise rm.AmbiguousMatchError(f"{len(live)} pods match ({names}); choose one with --target-pod")
    return live[0]


def _new_observation(args: argparse.Namespace, sample_id: str, context: Optional[str]) -> Dict[str, Any]:
    return {
        "schema": rm.OBSERVATION_SCHEMA,
        "sample_id": sample_id,
        "synthetic": False,
        "namespace": args.namespace,
        "selector": args.selector,
        "owner_uid": args.owner_uid,
        "kubectl_context": context,
        "baseline_pods": {"items": []},
        "target_uid": None,
        "target_pod": None,
        "delete_requested_mono_s": None,
        "delete_request_wall": None,
        "polls": [],
        "events": None,
        "exit_status": 0,
        "run_error": None,
        "probe": None,
        "tool": TOOL,
    }


def collect(
    kubectl: Kubectl,
    obs: Dict[str, Any],
    target: rm.PodView,
    baseline: Sequence[rm.PodView],
    args: argparse.Namespace,
    *,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    wall: Callable[[], datetime],
    say: Callable[[str], None],
) -> None:
    """Delete ``target`` then poll (bounded) until the replacement is Ready. Mutates ``obs``."""
    selector = rm.parse_selector(args.selector)
    t0 = clock()
    obs["delete_requested_mono_s"] = t0
    obs["delete_request_wall"] = wall().astimezone(timezone.utc).isoformat()
    say(f"deleting pod {target.name} (uid {target.uid}) in namespace {args.namespace}")
    kubectl.run(["delete", "pod", target.name, "-n", args.namespace, "--wait=false"])

    deadline = t0 + args.timeout_replacement
    baseline_uids = {v.uid for v in baseline}
    new_uids: List[str] = []
    found_uid: Optional[str] = None
    for _ in range(MAX_LIVE_POLLS):
        pods = list_pods(kubectl, args.namespace, args.selector)
        now = clock()
        obs["polls"].append({"mono_s": now, "pods": pods})
        selected = rm.select_pods(rm.parse_pod_list(pods), args.namespace, selector, args.owner_uid)
        for uid in rm.new_pod_candidates(baseline_uids, selected, target):
            if uid not in new_uids:
                new_uids.append(uid)
        if len(new_uids) > 1:
            raise rm.AmbiguousMatchError(
                f"{len(new_uids)} distinct new pods match the selector ({', '.join(sorted(new_uids)[:5])}); refusing to guess"
            )
        if new_uids:
            if found_uid is None:
                found_uid = new_uids[0]
                deadline = now + args.timeout_ready
                say(f"replacement pod observed (uid {found_uid}); waiting for Ready")
            view = next((v for v in selected if v.uid == found_uid), None)
            if view is None:
                raise rm.ReplacementDisappearedError("replacement pod vanished before Ready=True was observed")
            if view.is_ready:
                return
        remaining = deadline - clock()
        if remaining <= 0:
            what = "a replacement pod" if found_uid is None else "Ready=True"
            raise rm.PollTimeoutError(f"timed out waiting for {what}")
        sleep(min(args.poll_interval, remaining))
    raise rm.PollTimeoutError(f"gave up after {MAX_LIVE_POLLS} polls")


def _output_paths(out_dir: Path, sample_id: str) -> Dict[str, Path]:
    return {
        "observation": out_dir / f"{sample_id}_observation.json",
        "csv": out_dir / f"{sample_id}_recovery.csv",
        "json": out_dir / f"{sample_id}_recovery.json",
    }


def _write_new(path: Path, text: str) -> None:
    with open(path, "x", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _finalize(obs: Dict[str, Any], out_dir: Path, say: Callable[[str], None]) -> "tuple[int, List[Dict[str, Any]]]":
    """Write observation + rows (always, also for failed runs). Returns (0|EXIT_IO, rows)."""
    paths = _output_paths(out_dir, obs["sample_id"])
    rows: List[Dict[str, Any]] = []
    try:
        obs_text = json.dumps(obs, indent=2, allow_nan=False) + "\n"
        _write_new(paths["observation"], obs_text)
        ref = rm.evidence_ref_for(paths["observation"].name, obs_text.encode("utf-8"))
        rows = rm.analyze_observation(rm.load_json_strict(obs_text), evidence_ref=ref)
        _write_new(paths["csv"], rm.rows_to_csv(rows))
        _write_new(paths["json"], rm.rows_to_json(rows))
    except (OSError, ValueError, rm.RecoveryError) as exc:
        say(f"error: could not write evidence/results: {exc}")
        return rm.EXIT_IO, rows
    for row in rows:
        if row["role"] == "primary":
            shown = "n/a" if row["value"] is None else f"{row['value']:.3f} s"
            code = f" [{row['error_code']}]" if row["error_code"] else ""
            say(f"{row['measurement']}: {row['status']} {shown}{code}")
    say("disruption_to_first_restored_ros_message: NOT MEASURED by this tool (pod Ready is not ROS recovery)")
    say(f"wrote {paths['observation']}, {paths['csv']}, {paths['json']}")
    return 0, rows


def run(
    argv: Optional[Sequence[str]] = None,
    *,
    prog: str = TOOL,
    kubectl_factory: Optional[Callable[..., Kubectl]] = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    wall: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    out=None,
    err=None,
) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    factory = kubectl_factory or Kubectl

    def say(message: str) -> None:
        print(message, file=out)

    try:
        with contextlib.redirect_stderr(err):
            args = build_parser(prog).parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else rm.EXIT_USAGE

    obs: Optional[Dict[str, Any]] = None
    out_dir = args.out_dir
    status = 0
    try:
        validate_args(args)
        kubectl = factory(context=args.context, allow_delete=args.yes)
        if args.context is not None:
            say(f"kubectl context (explicit): {args.context}")
            context: Optional[str] = args.context
        else:
            context = kubectl.run(["config", "current-context"]).strip()
            rm.validate_context(context)
            say(f"kubectl context (current, not chosen by this tool): {context}")
        baseline_raw = list_pods(kubectl, args.namespace, args.selector)
        selector = rm.parse_selector(args.selector)
        baseline = rm.select_pods(rm.parse_pod_list(baseline_raw), args.namespace, selector, args.owner_uid)
        say(f"pods matching namespace={args.namespace} selector={args.selector}: "
            + (", ".join(v.name + (" (terminating)" if v.terminating else "") for v in baseline) or "none"))
        target = choose_target(baseline, args.target_pod)

        if not args.yes:
            say(f"DRY RUN: would delete pod {target.name} (uid {target.uid}) and measure its replacement.")
            say("Nothing was deleted. Re-run with --yes (and --context or --yes-context-is-correct) to do it.")
            return rm.EXIT_OK

        sample_id = args.sample_id or "rh-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        rm.validate_sample_id(sample_id)
        paths = _output_paths(out_dir, sample_id)
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise rm.GuardError(f"cannot create output directory: {exc.strerror}") from None
        existing = [str(p) for p in paths.values() if p.exists()]
        if existing:
            raise rm.GuardError(f"refusing to overwrite existing output: {existing[0]}")

        obs = _new_observation(args, sample_id, context)
        obs["baseline_pods"] = baseline_raw
        obs["target_uid"] = target.uid
        obs["target_pod"] = target.name
        try:
            collect(kubectl, obs, target, baseline, args, clock=clock, sleep=sleep, wall=wall, say=say)
            if args.with_events:
                replacement = rm.find_replacement(
                    baseline,
                    [rm.select_pods(rm.parse_pod_list(p["pods"]), args.namespace, selector, args.owner_uid)
                     for p in obs["polls"]],
                    target,
                )
                text = kubectl.run(["get", "events", "-n", args.namespace,
                                    "--field-selector", f"involvedObject.uid={replacement.uid}", "-o", "json"])
                obs["events"] = rm.sanitize_event_list(rm.load_json_strict(text), "kubectl get events output")
        except KeyboardInterrupt:
            status, obs["run_error"] = rm.EXIT_INTERRUPTED, {"code": "interrupted"}
        except rm.RecoveryError as exc:
            status, obs["run_error"] = exc.exit_code, {"code": exc.code}
            print(f"error [{exc.code}]: {exc}", file=err)
        except Exception as exc:  # cleanup guard: a bug must not lose the evidence of a real deletion
            status, obs["run_error"] = rm.EXIT_MEASUREMENT_FAILED, {"code": "internal_error"}
            print(f"error [internal_error]: {type(exc).__name__}", file=err)
        # Reached after ANY outcome of collect(): the pod may already be deleted, so always write evidence.
        obs["exit_status"] = status
        rc, rows = _finalize(obs, out_dir, say)
        if status == 0:
            status = rc if rc != 0 else (0 if rm.primaries_ok(rows) else rm.EXIT_MEASUREMENT_FAILED)
        return status
    except KeyboardInterrupt:
        return rm.EXIT_INTERRUPTED
    except rm.RecoveryError as exc:
        print(f"error [{exc.code}]: {exc}", file=err)
        return exc.exit_code


def main(argv: Optional[Sequence[str]] = None, *, prog: str = TOOL) -> int:
    return run(argv, prog=prog)


if __name__ == "__main__":
    sys.exit(main())
