"""Validator for per-run metric files, contract ``run-metrics/v1`` (stdlib only).

A run file holds ONE value (or an explicit non-value status) per registry
metric for ONE independent run, and names the manifest of that run. This
module validates the file, cross-checks it against its manifest, and loads a
whole directory of run files for the summary generator. Nothing is skipped
silently: every file, duplicate or inconsistency becomes a typed diagnostic.

CLI: ``python3 rm_runfile.py --base-dir fixtures fixtures/run-files/*.json``
exit codes 0 all valid, 1 at least one invalid, 2 usage or I/O error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import rm_contract as c  # noqa: E402
import rm_diag as d  # noqa: E402
import rm_metrics as m  # noqa: E402
import rm_validate as mv  # noqa: E402
from rm_diag import Collector, Result, ptr  # noqa: E402

TOP_REQUIRED = ("schema_version", "data_kind", "independent_run_id", "arm", "manifest_file", "manifest_sha256", "metrics")
ENTRY_REQUIRED = ("status", "value", "unit")
ENTRY_OPTIONAL = ("reason", "censored_lower_bound", "n_samples", "starved_ticks", "control_ticks", "missed_actions", "released_actions")

MISSING = {"unit": "UNIT_MISSING", "manifest_sha256": "DIGEST_MISSING"}


def _check_value(ctx: Collector, v: Any, path: str, metric: dict) -> Optional[Any]:
    dom = metric["value_domain"]
    if dom == "boolean":
        return d.get_bool(ctx, v, path, "value")
    if dom == "non_negative_int":
        return d.get_int(ctx, v, path, 0, c.MAX_COUNT, "value")
    if dom == "fraction_0_1":
        x = d.get_number(ctx, v, path, "value")
        if x is not None and x > 1.0:
            ctx.add("VALUE_OUT_OF_RANGE", path, f"fraction {d.show(v)} is above 1")
            return None
        return x
    return d.get_number(ctx, v, path, "value")


def _check_entry(ctx: Collector, e: Any, path: str, metric: dict) -> Optional[str]:
    o = d.get_obj(ctx, e, path, ENTRY_REQUIRED, ENTRY_OPTIONAL, MISSING)
    if o is None:
        return None
    mid = metric["id"]
    if "unit" in o:
        u = o["unit"]
        if not isinstance(u, str):
            ctx.add("WRONG_TYPE", ptr(path, "unit"), f"unit must be a string, got {d.type_name(u)}")
        elif u != metric["unit"]:
            ctx.add("METRIC_UNIT_MISMATCH", ptr(path, "unit"), f"unit {d.show(u)}; the registry defines {metric['unit']!r} for {mid}")
    status = d.get_enum(ctx, o["status"], ptr(path, "status"), m.STATUSES, "status") if "status" in o else None
    if status == "censored" and not metric["censorable"]:
        ctx.add("STATUS_INVALID", ptr(path, "status"), f"{mid} is not censorable")
        status = None
    allowed_extra = {
        "ok": {"value", "unit"} | ({"n_samples"} if mid in m.SAMPLE_COUNT_METRICS else set()) | set(m.EXTRA_FIELDS.get(mid, ())),
        "censored": {"censored_lower_bound"},
        "not_computable": {"reason"},
        "not_applicable": set(),
    }
    if status is not None:
        for k in ENTRY_OPTIONAL:
            if k in o and k not in allowed_extra[status]:
                ctx.add("FIELD_NOT_ALLOWED", ptr(path, k), f"{k!r} is not allowed with status {status}")
        for k in sorted(allowed_extra[status] - {"value", "unit"}):
            if k not in o:
                ctx.add("MISSING_FIELD", ptr(path, k), f"status {status} needs {k!r}")
    value = None
    if "value" in o:
        if status in ("censored", "not_computable", "not_applicable"):
            if o["value"] is not None:
                ctx.add("VALUE_STATUS_MISMATCH", ptr(path, "value"), f"status {status} needs value=null")
        elif status == "ok":
            if o["value"] is None:
                ctx.add("VALUE_STATUS_MISMATCH", ptr(path, "value"), "status ok needs a value")
            else:
                value = _check_value(ctx, o["value"], ptr(path, "value"), metric)
    if status == "censored" and "censored_lower_bound" in o:
        d.get_number(ctx, o["censored_lower_bound"], ptr(path, "censored_lower_bound"), "censored_lower_bound")
    if status == "not_computable" and "reason" in o:
        d.get_enum(ctx, o["reason"], ptr(path, "reason"), m.NOT_COMPUTABLE_REASONS, "reason")
    if status == "ok":
        if "n_samples" in o:
            d.get_int(ctx, o["n_samples"], ptr(path, "n_samples"), 1, c.MAX_COUNT, "n_samples")
        extra = m.EXTRA_FIELDS.get(mid)
        if extra:
            num_k, den_k = extra
            num = d.get_int(ctx, o[num_k], ptr(path, num_k), 0, c.MAX_COUNT, num_k) if num_k in o else None
            den = d.get_int(ctx, o[den_k], ptr(path, den_k), 1, c.MAX_COUNT, den_k) if den_k in o else None
            if num is not None and den is not None:
                if num > den:
                    ctx.add("RATIO_INCONSISTENT", ptr(path, num_k), f"{num_k} {num} exceeds {den_k} {den}")
                elif value is not None and abs(float(value) - num / den) > 1e-6:
                    ctx.add("RATIO_INCONSISTENT", ptr(path, "value"), f"value {d.show(value)} differs from {num_k}/{den_k} = {num / den:.6f}")
    return status


def _validate_runfile(doc: Any, ctx: Collector) -> None:
    if not isinstance(doc, dict):
        ctx.add("MALFORMED_DOCUMENT", "", f"top-level value is {d.type_name(doc)}, expected an object")
        return
    mv._scan(ctx, doc)
    if "schema_version" not in doc:
        ctx.add("MISSING_FIELD", "/schema_version", "schema_version is required")
        return
    ver = doc["schema_version"]
    if not isinstance(ver, str):
        ctx.add("WRONG_TYPE", "/schema_version", f"schema_version must be a string, got {d.type_name(ver)}")
        return
    if ver != c.RUNFILE_VERSION:
        ctx.add("RUNFILE_VERSION_UNKNOWN", "/schema_version", f"unknown schema_version {d.show(ver)}; supported: {c.RUNFILE_VERSION!r}")
        return
    d.get_obj(ctx, doc, "", TOP_REQUIRED, (), MISSING)
    if "data_kind" in doc:
        d.get_enum(ctx, doc["data_kind"], "/data_kind", c.DATA_KINDS, "data_kind")
    if "independent_run_id" in doc:
        d.get_id(ctx, doc["independent_run_id"], "/independent_run_id", "independent_run_id", placeholder_check=True)
    if "arm" in doc:
        d.get_id(ctx, doc["arm"], "/arm", "arm")
    if "manifest_file" in doc:
        s = d.get_str(ctx, doc["manifest_file"], "/manifest_file", c.PATH_RE, "PATH_UNSAFE", "manifest_file")
        if s is not None and any(seg in (".", "..") for seg in s.split("/")):
            ctx.add("PATH_UNSAFE", "/manifest_file", "path contains '.' or '..' segments")
    if "manifest_sha256" in doc:
        d.get_hex(ctx, doc["manifest_sha256"], "/manifest_sha256", c.SHA256_RE, "manifest_sha256")
    if "metrics" in doc:
        mets = d.get_obj(ctx, doc["metrics"], "/metrics", (), m.METRIC_IDS, {}, unknown_code="UNKNOWN_METRIC")
        if mets is not None:
            for mid in m.METRIC_IDS:
                if mid not in mets:
                    ctx.add("METRIC_MISSING", ptr("/metrics", mid), f"metric {mid!r} has no entry (use not_computable/not_applicable, never omit)")
                else:
                    _check_entry(ctx, mets[mid], ptr("/metrics", mid), m.BY_ID[mid])


def validate_runfile_document(doc: Any) -> Result:
    """Validate one parsed run file in isolation. Never raises."""
    ctx = Collector()
    try:
        _validate_runfile(doc, ctx)
    except Exception as exc:
        ctx.add("INTERNAL_ERROR", "", f"unexpected {type(exc).__name__}")
    return ctx.result()


def _entry_status(run_doc: dict, mid: str) -> Optional[str]:
    mets = run_doc.get("metrics")
    e = mets.get(mid) if isinstance(mets, dict) else None
    st = e.get("status") if isinstance(e, dict) else None
    return st if isinstance(st, str) else None


def check_against_manifest(run_doc: Any, man: Any, manifest_sha256: Optional[str]) -> Result:
    """Cross-check an already-valid run file against its already-valid manifest."""
    ctx = Collector()
    try:
        _cross(run_doc, man, manifest_sha256, ctx)
    except Exception as exc:
        ctx.add("INTERNAL_ERROR", "", f"unexpected {type(exc).__name__}")
    return ctx.result()


def _cross(run_doc: dict, man: dict, sha: Optional[str], ctx: Collector) -> None:
    def mismatch(path: str, msg: str, code: str = "RUNFILE_MANIFEST_MISMATCH") -> None:
        ctx.add(code, path, msg)

    if run_doc.get("manifest_sha256") != sha:
        mismatch("/manifest_sha256", "sha256 differs from the manifest file's actual bytes")
    if run_doc.get("independent_run_id") != man["run"]["independent_run_id"]:
        mismatch("/independent_run_id", "differs from the manifest's run.independent_run_id")
    if run_doc.get("arm") != man["arm"]:
        mismatch("/arm", "differs from the manifest's arm")
    if run_doc.get("data_kind") != man["data_kind"]:
        mismatch("/data_kind", "differs from the manifest's data_kind")
    domain = man["clocks"]["clock_domain"]
    for mid in m.METRIC_IDS:
        entry = run_doc["metrics"][mid]
        st = entry["status"]
        scope = m.BY_ID[mid]["clock_scope"]
        if scope == "same_clock_domain" and domain == "unsynchronized":
            if st in ("ok", "censored"):
                mismatch(f"/metrics/{mid}/status", f"clocks are unsynchronized; {mid} needs one clock domain and must be not_computable", "CLOCK_REQUIREMENT_VIOLATED")
        if st == "not_computable" and entry.get("reason") == "clock_unsynchronized" and domain != "unsynchronized":
            mismatch(f"/metrics/{mid}/reason", "reason clock_unsynchronized, but the manifest declares " + domain)
    applied = man["fault_injection"]["applied"]
    rec = _entry_status(run_doc, "recovery_time_ms")
    if applied is False and rec != "not_applicable":
        mismatch("/metrics/recovery_time_ms/status", "no fault was injected; recovery_time_ms must be not_applicable")
    if applied is True and rec == "not_applicable":
        mismatch("/metrics/recovery_time_ms/status", "a fault was injected; recovery_time_ms cannot be not_applicable")
    app = man["outcome"]["task_success"]["applicable"]
    ts = _entry_status(run_doc, "task_success")
    if app is False and ts != "not_applicable":
        mismatch("/metrics/task_success/status", "manifest says task success is not applicable")
    if app is True and ts == "not_applicable":
        mismatch("/metrics/task_success/status", "manifest says task success is applicable")
    fc = run_doc["metrics"]["failure_count"]
    if fc.get("status") == "ok" and fc.get("value") != man["outcome"]["failure_count"]:
        mismatch("/metrics/failure_count/value", "differs from the manifest's outcome.failure_count")
    if fc.get("status") != "ok":
        mismatch("/metrics/failure_count/status", "failure_count must always be ok (a count of 0 is a value)")


# --------------------------------------------------------------------------- set loading


@dataclass(frozen=True)
class Run:
    file: str
    run_id: str
    arm: str
    fresh_deployment: bool
    metrics: dict
    manifest: dict
    manifest_sha256: str
    file_sha256: str


def load_runfile_set(run_dir: str, base_dir: str, require_synthetic: bool = True) -> tuple:
    """Load every ``*.json`` in ``run_dir``. Returns ``(runs, problems)``.

    ``problems`` is a list of ``(file, Diagnostic)``. If it is non-empty the
    caller must not summarise: nothing is skipped silently. Never raises.
    """
    runs: list = []
    problems: list = []
    try:
        names = sorted(n for n in os.listdir(run_dir))
    except OSError as exc:
        return [], [("(directory)", d.Diagnostic("FILE_UNREADABLE", "", f"{type(exc).__name__}", c.hint_for("FILE_UNREADABLE")))]
    json_names = [n for n in names if n.endswith(".json")]
    other = [n for n in names if not n.endswith(".json")]
    for n in other:
        problems.append((n, d.Diagnostic("UNKNOWN_FIELD", "", "unexpected non-JSON file in the run-file directory", "Remove it; nothing is skipped silently.")))
    if not json_names:
        problems.append(("(directory)", d.Diagnostic("MISSING_FIELD", "", "no run files found", "Add run files.")))
    seen_ids: dict = {}
    for n in json_names:
        path = os.path.join(run_dir, n)
        doc, diags = d.read_json_file(path)
        if diags:
            problems += [(n, x) for x in diags]
            continue
        res = validate_runfile_document(doc)
        if not res.valid:
            problems += [(n, x) for x in res.errors]
            continue
        if require_synthetic and doc["data_kind"] != "synthetic":
            problems.append((n, d.Diagnostic("NOT_SYNTHETIC", "/data_kind", f"data_kind is {doc['data_kind']!r}", c.hint_for("NOT_SYNTHETIC"))))
            continue
        mpath = os.path.join(base_dir, *doc["manifest_file"].split("/"))
        mdoc, mdiags = d.read_json_file(mpath)
        if mdiags:
            problems += [(n, x) for x in mdiags]
            continue
        mres = mv.validate_document(mdoc)
        if not mres.valid:
            problems += [(n + " -> " + doc["manifest_file"], x) for x in mres.errors]
            continue
        sha = mv.sha256_file(mpath)
        cres = check_against_manifest(doc, mdoc, sha)
        if not cres.valid:
            problems += [(n, x) for x in cres.errors]
            continue
        rid = doc["independent_run_id"]
        if rid in seen_ids:
            problems.append((n, d.Diagnostic("DUPLICATE_RUN_ID", "/independent_run_id", f"{d.show(rid)} already used by {seen_ids[rid]}", c.hint_for("DUPLICATE_RUN_ID"))))
            continue
        seen_ids[rid] = n
        runs.append(Run(n, rid, doc["arm"], mdoc["run"]["fresh_deployment"], doc["metrics"], mdoc, sha, mv.sha256_file(path) or ""))
    return runs, problems


# --------------------------------------------------------------------------- CLI


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="rm_runfile.py",
        description="Validate run-metrics/v1 files, optionally against their manifests. Offline, stdlib only.",
        epilog="Exit codes: 0 all valid, 1 at least one invalid, 2 usage or I/O error.",
    )
    ap.add_argument("files", nargs="+", help="run-file JSON file(s)")
    ap.add_argument("--base-dir", help="directory that manifest_file paths are relative to; enables the cross-check against the manifest")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code in (0, None) else 2
    results = []
    for path in args.files:
        doc, diags = d.read_json_file(path)
        if diags:
            res = Result(tuple(diags))
        else:
            res = validate_runfile_document(doc)
            if res.valid and args.base_dir is not None:
                mpath = os.path.join(args.base_dir, *doc["manifest_file"].split("/"))
                mdoc, mdiags = d.read_json_file(mpath)
                if mdiags:
                    res = Result(tuple(mdiags))
                else:
                    mres = mv.validate_document(mdoc)
                    res = mres if not mres.valid else check_against_manifest(doc, mdoc, mv.sha256_file(mpath))
        results.append((path, res))
    report = mv.build_report(results)
    if args.format == "json":
        print(json.dumps(report, indent=2, ensure_ascii=True))
    else:
        print(mv.format_text(report))
    if any(x.code == "FILE_UNREADABLE" for _, r in results for x in r.diagnostics):
        return 2
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
