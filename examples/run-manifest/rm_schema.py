"""Builders for the JSON Schema documents (stdlib only).

``schema/run-manifest.v1.schema.json`` and ``schema/run-metrics.v1.schema.json``
are GENERATED from the same constants the validators use, by ``fixture_gen.py``
(checked for drift with ``fixture_gen.py --check``).

The schemas document the *structure* (types, enums, patterns, required keys and
the used/applied conditionals). JSON Schema cannot express the cross-field and
cross-file rules (end >= start, warmup < timeout, status vs censored, dimension
equality, clock rules, secret/address heuristics, duplicate keys); those are
enforced only by ``rm_validate.py`` / ``rm_runfile.py``, which are the
executable authority.
"""

from __future__ import annotations

import rm_contract as c
import rm_metrics as m

SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
BASE_ID = "https://github.com/MSKazemi/robo-resilience-lab/examples/run-manifest/schema/"


def _pat(rx) -> str:
    return "^" + rx.pattern + "$"


def _obj(props: dict, required=(), extra=None) -> dict:
    out = {
        "type": "object",
        "required": list(required),
        "properties": props,
        "additionalProperties": False,
        "patternProperties": {"^x_": {}},
    }
    if extra:
        out["allOf"] = extra
    return out


def _str(rx=None, **kw) -> dict:
    out: dict = {"type": "string", "minLength": 1, "maxLength": c.MAX_TEXT}
    if rx is not None:
        out["pattern"] = _pat(rx)
    out.update(kw)
    return out


def _enum(values) -> dict:
    return {"type": "string", "enum": list(values)}


def _int(lo: int, hi: int) -> dict:
    return {"type": "integer", "minimum": lo, "maximum": hi}


def _qty(kind: str, positive: bool = False, ratio: bool = False) -> dict:
    val: dict = {"type": "number", "maximum": c.MAX_QUANTITY}
    if positive:
        val["exclusiveMinimum"] = 0
    else:
        val["minimum"] = 0
    return {
        "type": "object",
        "required": ["value", "unit"],
        "properties": {"value": val, "unit": _enum(c.UNITS[kind])},
        "additionalProperties": False,
        "patternProperties": {"^x_": {}},
        **({"description": "ratio: fraction must be <= 1 and percent <= 100 (checked by the validator)"} if ratio else {}),
    }


def _when(flag: str, value: bool, then_required=(), forbid=()) -> dict:
    cond = {"properties": {flag: {"const": value}}, "required": [flag]}
    then: dict = {}
    if then_required:
        then["required"] = list(then_required)
    if forbid:
        then["properties"] = {k: False for k in forbid}
    return {"if": cond, "then": then}


def _asset(hash_key: str) -> dict:
    props = {
        "used": {"type": "boolean"},
        "name": _str(c.ID_RE),
        "revision": {"type": "string", "pattern": _pat(c.GIT_SHA_RE), "description": "40 lowercase hex commit"},
        "license": _str(c.SPDX_RE, description="SPDX id; non-answers (unknown, none, ...) are rejected by the validator"),
        hash_key: {"type": "string", "pattern": _pat(c.SHA256_RE)},
    }
    return _obj(props, ["used"], [
        _when("used", True, ("name", "revision", "license")),
        _when("used", False, (), ("name", "revision", "license", hash_key)),
    ])


def manifest_schema() -> dict:
    run = _obj(
        {
            "independent_run_id": _str(c.ID_RE),
            "group_id": _str(c.ID_RE),
            "run_index": _int(0, 10**6),
            "fresh_deployment": {"type": "boolean"},
            "seed": _int(0, c.MAX_SEED),
            "started_utc": {"type": "string", "pattern": _pat(c.TS_RE)},
            "ended_utc": {"type": "string", "pattern": _pat(c.TS_RE)},
            "warmup": _qty("time"),
            "timeout": _qty("time", positive=True),
        },
        ["independent_run_id", "group_id", "run_index", "fresh_deployment", "seed", "started_utc", "ended_utc", "warmup", "timeout"],
    )
    nv = lambda: _obj({"name": _str(c.ID_RE), "version": _str(c.VERSION_RE)}, ["name", "version"])  # noqa: E731
    props = {
        "schema_version": {"const": c.SCHEMA_VERSION},
        "data_kind": _enum(c.DATA_KINDS),
        "experiment_id": _str(c.ID_RE),
        "arm": _str(c.ID_RE),
        "description": _str(c.TEXT_RE),
        "run": run,
        "source": _obj(
            {"git_sha": {"type": "string", "pattern": _pat(c.GIT_SHA_RE)}, "git_dirty": {"type": "boolean"}},
            ["git_sha", "git_dirty"],
        ),
        "images": {
            "type": "array",
            "minItems": 1,
            "maxItems": c.MAX_LIST,
            "items": _obj({"name": _str(c.ID_RE), "oci_digest": {"type": "string", "pattern": _pat(c.OCI_DIGEST_RE)}}, ["name", "oci_digest"]),
        },
        "software": _obj(
            {
                "ros_distro": _str(c.ID_RE),
                "rmw_implementation": _str(c.ID_RE),
                "rmw_version": _str(c.VERSION_RE),
                "cni": nv(),
                "kubernetes_version": _str(c.K8S_RE),
                "container_runtime": nv(),
            },
            ["ros_distro", "rmw_implementation", "rmw_version", "cni", "kubernetes_version"],
        ),
        "simulator": _obj(
            {"used": {"type": "boolean"}, "name": _str(c.ID_RE), "version": _str(c.VERSION_RE)},
            ["used"],
            [_when("used", True, ("name", "version")), _when("used", False, (), ("name", "version"))],
        ),
        "hardware": _obj(
            {
                "cpu": _obj(
                    {"model": _str(c.TEXT_RE), "logical_cores": _int(1, 4096), "architecture": _enum(c.ARCHITECTURES)},
                    ["model", "logical_cores", "architecture"],
                ),
                "memory": _qty("memory", positive=True),
                "gpu": _obj(
                    {"present": {"type": "boolean"}, "model": _str(c.TEXT_RE), "count": _int(1, 64), "driver_version": _str(c.VERSION_RE)},
                    ["present"],
                    [_when("present", True, ("model", "count", "driver_version")), _when("present", False, (), ("model", "count", "driver_version"))],
                ),
                "os": _str(c.TEXT_RE),
                "kernel_version": _str(c.VERSION_RE),
            },
            ["cpu", "memory", "gpu", "os"],
        ),
        "placement": {
            "type": "array",
            "minItems": 1,
            "maxItems": c.MAX_LIST,
            "items": _obj(
                {"component": _str(c.ID_RE), "node": _str(c.ID_RE, description="symbolic name, never an address"), "tier": _enum(c.TIERS)},
                ["component", "node", "tier"],
            ),
        },
        "network": _obj(
            {
                "profile": _str(c.ID_RE),
                "shaped": {"type": "boolean"},
                "shaping_tool": _str(c.TEXT_RE),
                "one_way_delay": _qty("time"),
                "jitter": _qty("time"),
                "loss": _qty("ratio", ratio=True),
                "bandwidth": _qty("bandwidth", positive=True),
            },
            ["profile", "shaped"],
            [_when("shaped", True, ("shaping_tool",)), _when("shaped", False, (), ("shaping_tool", "one_way_delay", "jitter", "loss", "bandwidth"))],
        ),
        "qos": {
            "type": "array",
            "minItems": 1,
            "maxItems": c.MAX_LIST,
            "items": _obj(
                {
                    "topic": _str(c.TOPIC_RE),
                    "reliability": _enum(c.RELIABILITY),
                    "durability": _enum(c.DURABILITY),
                    "history": _enum(c.HISTORY),
                    "depth": _int(1, 100000),
                    "deadline": _qty("time", positive=True),
                    "lifespan": _qty("time", positive=True),
                },
                ["topic", "reliability", "durability", "history"],
                [
                    {"if": {"properties": {"history": {"const": "keep_last"}}, "required": ["history"]}, "then": {"required": ["depth"]}},
                    {"if": {"properties": {"history": {"const": "keep_all"}}, "required": ["history"]}, "then": {"properties": {"depth": False}}},
                ],
            ),
        },
        "model": _asset("weights_sha256"),
        "dataset": _asset("content_sha256"),
        "code_license": _str(c.SPDX_RE),
        "clocks": _obj(
            {
                "clock_basis": _enum(c.CLOCK_BASES),
                "clock_domain": _enum(c.CLOCK_DOMAINS),
                "simulation_time": {"type": "boolean"},
                "sync_method": _enum(c.SYNC_METHODS),
                "estimated_max_offset": _qty("time"),
            },
            ["clock_basis", "clock_domain", "simulation_time"],
            [
                {"if": {"properties": {"clock_domain": {"const": "synchronized"}}, "required": ["clock_domain"]}, "then": {"required": ["sync_method", "estimated_max_offset"]}},
                {"if": {"properties": {"clock_domain": {"enum": ["single_clock", "unsynchronized"]}}, "required": ["clock_domain"]}, "then": {"properties": {"sync_method": False, "estimated_max_offset": False}}},
            ],
        ),
        "timing": _obj({"control_rate": _qty("rate", positive=True), "action_age_deadline": _qty("time", positive=True)}, ["control_rate", "action_age_deadline"]),
        "action_space": _obj(
            {
                "name": _str(c.ID_RE),
                "dimension": _int(1, 64),
                "names": {"type": "array", "minItems": 1, "maxItems": 64, "items": _str(c.ID_RE)},
                "units": {"type": "array", "minItems": 1, "maxItems": 64, "items": {"type": "string", "pattern": _pat(c.UNIT_TEXT_RE)}},
                "actuation": {"const": "none"},
            },
            ["name", "dimension", "names", "units", "actuation"],
        ),
        "fault_injection": _obj(
            {"applied": {"type": "boolean"}, "kind": _str(c.ID_RE), "duration": _qty("time", positive=True)},
            ["applied"],
            [_when("applied", True, ("kind", "duration")), _when("applied", False, (), ("kind", "duration"))],
        ),
        "measurement_source": _obj(
            {"kind": _enum(c.MEASUREMENT_KINDS), "tool": _str(c.ID_RE), "tool_version": _str(c.VERSION_RE)},
            ["kind", "tool", "tool_version"],
        ),
        "outcome": _obj(
            {
                "status": _enum(c.RUN_STATUSES),
                "censored": {"type": "boolean"},
                "censor_reason": {"type": ["string", "null"], "maxLength": c.MAX_TEXT},
                "observed_duration": _qty("time"),
                "failure_count": _int(0, c.MAX_COUNT),
                "task_success": _obj({"applicable": {"type": "boolean"}, "value": {"type": ["boolean", "null"]}}, ["applicable", "value"]),
            },
            ["status", "censored", "observed_duration", "failure_count", "task_success"],
        ),
        "data_hashes": {
            "type": "array",
            "minItems": 1,
            "maxItems": c.MAX_LIST,
            "items": _obj(
                {"path": _str(c.PATH_RE), "role": _enum(c.DATA_HASH_ROLES), "sha256": {"type": "string", "pattern": _pat(c.SHA256_RE)}},
                ["path", "role", "sha256"],
            ),
        },
    }
    top = _obj(props, [k for k in props if k != "description"])
    return {
        "$schema": SCHEMA_DIALECT,
        "$id": BASE_ID + "run-manifest.v1.schema.json",
        "title": "Run manifest, contract run-manifest/v1",
        "description": (
            "Structural documentation of the contract. The executable authority is rm_validate.py, which also checks what "
            "JSON Schema cannot express: duplicate keys, NaN/Infinity, ended_utc >= started_utc, warmup < timeout, "
            "status vs censored vs failure_count consistency, task_success consistency, array lengths vs dimension, "
            "measurement kind vs data_kind, placeholder hashes, secret/address heuristics. Every example in this "
            "directory is SYNTHETIC. Vendor extension keys start with 'x_'."
        ),
        **top,
    }


def runfile_schema() -> dict:
    metric_props = {}
    for met in m.METRICS:
        mid = met["id"]
        if met["value_domain"] == "boolean":
            val: dict = {"type": ["boolean", "null"]}
        elif met["value_domain"] == "non_negative_int":
            val = {"type": ["integer", "null"], "minimum": 0}
        elif met["value_domain"] == "fraction_0_1":
            val = {"type": ["number", "null"], "minimum": 0, "maximum": 1}
        else:
            val = {"type": ["number", "null"], "minimum": 0}
        props = {
            "status": _enum(m.STATUSES if met["censorable"] else tuple(s for s in m.STATUSES if s != "censored")),
            "value": val,
            "unit": {"const": met["unit"]},
            "reason": _enum(m.NOT_COMPUTABLE_REASONS),
            "censored_lower_bound": {"type": "number", "minimum": 0},
        }
        if mid in m.SAMPLE_COUNT_METRICS:
            props["n_samples"] = _int(1, c.MAX_COUNT)
        for k in m.EXTRA_FIELDS.get(mid, ()):
            props[k] = _int(0, c.MAX_COUNT)
        metric_props[mid] = _obj(props, ["status", "value", "unit"])
    return {
        "$schema": SCHEMA_DIALECT,
        "$id": BASE_ID + "run-metrics.v1.schema.json",
        "title": "Per-run metric file, contract run-metrics/v1",
        "description": (
            "One file per independent run. Every registry metric must have an entry. The executable authority is "
            "rm_runfile.py, which also checks status/value/field consistency, ratios, and the cross-check against the "
            "manifest (ids, arm, data_kind, manifest sha256, clock requirements, applicability). SYNTHETIC fixtures only."
        ),
        **_obj(
            {
                "schema_version": {"const": c.RUNFILE_VERSION},
                "data_kind": _enum(c.DATA_KINDS),
                "independent_run_id": _str(c.ID_RE),
                "arm": _str(c.ID_RE),
                "manifest_file": _str(c.PATH_RE),
                "manifest_sha256": {"type": "string", "pattern": _pat(c.SHA256_RE)},
                "metrics": _obj(metric_props, list(m.METRIC_IDS)),
            },
            ["schema_version", "data_kind", "independent_run_id", "arm", "manifest_file", "manifest_sha256", "metrics"],
        ),
    }
