"""Deterministic generator for every committed SYNTHETIC artifact of this directory (stdlib only).

    python3 examples/run-manifest/fixture_gen.py            # (re)write generated files
    python3 examples/run-manifest/fixture_gen.py --check    # exit 1 if anything drifted

Generated: ``schema/*.json`` (from rm_schema.py), ``METRICS.md`` and
``schema/metrics-registry.v1.json`` (from rm_metrics.py), ``fixtures/manifests/valid``,
``fixtures/manifests/invalid`` + ``expected_diagnostics.json``, ``fixtures/run-files``
and ``fixtures/synthetic-workload.v1.json``. ``reports/`` is produced by ``rm_summary.py``.

Everything is invented. Values come from ``random.Random(seed)`` with arbitrary
toy bases; hashes are sha1/sha256 of the literal text "SYNTHETIC:<label>" (so
they are well-formed but identify no real commit, image or file). No real
robot, address, person, image payload or measurement is involved.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import random
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import rm_contract as c  # noqa: E402
import rm_metrics as m  # noqa: E402
import rm_schema as sch  # noqa: E402

WORKLOAD_REL = "fixtures/synthetic-workload.v1.json"
MANIFEST_DIR = "fixtures/manifests"
RUN_DIR = "fixtures/run-files"
MANAGED_DIRS = (MANIFEST_DIR + "/valid", MANIFEST_DIR + "/invalid", RUN_DIR, "schema")

JOINTS = (("shoulder_pan", "rad"), ("shoulder_lift", "rad"), ("elbow_flex", "rad"), ("wrist_flex", "rad"), ("wrist_roll", "rad"), ("gripper", "m"))
WARMUP_S, TIMEOUT_S, COMPLETED_S, RATE_HZ = 10, 60, 55, 10
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)  # invented run date

# Arbitrary toy bases (ms or fraction or count). They resemble nothing measured.
ARMS = (
    {
        "name": "local-baseline", "n": 4, "clock": "single_clock", "shaped": False, "fault": False, "task": False,
        "base": dict(s2p=12.0, rsr=30.0, vd=1.5, aa=60.0, starv=0.01, miss=0.02, gap=150.0, gapn=1),
    },
    {
        "name": "edge-cloud-lossy", "n": 4, "clock": "synchronized", "shaped": True, "fault": True, "task": True,
        "base": dict(s2p=18.0, rsr=95.0, vd=1.6, aa=150.0, starv=0.06, miss=0.15, gap=900.0, gapn=6),
    },
    {
        "name": "edge-cloud-single-run", "n": 1, "clock": "unsynchronized", "shaped": True, "fault": False, "task": False,
        "base": dict(s2p=20.0, rsr=90.0, vd=1.5, aa=140.0, starv=0.05, miss=0.12, gap=800.0, gapn=5),
    },
)


def dumps(doc) -> str:
    return json.dumps(doc, indent=2, ensure_ascii=True) + "\n"


def shex(label: str, algo: str = "sha256") -> str:
    return hashlib.new(algo, ("SYNTHETIC:" + label).encode("ascii")).hexdigest()


def utc(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def q(value, unit):
    return {"value": value, "unit": unit}


def run_id(arm: str, idx: int) -> str:
    return f"syn-{arm}-r{idx:02d}"


def workload_text() -> str:
    return dumps({
        "synthetic": True,
        "note": "SYNTHETIC workload description. It exists only so that data_hashes[] points at a real file that can be re-hashed.",
        "control_rate_hz": RATE_HZ,
        "warmup_s": WARMUP_S,
        "timeout_s": TIMEOUT_S,
    })


def _status(arm: dict, idx: int) -> str:
    return "timed_out" if (arm["fault"] and idx == arm["n"]) else "completed"


def _failures(arm: dict, idx: int, rng: random.Random) -> int:
    if not arm["fault"]:
        return 0
    return 2 if _status(arm, idx) == "timed_out" else rng.randint(0, 2)


def make_manifest(ai: int, idx: int, workload_sha: str) -> dict:
    arm = ARMS[ai]
    status = _status(arm, idx)
    timed_out = status == "timed_out"
    observed = TIMEOUT_S if timed_out else COMPLETED_S
    start = T0 + timedelta(minutes=60 * ai + 10 * idx)
    rng = random.Random(1000 * ai + idx + 7)  # separate stream from the metric values
    failures = _failures(arm, idx, rng)
    local = arm["name"] == "local-baseline"
    edge, cloud = ("node-a", "local") if local else ("edge-node", "edge"), ("node-a", "local") if local else ("cloud-node", "cloud")
    placement = [
        {"component": "sensor-source", "node": edge[0], "tier": edge[1]},
        {"component": "policy-client", "node": edge[0], "tier": edge[1]},
        {"component": "validator", "node": edge[0], "tier": edge[1]},
        {"component": "policy-server", "node": cloud[0], "tier": cloud[1]},
    ]
    clocks = {"clock_basis": "monotonic", "clock_domain": arm["clock"], "simulation_time": False}
    if arm["clock"] == "synchronized":
        clocks.update(sync_method="ptp", estimated_max_offset=q(1, "ms"))
    if arm["shaped"]:
        network = {
            "profile": "synthetic-lossy-wan", "shaped": True, "shaping_tool": "synthetic-shaper",
            "one_way_delay": q(40, "ms"), "jitter": q(5, "ms"), "loss": q(2, "percent"), "bandwidth": q(50, "Mbit/s"),
        }
    else:
        network = {"profile": "synthetic-loopback", "shaped": False}
    fault = {"applied": True, "kind": "network-partition-step", "duration": q(5, "s")} if arm["fault"] else {"applied": False}
    if arm["task"]:
        task = {"applicable": True, "value": (not timed_out) and idx != 3}
    else:
        task = {"applicable": False, "value": None}
    return {
        "schema_version": c.SCHEMA_VERSION,
        "data_kind": "synthetic",
        "experiment_id": "SYNTHETIC-EXAMPLE",
        "arm": arm["name"],
        "description": "SYNTHETIC fixture run with invented values; not a measurement",
        "run": {
            "independent_run_id": run_id(arm["name"], idx),
            "group_id": "syn-group-0001",
            "run_index": idx,
            "fresh_deployment": True,
            "seed": 1000 * ai + idx,
            "started_utc": utc(start),
            "ended_utc": utc(start + timedelta(seconds=observed)),
            "warmup": q(WARMUP_S, "s"),
            "timeout": q(TIMEOUT_S, "s"),
        },
        "source": {"git_sha": shex("source-commit", "sha1"), "git_dirty": False},
        "images": [
            {"name": "synthetic-policy-server", "oci_digest": "sha256:" + shex("image:policy-server")},
            {"name": "synthetic-validator", "oci_digest": "sha256:" + shex("image:validator")},
        ],
        "software": {
            "ros_distro": "synthetic-distro", "rmw_implementation": "synthetic-rmw", "rmw_version": "0.0.0",
            "cni": {"name": "synthetic-cni", "version": "0.0.0"}, "kubernetes_version": "0.0.0",
        },
        "simulator": {"used": False},
        "hardware": {
            "cpu": {"model": "SYNTHETIC-CPU", "logical_cores": 8, "architecture": "x86_64"},
            "memory": q(16, "GiB"), "gpu": {"present": False}, "os": "synthetic-os 0.0", "kernel_version": "0.0.0-synthetic",
        },
        "placement": placement,
        "network": network,
        "qos": [
            {"topic": "/observation", "reliability": "best_effort", "durability": "volatile", "history": "keep_last", "depth": 5, "deadline": q(100, "ms")},
            {"topic": "/action", "reliability": "reliable", "durability": "volatile", "history": "keep_last", "depth": 10},
        ],
        "model": {"used": True, "name": "synthetic-mock-policy", "revision": shex("model-commit", "sha1"), "license": "CC0-1.0"},
        "dataset": {"used": False},
        "code_license": "Apache-2.0",
        "clocks": clocks,
        "timing": {"control_rate": q(RATE_HZ, "Hz"), "action_age_deadline": q(200, "ms")},
        "action_space": {
            "name": "synthetic-6dof-joint-targets", "dimension": 6,
            "names": [j[0] for j in JOINTS], "units": [j[1] for j in JOINTS], "actuation": "none",
        },
        "fault_injection": fault,
        "measurement_source": {"kind": "synthetic_generator", "tool": "fixture_gen.py", "tool_version": "1"},
        "outcome": {
            "status": status,
            "censored": timed_out,
            "censor_reason": "run timeout reached before the first valid action after the fault" if timed_out else None,
            "observed_duration": q(observed, "s"),
            "failure_count": failures,
            "task_success": task,
        },
        "data_hashes": [{"path": WORKLOAD_REL, "role": "input", "sha256": workload_sha}],
    }


def make_variant(workload_sha: str) -> dict:
    """A second valid manifest that exercises the optional branches (GPU, dataset, simulator, keep_all)."""
    doc = make_manifest(1, 1, workload_sha)
    doc["arm"] = "variant"
    doc["run"]["independent_run_id"] = "syn-variant-r01"
    doc["hardware"]["gpu"] = {"present": True, "model": "SYNTHETIC-GPU", "count": 1, "driver_version": "0.0.0-synthetic"}
    doc["dataset"] = {"used": True, "name": "synthetic-dataset", "revision": shex("dataset-commit", "sha1"), "license": "CC0-1.0", "content_sha256": shex("dataset-content")}
    doc["simulator"] = {"used": True, "name": "synthetic-sim", "version": "0.0.0"}
    doc["clocks"] = {"clock_basis": "wall", "clock_domain": "synchronized", "simulation_time": True, "sync_method": "chrony", "estimated_max_offset": q(250, "us")}
    doc["qos"][1].update(history="keep_all", durability="transient_local", lifespan=q(2, "s"))
    del doc["qos"][1]["depth"]
    doc["software"]["container_runtime"] = {"name": "synthetic-runtime", "version": "0.0.0"}
    return doc


def make_metrics(ai: int, idx: int, man: dict) -> dict:
    arm = ARMS[ai]
    b = arm["base"]
    rng = random.Random(1000 * ai + idx)
    jit = lambda v: round(v * (1 + rng.uniform(-0.2, 0.2)), 3)  # noqa: E731
    observed = man["outcome"]["observed_duration"]["value"]
    ticks = int((observed - WARMUP_S) * RATE_HZ)
    unsync = arm["clock"] == "unsynchronized"

    def lat(v):
        return {"status": "ok", "value": jit(v), "unit": "ms", "n_samples": ticks}

    def nc(unit):
        return {"status": "not_computable", "value": None, "unit": unit, "reason": "clock_unsynchronized"}

    s2p, rsr, vd, aa = lat(b["s2p"]), lat(b["rsr"]), lat(b["vd"]), lat(b["aa"])
    starved = int(jit(b["starv"]) * ticks)
    released = ticks - starved
    missed = int(jit(b["miss"]) * released)
    out = {
        "sensor_to_policy_request_ms": nc("ms") if unsync else s2p,
        "request_serve_response_ms": rsr,
        "validator_delay_ms": vd,
        "action_age_ms": nc("ms") if unsync else aa,
        "buffer_starvation_fraction": {"status": "ok", "value": round(starved / ticks, 6), "unit": "fraction", "starved_ticks": starved, "control_ticks": ticks},
        "deadline_miss_fraction": nc("fraction") if unsync else {"status": "ok", "value": round(missed / released, 6), "unit": "fraction", "missed_actions": missed, "released_actions": released},
        "message_gap_max_ms": {"status": "ok", "value": jit(b["gap"]), "unit": "ms", "n_samples": ticks},
        "message_gap_count": {"status": "ok", "value": max(0, b["gapn"] + rng.randint(-1, 1)), "unit": "count"},
    }
    if not arm["fault"]:
        out["recovery_time_ms"] = {"status": "not_applicable", "value": None, "unit": "ms"}
    elif man["outcome"]["status"] == "timed_out":
        out["recovery_time_ms"] = {"status": "censored", "value": None, "unit": "ms", "censored_lower_bound": 35000.0}
    else:
        out["recovery_time_ms"] = {"status": "ok", "value": round(rng.uniform(4000, 9000), 3), "unit": "ms"}
    out["failure_count"] = {"status": "ok", "value": man["outcome"]["failure_count"], "unit": "count"}
    ts = man["outcome"]["task_success"]
    out["task_success"] = {"status": "ok", "value": ts["value"], "unit": "boolean"} if ts["applicable"] else {"status": "not_applicable", "value": None, "unit": "boolean"}
    return {mid: out[mid] for mid in m.METRIC_IDS}


def make_runfile(man: dict, man_text: str, metrics: dict) -> dict:
    rid = man["run"]["independent_run_id"]
    return {
        "schema_version": c.RUNFILE_VERSION,
        "data_kind": "synthetic",
        "independent_run_id": rid,
        "arm": man["arm"],
        "manifest_file": f"manifests/valid/{rid}.json",
        "manifest_sha256": hashlib.sha256(man_text.encode("utf-8")).hexdigest(),
        "metrics": metrics,
    }


# --------------------------------------------------------------------------- invalid fixtures


def _del(path):
    def f(d):
        cur = d
        for k in path[:-1]:
            cur = cur[k]
        del cur[path[-1]]
    return f


def _set(path, value):
    def f(d):
        cur = d
        for k in path[:-1]:
            cur = cur[k]
        cur[path[-1]] = value
    return f


def _multi(*fs):
    def f(d):
        for g in fs:
            g(d)
    return f


def _swap_times(d):
    r = d["run"]
    r["started_utc"], r["ended_utc"] = r["ended_utc"], r["started_utc"]


# name -> (mutation, [(code, path), ...])
STRUCT_INVALID = {
    "unit_missing": (_del(("timing", "action_age_deadline", "unit")), [("UNIT_MISSING", "/timing/action_age_deadline/unit")]),
    "unit_wrong_dimension": (_set(("run", "timeout", "unit"), "Hz"), [("UNIT_NOT_ALLOWED", "/run/timeout/unit")]),
    "revision_missing_model": (_del(("model", "revision")), [("REVISION_MISSING", "/model/revision")]),
    "source_sha_missing": (_del(("source", "git_sha")), [("REVISION_MISSING", "/source/git_sha")]),
    "source_sha_malformed": (_set(("source", "git_sha"), shex("source-commit", "sha1")[:39]), [("HASH_MALFORMED", "/source/git_sha")]),
    "oci_digest_missing": (_del(("images", 0, "oci_digest")), [("DIGEST_MISSING", "/images/0/oci_digest")]),
    "oci_digest_placeholder": (_set(("images", 0, "oci_digest"), "sha256:" + "0" * 64), [("PLACEHOLDER_VALUE", "/images/0/oci_digest")]),
    "clocks_missing": (_del(("clocks",)), [("CLOCK_MISSING", "/clocks")]),
    "clock_sync_incomplete": (_del(("clocks", "estimated_max_offset")), [("CLOCK_SYNC_INCOMPLETE", "/clocks/estimated_max_offset")]),
    "timestamp_not_utc": (_set(("run", "started_utc"), "2026-01-01T01:10:00+01:00"), [("TIMESTAMP_NOT_UTC", "/run/started_utc")]),
    "timestamp_impossible_date": (_set(("run", "started_utc"), "2026-02-30T00:00:00Z"), [("TIMESTAMP_MALFORMED", "/run/started_utc")]),
    "timestamp_trailing_newline": (_set(("run", "started_utc"), "2026-01-01T01:10:00Z\n"), [("TIMESTAMP_MALFORMED", "/run/started_utc")]),
    "time_order": (_swap_times, [("TIME_ORDER", "/run/ended_utc")]),
    "run_id_malformed": (_set(("run", "independent_run_id"), "run 1"), [("ID_MALFORMED", "/run/independent_run_id")]),
    "schema_version_unknown": (_set(("schema_version",), "run-manifest/v2"), [("SCHEMA_VERSION_UNKNOWN", "/schema_version")]),
    "seed_bool": (_set(("run", "seed"), True), [("BOOL_AS_NUMBER", "/run/seed")]),
    "seed_list": (_set(("run", "seed"), [1, 2]), [("WRONG_TYPE", "/run/seed")]),
    "warmup_negative": (_set(("run", "warmup", "value"), -1), [("NEGATIVE_VALUE", "/run/warmup/value")]),
    "timeout_nan": (_set(("run", "timeout", "value"), float("nan")), [("NON_FINITE_VALUE", "/run/timeout/value")]),
    "censoring_inconsistent": (
        _multi(_set(("outcome", "status"), "timed_out"), _set(("outcome", "failure_count"), 1)),
        [("CENSORING_INCONSISTENT", "/outcome/censored")],
    ),
    "censor_reason_missing": (
        _multi(_set(("outcome", "status"), "timed_out"), _set(("outcome", "censored"), True), _set(("outcome", "failure_count"), 1)),
        [("CENSOR_REASON_MISSING", "/outcome/censor_reason")],
    ),
    "task_success_inconsistent": (_set(("outcome", "task_success"), {"applicable": False, "value": True}), [("TASK_SUCCESS_INCONSISTENT", "/outcome/task_success/value")]),
    "measurement_kind_mismatch": (_set(("measurement_source", "kind"), "ros2_tracing"), [("MEASUREMENT_KIND_MISMATCH", "/measurement_source/kind")]),
    "actuation_enabled": (_set(("action_space", "actuation"), "enabled"), [("ACTUATION_NOT_NONE", "/action_space/actuation")]),
    "address_in_value": (_set(("placement", 0, "node"), "192.0.2.10"), [("ADDRESS_IN_VALUE", "/placement/0/node")]),
    "secret_like_key": (_set(("x_password",), "not-a-real-secret"), [("SECRET_LIKE_KEY", "/x_password")]),
}


def make_invalid(base_text: str, base: dict) -> dict:
    """``{name: (text, [(code, path)])}``."""
    out = {}
    for name, (fn, expected) in STRUCT_INVALID.items():
        doc = copy.deepcopy(base)
        fn(doc)
        out[name] = (dumps(doc), expected)
    seed_line = f'"seed": {base["run"]["seed"]},'
    assert base_text.count(seed_line) == 1
    out["duplicate_key"] = (base_text.replace(seed_line, seed_line + "\n    " + '"seed": 7,'), [("DUPLICATE_KEY", "/run/seed")])
    out["malformed_json"] = (base_text[: len(base_text) // 2], [("MALFORMED_JSON", "")])
    out["top_level_array"] = ("[]\n", [("MALFORMED_DOCUMENT", "")])
    return out


# --------------------------------------------------------------------------- everything


def generate() -> dict:
    """``{relative path: text}`` for every generated file."""
    files: dict = {}
    wl = workload_text()
    files[WORKLOAD_REL] = wl
    wl_sha = hashlib.sha256(wl.encode("utf-8")).hexdigest()
    first_lossy = None
    for ai, arm in enumerate(ARMS):
        for idx in range(1, arm["n"] + 1):
            man = make_manifest(ai, idx, wl_sha)
            text = dumps(man)
            rid = man["run"]["independent_run_id"]
            files[f"{MANIFEST_DIR}/valid/{rid}.json"] = text
            files[f"{RUN_DIR}/{rid}.json"] = dumps(make_runfile(man, text, make_metrics(ai, idx, man)))
            if ai == 1 and idx == 1:
                first_lossy = (man, text)
    files[f"{MANIFEST_DIR}/valid/syn-variant-r01.json"] = dumps(make_variant(wl_sha))
    inv = make_invalid(first_lossy[1], first_lossy[0])
    expected = {}
    for name, (text, exp) in sorted(inv.items()):
        files[f"{MANIFEST_DIR}/invalid/{name}.json"] = text
        expected[f"invalid/{name}.json"] = [{"code": cd, "path": p} for cd, p in exp]
    files[f"{MANIFEST_DIR}/expected_diagnostics.json"] = dumps(expected)
    files["schema/run-manifest.v1.schema.json"] = dumps(sch.manifest_schema())
    files["schema/run-metrics.v1.schema.json"] = dumps(sch.runfile_schema())
    files["schema/metrics-registry.v1.json"] = dumps(m.registry())
    files["METRICS.md"] = m.render_markdown()
    return files


def drift(files: dict) -> list:
    msgs = []
    for rel, text in sorted(files.items()):
        try:
            with open(os.path.join(HERE, *rel.split("/")), "rb") as fh:
                if fh.read() != text.encode("utf-8"):
                    msgs.append(f"{rel}: differs from regenerated content")
        except OSError:
            msgs.append(f"{rel}: missing")
    wanted = set(files)
    for d in MANAGED_DIRS:
        try:
            for n in sorted(os.listdir(os.path.join(HERE, *d.split("/")))):
                rel = f"{d}/{n}"
                if rel not in wanted:
                    msgs.append(f"{rel}: stray file (not generated)")
        except OSError:
            pass
    return msgs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="fixture_gen.py", description="Regenerate or check the SYNTHETIC fixtures, schemas and metric docs.")
    ap.add_argument("--check", action="store_true", help="exit 1 if the committed files differ from a regeneration (writes nothing)")
    args = ap.parse_args(argv)
    files = generate()
    if args.check:
        msgs = drift(files)
        for line in msgs:
            print("DRIFT " + line, file=sys.stderr)
        if msgs:
            print(f"{len(msgs)} file(s) drifted; run without --check to regenerate", file=sys.stderr)
            return 1
        print(f"OK {len(files)} generated files match")
        return 0
    stray = [x.split(": ")[0] for x in drift(files) if x.endswith("stray file (not generated)")]
    for rel, text in files.items():
        path = os.path.join(HERE, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(text.encode("utf-8"))
    for rel in stray:
        os.remove(os.path.join(HERE, *rel.split("/")))
        print(f"removed stray {rel}")
    print(f"wrote {len(files)} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
