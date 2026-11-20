"""Validator and CLI for the run manifest, contract ``run-manifest/v1``.

Python standard library only. Offline. Fail closed: every problem becomes a
``Diagnostic`` (stable ``code``, JSON-pointer ``path``, ``message``, ``hint``).
``validate_document`` / ``validate_text`` / ``validate_file`` never raise, keep
checking after the first problem, and turn an unexpected internal error into
``INTERNAL_ERROR`` (the document is then invalid).

This checks the *shape and internal consistency* of provenance metadata. It
does not prove that the recorded versions, digests or clocks are true, and it
does not run, pull or contact anything.

CLI exit codes: 0 all files valid, 1 at least one invalid, 2 usage or I/O error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import rm_contract as c  # noqa: E402
import rm_diag as d  # noqa: E402
from rm_diag import Collector, Result, ptr  # noqa: E402

TOP_REQUIRED = (
    "schema_version", "data_kind", "experiment_id", "arm", "run", "source", "images", "software",
    "simulator", "hardware", "placement", "network", "qos", "model", "dataset", "code_license",
    "clocks", "timing", "action_space", "fault_injection", "measurement_source", "outcome", "data_hashes",
)
TOP_OPTIONAL = ("description",)

# A missing key of this name gets a specific typed code instead of MISSING_FIELD.
MISSING_CODES = {
    "unit": "UNIT_MISSING",
    "units": "UNIT_MISSING",
    "git_sha": "REVISION_MISSING",
    "revision": "REVISION_MISSING",
    "source": "REVISION_MISSING",
    "oci_digest": "DIGEST_MISSING",
    "sha256": "DIGEST_MISSING",
    "clocks": "CLOCK_MISSING",
    "clock_basis": "CLOCK_MISSING",
    "clock_domain": "CLOCK_MISSING",
    "license": "LICENSE_MISSING",
    "code_license": "LICENSE_MISSING",
}


def _obj(ctx, v, path, required=(), optional=()):
    return d.get_obj(ctx, v, path, required, optional, MISSING_CODES)


def _list(ctx: Collector, v: Any, path: str, what: str, min_len: int = 1, empty_code: str = "MISSING_FIELD") -> Optional[list]:
    if not isinstance(v, list):
        ctx.add("WRONG_TYPE", path, f"{what} must be an array, got {d.type_name(v)}")
        return None
    if len(v) < min_len:
        ctx.add(empty_code, path, f"{what} must have at least {min_len} entr{'y' if min_len == 1 else 'ies'}")
        return None
    if len(v) > c.MAX_LIST:
        ctx.add("VALUE_OUT_OF_RANGE", path, f"{what} has more than {c.MAX_LIST} entries")
        return None
    return v


def _license(ctx: Collector, v: Any, path: str) -> Optional[str]:
    if v is None or (isinstance(v, str) and not v.strip()):
        ctx.add("LICENSE_MISSING", path, "licence is null or empty")
        return None
    s = d.get_str(ctx, v, path, what="licence")
    if s is None:
        return None
    if s.strip().lower() in c.UNDECLARED_LICENSES:  # whole-value match
        ctx.add("LICENSE_UNDECLARED", path, f"licence {d.show(s)} is a non-answer")
        return None
    if c.SPDX_RE.fullmatch(s) is None:
        ctx.add("LICENSE_MALFORMED", path, f"licence {d.show(s)} is not an SPDX-style identifier")
        return None
    return s


# --------------------------------------------------------------------------- structural scan


def _scan(ctx: Collector, root: Any) -> None:
    """One pass over the whole tree: duplicate keys, non-finite numbers, depth,
    secrets and addresses. Field-level checks stay silent about NaN/inf because
    this scan reports them exactly once."""
    stack = [(root, "", 0, "")]
    nodes = 0
    while stack:
        node, path, depth, key = stack.pop()
        nodes += 1
        if nodes > c.MAX_NODES:
            ctx.add("TOO_MANY_NODES", "", f"more than {c.MAX_NODES} JSON nodes")
            return
        if depth > c.MAX_DEPTH:
            ctx.add("NESTING_TOO_DEEP", path, f"nesting deeper than {c.MAX_DEPTH}")
            continue
        if isinstance(node, dict):
            for k in getattr(node, "dups", ()):
                ctx.add("DUPLICATE_KEY", ptr(path, k), f"key {d.show(k)} appears more than once in this object")
            for k, val in node.items():
                ks = k if isinstance(k, str) else str(k)
                if c.SECRET_KEY_RE.search(ks.lower()):
                    ctx.add("SECRET_LIKE_KEY", ptr(path, ks), f"key {d.show(ks)} looks like a credential field")
                stack.append((val, ptr(path, ks), depth + 1, ks))
        elif isinstance(node, list):
            for i, val in enumerate(node):
                stack.append((val, ptr(path, i), depth + 1, ""))
        elif isinstance(node, float):
            if node != node or node in (float("inf"), float("-inf")):
                ctx.add("NON_FINITE_VALUE", path, f"non-finite number {node} is not allowed")
        elif isinstance(node, str):
            _scan_string(ctx, node, path, key)


def _scan_string(ctx: Collector, s: str, path: str, key: str) -> None:
    for rx in c.SECRET_VALUE_RES:
        if rx.search(s):
            ctx.add("SECRET_LIKE_VALUE", path, "value matches a credential/token pattern")
            return
    if c.K8S_JOIN_TOKEN_RE.fullmatch(s):
        ctx.add("SECRET_LIKE_VALUE", path, "value has the shape of a Kubernetes bootstrap token")
        return
    if key.endswith("version"):
        return  # 1.2.3.4-style versions are legitimate
    for m in c.IPV4_RE.finditer(s):
        if all(int(g) <= 255 for g in m.groups()):
            ctx.add("ADDRESS_IN_VALUE", path, "value contains an IPv4 address")
            return


# --------------------------------------------------------------------------- sections


def _check_run(ctx: Collector, v: Any, path: str) -> dict:
    out: dict = {}
    req = ("independent_run_id", "group_id", "run_index", "fresh_deployment", "seed",
           "started_utc", "ended_utc", "warmup", "timeout")
    o = _obj(ctx, v, path, req)
    if o is None:
        return out
    if "independent_run_id" in o:
        d.get_id(ctx, o["independent_run_id"], ptr(path, "independent_run_id"), "independent_run_id", placeholder_check=True)
    if "group_id" in o:
        d.get_id(ctx, o["group_id"], ptr(path, "group_id"), "group_id", placeholder_check=True)
    if "run_index" in o:
        d.get_int(ctx, o["run_index"], ptr(path, "run_index"), 0, 10**6, "run_index")
    if "fresh_deployment" in o:
        d.get_bool(ctx, o["fresh_deployment"], ptr(path, "fresh_deployment"), "fresh_deployment")
    if "seed" in o:
        d.get_int(ctx, o["seed"], ptr(path, "seed"), 0, c.MAX_SEED, "seed")
    start = end = None
    if "started_utc" in o:
        start = d.get_utc(ctx, o["started_utc"], ptr(path, "started_utc"), "started_utc")
    if "ended_utc" in o:
        end = d.get_utc(ctx, o["ended_utc"], ptr(path, "ended_utc"), "ended_utc")
    if start is not None and end is not None and end < start:  # equal is allowed; end < start is not
        ctx.add("TIME_ORDER", ptr(path, "ended_utc"), "ended_utc is earlier than started_utc")
    warm = tmo = None
    if "warmup" in o:
        warm = d.get_quantity(ctx, o["warmup"], ptr(path, "warmup"), "time", what="warmup")
    if "timeout" in o:
        tmo = d.get_quantity(ctx, o["timeout"], ptr(path, "timeout"), "time", positive=True, what="timeout")
    if warm is not None and tmo is not None and warm >= tmo:
        ctx.add("WARMUP_EXCEEDS_TIMEOUT", ptr(path, "warmup"), "warmup is not shorter than timeout")
    out["timeout_s"] = tmo
    return out


def _check_source(ctx: Collector, v: Any, path: str) -> Optional[bool]:
    o = _obj(ctx, v, path, ("git_sha", "git_dirty"))
    if o is None:
        return None
    if "git_sha" in o:
        d.get_hex(ctx, o["git_sha"], ptr(path, "git_sha"), c.GIT_SHA_RE, "git_sha (40 lowercase hex)")
    if "git_dirty" in o:
        return d.get_bool(ctx, o["git_dirty"], ptr(path, "git_dirty"), "git_dirty")
    return None


def _check_images(ctx: Collector, v: Any, path: str) -> None:
    items = _list(ctx, v, path, "images", 1, "DIGEST_MISSING")
    if items is None:
        return
    seen: set = set()
    for i, img in enumerate(items):
        p = ptr(path, i)
        o = _obj(ctx, img, p, ("name", "oci_digest"))
        if o is None:
            continue
        if "name" in o:
            n = d.get_id(ctx, o["name"], ptr(p, "name"), "image name")
            if n is not None:
                if n in seen:
                    ctx.add("DUPLICATE_ENTRY", ptr(p, "name"), f"image name {d.show(n)} listed twice")
                seen.add(n)
        if "oci_digest" in o:
            d.get_hex(ctx, o["oci_digest"], ptr(p, "oci_digest"), c.OCI_DIGEST_RE, "oci_digest ('sha256:' + 64 lowercase hex)", prefix="sha256:")


def _name_version(ctx: Collector, v: Any, path: str, what: str, version_pattern=None) -> None:
    o = _obj(ctx, v, path, ("name", "version"))
    if o is None:
        return
    if "name" in o:
        d.get_id(ctx, o["name"], ptr(path, "name"), f"{what} name")
    if "version" in o:
        d.get_version(ctx, o["version"], ptr(path, "version"), f"{what} version", version_pattern)


def _check_software(ctx: Collector, v: Any, path: str) -> None:
    o = _obj(ctx, v, path, ("ros_distro", "rmw_implementation", "rmw_version", "cni", "kubernetes_version"), ("container_runtime",))
    if o is None:
        return
    if "ros_distro" in o:
        d.get_id(ctx, o["ros_distro"], ptr(path, "ros_distro"), "ros_distro")
    if "rmw_implementation" in o:
        d.get_id(ctx, o["rmw_implementation"], ptr(path, "rmw_implementation"), "rmw_implementation")
    if "rmw_version" in o:
        d.get_version(ctx, o["rmw_version"], ptr(path, "rmw_version"), "rmw_version")
    if "cni" in o:
        _name_version(ctx, o["cni"], ptr(path, "cni"), "cni")
    if "kubernetes_version" in o:
        d.get_version(ctx, o["kubernetes_version"], ptr(path, "kubernetes_version"), "kubernetes_version", c.K8S_RE)
    if "container_runtime" in o:
        _name_version(ctx, o["container_runtime"], ptr(path, "container_runtime"), "container_runtime")


def _check_simulator(ctx: Collector, v: Any, path: str) -> Optional[bool]:
    o = _obj(ctx, v, path, ("used",), ("name", "version"))
    if o is None:
        return None
    used = d.get_bool(ctx, o["used"], ptr(path, "used"), "simulator.used") if "used" in o else None
    if used is True:
        for k in ("name", "version"):
            if k not in o:
                ctx.add("REVISION_MISSING" if k == "version" else "MISSING_FIELD", ptr(path, k), f"a used simulator needs {k!r}")
        if "name" in o:
            d.get_id(ctx, o["name"], ptr(path, "name"), "simulator name")
        if "version" in o:
            d.get_version(ctx, o["version"], ptr(path, "version"), "simulator version")
    elif used is False:
        for k in ("name", "version"):
            if k in o:
                ctx.add("FIELD_NOT_ALLOWED", ptr(path, k), "simulator.used is false; remove its details")
    return used


def _check_hardware(ctx: Collector, v: Any, path: str) -> None:
    o = _obj(ctx, v, path, ("cpu", "memory", "gpu", "os"), ("kernel_version",))
    if o is None:
        return
    if "cpu" in o:
        cp = ptr(path, "cpu")
        cpu = _obj(ctx, o["cpu"], cp, ("model", "logical_cores", "architecture"))
        if cpu is not None:
            if "model" in cpu:
                d.get_text(ctx, cpu["model"], ptr(cp, "model"), "cpu model")
            if "logical_cores" in cpu:
                d.get_int(ctx, cpu["logical_cores"], ptr(cp, "logical_cores"), 1, 4096, "logical_cores")
            if "architecture" in cpu:
                d.get_enum(ctx, cpu["architecture"], ptr(cp, "architecture"), c.ARCHITECTURES, "architecture")
    if "memory" in o:
        d.get_quantity(ctx, o["memory"], ptr(path, "memory"), "memory", positive=True, what="memory")
    if "gpu" in o:
        gp = ptr(path, "gpu")
        gpu = _obj(ctx, o["gpu"], gp, ("present",), ("model", "count", "driver_version"))
        if gpu is not None and "present" in gpu:
            present = d.get_bool(ctx, gpu["present"], ptr(gp, "present"), "gpu.present")
            if present is True:
                for k in ("model", "count", "driver_version"):
                    if k not in gpu:
                        ctx.add("MISSING_FIELD", ptr(gp, k), f"a present GPU needs {k!r}")
                if "model" in gpu:
                    d.get_text(ctx, gpu["model"], ptr(gp, "model"), "gpu model")
                if "count" in gpu:
                    d.get_int(ctx, gpu["count"], ptr(gp, "count"), 1, 64, "gpu count")
                if "driver_version" in gpu:
                    d.get_version(ctx, gpu["driver_version"], ptr(gp, "driver_version"), "gpu driver_version")
            elif present is False:
                for k in ("model", "count", "driver_version"):
                    if k in gpu:
                        ctx.add("FIELD_NOT_ALLOWED", ptr(gp, k), "gpu.present is false; remove its details")
    if "os" in o:
        d.get_text(ctx, o["os"], ptr(path, "os"), "os")
    if "kernel_version" in o:
        d.get_version(ctx, o["kernel_version"], ptr(path, "kernel_version"), "kernel_version")


def _check_placement(ctx: Collector, v: Any, path: str) -> None:
    items = _list(ctx, v, path, "placement")
    if items is None:
        return
    seen: set = set()
    for i, e in enumerate(items):
        p = ptr(path, i)
        o = _obj(ctx, e, p, ("component", "node", "tier"))
        if o is None:
            continue
        if "component" in o:
            comp = d.get_id(ctx, o["component"], ptr(p, "component"), "component")
            if comp is not None:
                if comp in seen:
                    ctx.add("DUPLICATE_ENTRY", ptr(p, "component"), f"component {d.show(comp)} placed twice")
                seen.add(comp)
        if "node" in o:
            d.get_id(ctx, o["node"], ptr(p, "node"), "node (symbolic name, never an address)")
        if "tier" in o:
            d.get_enum(ctx, o["tier"], ptr(p, "tier"), c.TIERS, "tier")


def _check_network(ctx: Collector, v: Any, path: str) -> None:
    o = _obj(ctx, v, path, ("profile", "shaped"), ("shaping_tool", "one_way_delay", "jitter", "loss", "bandwidth"))
    if o is None:
        return
    if "profile" in o:
        d.get_id(ctx, o["profile"], ptr(path, "profile"), "network profile")
    shaped = d.get_bool(ctx, o["shaped"], ptr(path, "shaped"), "network.shaped") if "shaped" in o else None
    shaping_keys = ("shaping_tool", "one_way_delay", "jitter", "loss", "bandwidth")
    if shaped is False:
        for k in shaping_keys:
            if k in o:
                ctx.add("FIELD_NOT_ALLOWED", ptr(path, k), "network.shaped is false; remove shaping details")
        return
    if shaped is True and "shaping_tool" not in o:
        ctx.add("MISSING_FIELD", ptr(path, "shaping_tool"), "a shaped network must name the shaping tool")
    if "shaping_tool" in o:
        d.get_text(ctx, o["shaping_tool"], ptr(path, "shaping_tool"), "shaping_tool")
    for k in ("one_way_delay", "jitter"):
        if k in o:
            d.get_quantity(ctx, o[k], ptr(path, k), "time", what=k)
    if "loss" in o:
        d.get_quantity(ctx, o["loss"], ptr(path, "loss"), "ratio", what="loss", ratio_max=1.0)
    if "bandwidth" in o:
        d.get_quantity(ctx, o["bandwidth"], ptr(path, "bandwidth"), "bandwidth", positive=True, what="bandwidth")


def _check_qos(ctx: Collector, v: Any, path: str) -> None:
    items = _list(ctx, v, path, "qos", 1)
    if items is None:
        return
    seen: set = set()
    for i, e in enumerate(items):
        p = ptr(path, i)
        o = _obj(ctx, e, p, ("topic", "reliability", "durability", "history"), ("depth", "deadline", "lifespan"))
        if o is None:
            continue
        if "topic" in o:
            t = d.get_str(ctx, o["topic"], ptr(p, "topic"), c.TOPIC_RE, "ID_MALFORMED", "topic")
            if t is not None:
                if t in seen:
                    ctx.add("DUPLICATE_ENTRY", ptr(p, "topic"), f"topic {d.show(t)} listed twice")
                seen.add(t)
        if "reliability" in o:
            d.get_enum(ctx, o["reliability"], ptr(p, "reliability"), c.RELIABILITY, "reliability")
        if "durability" in o:
            d.get_enum(ctx, o["durability"], ptr(p, "durability"), c.DURABILITY, "durability")
        hist = d.get_enum(ctx, o["history"], ptr(p, "history"), c.HISTORY, "history") if "history" in o else None
        if hist == "keep_last":
            if "depth" not in o:
                ctx.add("MISSING_FIELD", ptr(p, "depth"), "history keep_last needs depth")
            else:
                d.get_int(ctx, o["depth"], ptr(p, "depth"), 1, 100000, "depth")
        elif hist == "keep_all" and "depth" in o:
            ctx.add("FIELD_NOT_ALLOWED", ptr(p, "depth"), "history keep_all takes no depth")
        for k in ("deadline", "lifespan"):
            if k in o:
                d.get_quantity(ctx, o[k], ptr(p, k), "time", positive=True, what=k)


def _check_asset(ctx: Collector, v: Any, path: str, what: str, hash_key: str) -> None:
    o = _obj(ctx, v, path, ("used",), ("name", "revision", "license", hash_key))
    if o is None or "used" not in o:
        return
    used = d.get_bool(ctx, o["used"], ptr(path, "used"), f"{what}.used")
    if used is True:
        for k in ("name", "revision", "license"):
            if k not in o:
                ctx.add(MISSING_CODES.get(k, "MISSING_FIELD"), ptr(path, k), f"a used {what} needs {k!r}")
        if "name" in o:
            d.get_id(ctx, o["name"], ptr(path, "name"), f"{what} name")
        if "revision" in o:
            d.get_hex(ctx, o["revision"], ptr(path, "revision"), c.GIT_SHA_RE, f"{what} revision (40 lowercase hex commit)")
        if "license" in o:
            _license(ctx, o["license"], ptr(path, "license"))
        if hash_key in o:
            d.get_hex(ctx, o[hash_key], ptr(path, hash_key), c.SHA256_RE, hash_key)
    elif used is False:
        for k in ("name", "revision", "license", hash_key):
            if k in o:
                ctx.add("FIELD_NOT_ALLOWED", ptr(path, k), f"{what}.used is false; remove its details")


def _check_clocks(ctx: Collector, v: Any, path: str) -> Optional[str]:
    o = _obj(ctx, v, path, ("clock_basis", "clock_domain", "simulation_time"), ("sync_method", "estimated_max_offset"))
    if o is None:
        return None
    if "clock_basis" in o:
        d.get_enum(ctx, o["clock_basis"], ptr(path, "clock_basis"), c.CLOCK_BASES, "clock_basis")
    if "simulation_time" in o:
        d.get_bool(ctx, o["simulation_time"], ptr(path, "simulation_time"), "simulation_time")
    domain = d.get_enum(ctx, o["clock_domain"], ptr(path, "clock_domain"), c.CLOCK_DOMAINS, "clock_domain") if "clock_domain" in o else None
    if domain == "synchronized":
        for k in ("sync_method", "estimated_max_offset"):
            if k not in o:
                ctx.add("CLOCK_SYNC_INCOMPLETE", ptr(path, k), f"clock_domain synchronized needs {k!r}")
        if "sync_method" in o:
            d.get_enum(ctx, o["sync_method"], ptr(path, "sync_method"), c.SYNC_METHODS, "sync_method")
        if "estimated_max_offset" in o:
            d.get_quantity(ctx, o["estimated_max_offset"], ptr(path, "estimated_max_offset"), "time", what="estimated_max_offset")
    elif domain in ("single_clock", "unsynchronized"):
        for k in ("sync_method", "estimated_max_offset"):
            if k in o:
                ctx.add("CLOCK_SYNC_INCOMPLETE", ptr(path, k), f"clock_domain {domain} must not carry {k!r}")
    return domain


def _check_timing(ctx: Collector, v: Any, path: str) -> None:
    o = _obj(ctx, v, path, ("control_rate", "action_age_deadline"))
    if o is None:
        return
    if "control_rate" in o:
        d.get_quantity(ctx, o["control_rate"], ptr(path, "control_rate"), "rate", positive=True, what="control_rate")
    if "action_age_deadline" in o:
        d.get_quantity(ctx, o["action_age_deadline"], ptr(path, "action_age_deadline"), "time", positive=True, what="action_age_deadline")


def _check_action_space(ctx: Collector, v: Any, path: str) -> None:
    o = _obj(ctx, v, path, ("name", "dimension", "names", "units", "actuation"))
    if o is None:
        return
    if "name" in o:
        d.get_id(ctx, o["name"], ptr(path, "name"), "action space name")
    dim = d.get_int(ctx, o["dimension"], ptr(path, "dimension"), 1, 64, "dimension") if "dimension" in o else None
    for key, pattern, bad, what in (("names", c.ID_RE, "ID_MALFORMED", "action name"), ("units", c.UNIT_TEXT_RE, "STRING_INVALID", "action unit")):
        if key not in o:
            continue
        lst = _list(ctx, o[key], ptr(path, key), key, 1, "UNIT_MISSING" if key == "units" else "MISSING_FIELD")
        if lst is None:
            continue
        for i, item in enumerate(lst):
            d.get_str(ctx, item, ptr(path, key, i), pattern, bad, what)
        if dim is not None and len(lst) != dim:
            ctx.add("DIM_MISMATCH", ptr(path, key), f"{len(lst)} {key} for dimension {dim}")
        if key == "names":
            strs = [x for x in lst if isinstance(x, str)]
            if len(set(strs)) != len(strs):
                ctx.add("DUPLICATE_ENTRY", ptr(path, key), "action names must be unique")
    if "actuation" in o:
        a = o["actuation"]
        if not isinstance(a, str):
            ctx.add("WRONG_TYPE", ptr(path, "actuation"), f"actuation must be the string 'none', got {d.type_name(a)}")
        elif a != "none":
            ctx.add("ACTUATION_NOT_NONE", ptr(path, "actuation"), f"actuation {d.show(a)} is not 'none'")


def _check_fault(ctx: Collector, v: Any, path: str) -> None:
    o = _obj(ctx, v, path, ("applied",), ("kind", "duration"))
    if o is None or "applied" not in o:
        return
    applied = d.get_bool(ctx, o["applied"], ptr(path, "applied"), "fault_injection.applied")
    if applied is True:
        for k in ("kind", "duration"):
            if k not in o:
                ctx.add("FAULT_INCONSISTENT", ptr(path, k), f"applied=true needs {k!r}")
        if "kind" in o:
            d.get_id(ctx, o["kind"], ptr(path, "kind"), "fault kind")
        if "duration" in o:
            d.get_quantity(ctx, o["duration"], ptr(path, "duration"), "time", positive=True, what="fault duration")
    elif applied is False:
        for k in ("kind", "duration"):
            if k in o:
                ctx.add("FAULT_INCONSISTENT", ptr(path, k), f"applied=false forbids {k!r}")


def _check_measurement(ctx: Collector, v: Any, path: str) -> Optional[str]:
    o = _obj(ctx, v, path, ("kind", "tool", "tool_version"))
    if o is None:
        return None
    kind = d.get_enum(ctx, o["kind"], ptr(path, "kind"), c.MEASUREMENT_KINDS, "measurement kind") if "kind" in o else None
    if "tool" in o:
        d.get_id(ctx, o["tool"], ptr(path, "tool"), "measurement tool")
    if "tool_version" in o:
        d.get_version(ctx, o["tool_version"], ptr(path, "tool_version"), "tool_version")
    return kind


def _check_outcome(ctx: Collector, v: Any, path: str, timeout_s: Optional[float]) -> None:
    req = ("status", "censored", "observed_duration", "failure_count", "task_success")
    o = _obj(ctx, v, path, req, ("censor_reason",))
    if o is None:
        return
    status = d.get_enum(ctx, o["status"], ptr(path, "status"), c.RUN_STATUSES, "status") if "status" in o else None
    censored = d.get_bool(ctx, o["censored"], ptr(path, "censored"), "censored") if "censored" in o else None
    if censored is not None and status is not None:
        if status in ("timed_out", "aborted") and censored is False:
            ctx.add("CENSORING_INCONSISTENT", ptr(path, "censored"), f"status {status} must be censored=true")
        elif status == "completed" and censored is True:
            ctx.add("CENSORING_INCONSISTENT", ptr(path, "censored"), "a completed run cannot be censored")
    if censored is True:
        reason = o.get("censor_reason")
        if reason is None or (isinstance(reason, str) and not reason.strip()):
            ctx.add("CENSOR_REASON_MISSING", ptr(path, "censor_reason"), "a censored run needs a non-empty censor_reason")
        else:
            d.get_text(ctx, reason, ptr(path, "censor_reason"), "censor_reason")
    elif censored is False and o.get("censor_reason") is not None:
        ctx.add("CENSORING_INCONSISTENT", ptr(path, "censor_reason"), "censor_reason is set but censored is false")
    obs = None
    if "observed_duration" in o:
        obs = d.get_quantity(ctx, o["observed_duration"], ptr(path, "observed_duration"), "time", what="observed_duration")
    if status == "completed" and obs is not None and timeout_s is not None and obs > timeout_s:
        ctx.add("TIMEOUT_EXCEEDED", ptr(path, "observed_duration"), "a completed run observed longer than its timeout")
    fc = d.get_int(ctx, o["failure_count"], ptr(path, "failure_count"), 0, c.MAX_COUNT, "failure_count") if "failure_count" in o else None
    if fc == 0 and status in ("failed", "timed_out", "aborted"):
        ctx.add("FAILURE_COUNT_INCONSISTENT", ptr(path, "failure_count"), f"status {status} with failure_count 0")
    if "task_success" in o:
        tp = ptr(path, "task_success")
        ts = _obj(ctx, o["task_success"], tp, ("applicable", "value"))
        if ts is not None and "applicable" in ts:
            app = d.get_bool(ctx, ts["applicable"], ptr(tp, "applicable"), "task_success.applicable")
            if "value" in ts:
                val = ts["value"]
                if app is False and val is not None:
                    ctx.add("TASK_SUCCESS_INCONSISTENT", ptr(tp, "value"), "applicable=false needs value=null")
                elif app is True:
                    if val is None:
                        ctx.add("TASK_SUCCESS_INCONSISTENT", ptr(tp, "value"), "applicable=true needs true or false")
                    elif not isinstance(val, bool):
                        ctx.add("WRONG_TYPE", ptr(tp, "value"), f"value must be true/false/null, got {d.type_name(val)} {d.show(val)}")


def _check_data_hashes(ctx: Collector, v: Any, path: str) -> None:
    items = _list(ctx, v, path, "data_hashes", 1, "DIGEST_MISSING")
    if items is None:
        return
    seen: set = set()
    for i, e in enumerate(items):
        p = ptr(path, i)
        o = _obj(ctx, e, p, ("path", "role", "sha256"))
        if o is None:
            continue
        if "path" in o:
            s = d.get_str(ctx, o["path"], ptr(p, "path"), c.PATH_RE, "PATH_UNSAFE", "path")
            if s is not None:
                if any(seg in (".", "..") for seg in s.split("/")):
                    ctx.add("PATH_UNSAFE", ptr(p, "path"), "path contains '.' or '..' segments")
                elif s in seen:
                    ctx.add("DUPLICATE_ENTRY", ptr(p, "path"), f"path {d.show(s)} hashed twice")
                seen.add(s)
        if "role" in o:
            d.get_enum(ctx, o["role"], ptr(p, "role"), c.DATA_HASH_ROLES, "role")
        if "sha256" in o:
            d.get_hex(ctx, o["sha256"], ptr(p, "sha256"), c.SHA256_RE, "sha256 (64 lowercase hex)")


# --------------------------------------------------------------------------- entry points


def _validate(doc: Any, ctx: Collector) -> None:
    if not isinstance(doc, dict):
        ctx.add("MALFORMED_DOCUMENT", "", f"top-level value is {d.type_name(doc)}, expected an object")
        return
    _scan(ctx, doc)
    ver = doc.get("schema_version")
    if "schema_version" not in doc:
        ctx.add("MISSING_FIELD", "/schema_version", "schema_version is required")
        return
    if not isinstance(ver, str):
        ctx.add("WRONG_TYPE", "/schema_version", f"schema_version must be a string, got {d.type_name(ver)}")
        return
    if ver != c.SCHEMA_VERSION:
        ctx.add("SCHEMA_VERSION_UNKNOWN", "/schema_version", f"unknown schema_version {d.show(ver)}; this validator supports {c.SCHEMA_VERSION!r}")
        return
    _obj(ctx, doc, "", TOP_REQUIRED, TOP_OPTIONAL)
    kind = d.get_enum(ctx, doc["data_kind"], "/data_kind", c.DATA_KINDS, "data_kind") if "data_kind" in doc else None
    if "experiment_id" in doc:
        d.get_id(ctx, doc["experiment_id"], "/experiment_id", "experiment_id", placeholder_check=True)
    if "arm" in doc:
        d.get_id(ctx, doc["arm"], "/arm", "arm")
    if "description" in doc:
        d.get_text(ctx, doc["description"], "/description", "description")
    run = _check_run(ctx, doc["run"], "/run") if "run" in doc else {}
    dirty = _check_source(ctx, doc["source"], "/source") if "source" in doc else None
    if dirty is True and kind == "measured":
        ctx.add("SOURCE_DIRTY", "/source/git_dirty", "measured run from a dirty source tree")
    if "images" in doc:
        _check_images(ctx, doc["images"], "/images")
    if "software" in doc:
        _check_software(ctx, doc["software"], "/software")
    sim_used = _check_simulator(ctx, doc["simulator"], "/simulator") if "simulator" in doc else None
    if kind == "simulated" and sim_used is False:
        ctx.add("SIMULATOR_REQUIRED", "/simulator/used", "data_kind is 'simulated' but simulator.used is false")
    if "hardware" in doc:
        _check_hardware(ctx, doc["hardware"], "/hardware")
    if "placement" in doc:
        _check_placement(ctx, doc["placement"], "/placement")
    if "network" in doc:
        _check_network(ctx, doc["network"], "/network")
    if "qos" in doc:
        _check_qos(ctx, doc["qos"], "/qos")
    if "model" in doc:
        _check_asset(ctx, doc["model"], "/model", "model", "weights_sha256")
    if "dataset" in doc:
        _check_asset(ctx, doc["dataset"], "/dataset", "dataset", "content_sha256")
    if "code_license" in doc:
        _license(ctx, doc["code_license"], "/code_license")
    if "clocks" in doc:
        _check_clocks(ctx, doc["clocks"], "/clocks")
    if "timing" in doc:
        _check_timing(ctx, doc["timing"], "/timing")
    if "action_space" in doc:
        _check_action_space(ctx, doc["action_space"], "/action_space")
    if "fault_injection" in doc:
        _check_fault(ctx, doc["fault_injection"], "/fault_injection")
    if "measurement_source" in doc:
        mk = _check_measurement(ctx, doc["measurement_source"], "/measurement_source")
        if mk is not None and kind is not None:
            if (kind == "synthetic") != (mk == "synthetic_generator"):
                ctx.add("MEASUREMENT_KIND_MISMATCH", "/measurement_source/kind",
                        f"data_kind {kind!r} with measurement kind {mk!r}")
    if "outcome" in doc:
        _check_outcome(ctx, doc["outcome"], "/outcome", run.get("timeout_s"))
    if "data_hashes" in doc:
        _check_data_hashes(ctx, doc["data_hashes"], "/data_hashes")


def validate_document(doc: Any) -> Result:
    """Validate an already-parsed manifest. Never raises."""
    ctx = Collector()
    try:
        _validate(doc, ctx)
    except Exception as exc:  # fail closed: an internal error makes the document invalid
        ctx.add("INTERNAL_ERROR", "", f"unexpected {type(exc).__name__}")
    return ctx.result()


def validate_text(text: str) -> Result:
    doc, diags = d.parse_json_text(text)
    if diags:
        return Result(tuple(diags))
    return validate_document(doc)


def validate_file(path: str) -> Result:
    doc, diags = d.read_json_file(path)
    if diags:
        return Result(tuple(diags))
    return validate_document(doc)


def sha256_file(path: str) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def verify_data_hashes(doc: Any, base_dir: str) -> Result:
    """Opt-in: check each ``data_hashes`` entry against real files under ``base_dir``.

    Only meaningful for a manifest that already validated. Never raises.
    """
    ctx = Collector()
    try:
        entries = doc.get("data_hashes") if isinstance(doc, dict) else None
        if not isinstance(entries, list):
            ctx.add("MISSING_FIELD", "/data_hashes", "no data_hashes list to verify")
        else:
            for i, e in enumerate(entries):
                if not isinstance(e, dict) or not isinstance(e.get("path"), str) or not isinstance(e.get("sha256"), str):
                    continue  # shape problems belong to validate_document
                rel = e["path"]
                if c.PATH_RE.fullmatch(rel) is None or any(s in (".", "..") for s in rel.split("/")):
                    ctx.add("PATH_UNSAFE", ptr("/data_hashes", i, "path"), "refusing to open an unsafe path")
                    continue
                actual = sha256_file(os.path.join(base_dir, *rel.split("/")))
                if actual is None:
                    ctx.add("HASH_FILE_MISSING", ptr("/data_hashes", i, "path"), f"{d.show(rel)} not found under the base directory")
                elif actual != e["sha256"]:
                    ctx.add("HASH_MISMATCH", ptr("/data_hashes", i, "sha256"), f"sha256 of {d.show(rel)} differs from the manifest")
    except Exception as exc:
        ctx.add("INTERNAL_ERROR", "", f"unexpected {type(exc).__name__}")
    return ctx.result()


# --------------------------------------------------------------------------- CLI


def build_report(results: list) -> dict:
    files = [
        {
            "file": name,
            "ok": res.valid,
            "error_count": len(res.errors),
            "warning_count": len(res.diagnostics) - len(res.errors),
            "truncated": res.truncated,
            "diagnostics": [x.as_dict() for x in res.diagnostics],
        }
        for name, res in results
    ]
    return {"report_version": c.REPORT_VERSION, "schema_version": c.SCHEMA_VERSION, "ok": all(f["ok"] for f in files), "files": files}


def format_text(report: dict) -> str:
    lines = []
    for f in report["files"]:
        lines.append(f"{'OK  ' if f['ok'] else 'FAIL'} {d.show(f['file'], 200)}")
        for x in f["diagnostics"]:
            lines.append(f"  [{x['severity']}:{x['code']}] {x['path'] or '(document)'}: {x['message']}")
            lines.append(f"      hint: {x['hint']}")
        if f["truncated"]:
            lines.append(f"  ... output truncated at {c.MAX_DIAGNOSTICS} diagnostics")
    bad = sum(1 for f in report["files"] if not f["ok"])
    lines.append(f"{len(report['files']) - bad} valid, {bad} invalid ({c.SCHEMA_VERSION})")
    return "\n".join(lines)


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="rm_validate.py",
        description="Validate run manifests (contract run-manifest/v1). Offline, stdlib only.",
        epilog="Exit codes: 0 all valid, 1 at least one invalid, 2 usage or I/O error.",
    )
    ap.add_argument("files", nargs="*", help="manifest JSON file(s)")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    ap.add_argument("--list-codes", action="store_true", help="print every diagnostic code and exit")
    ap.add_argument("--verify-hashes", metavar="BASE_DIR", help="also check data_hashes[] against files under BASE_DIR")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code in (0, None) else 2
    if args.list_codes:
        for code in sorted(c.CODES):
            meaning, hint = c.CODES[code]
            sev = "warning" if code in c.WARNING_CODES else "error"
            print(f"{code} ({sev})\n    {meaning}\n    fix: {hint}")
        return 0
    if not args.files:
        ap.print_usage(sys.stderr)
        print("error: give at least one file (or --list-codes)", file=sys.stderr)
        return 2
    results = []
    for path in args.files:
        doc, diags = d.read_json_file(path)
        if diags:
            res = Result(tuple(diags))
        else:
            res = validate_document(doc)
            if args.verify_hashes is not None and res.valid:
                extra = verify_data_hashes(doc, args.verify_hashes)
                res = Result(res.diagnostics + extra.diagnostics, res.truncated or extra.truncated)
        results.append((path, res))
    report = build_report(results)
    if args.format == "json":
        print(json.dumps(report, indent=2, ensure_ascii=True))
    else:
        print(format_text(report))
    io_error = any(x.code == "FILE_UNREADABLE" for _, res in results for x in res.diagnostics)
    if io_error:
        return 2
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
