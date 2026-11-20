"""Shared helpers for the run-manifest tests (stdlib only)."""

import copy
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import rm_contract as c  # noqa: E402,F401
import rm_validate as v  # noqa: E402,F401

FIX = os.path.join(ROOT, "fixtures")
VALID_DIR = os.path.join(FIX, "manifests", "valid")
INVALID_DIR = os.path.join(FIX, "manifests", "invalid")
RUN_DIR = os.path.join(FIX, "run-files")
LOSSY1 = "syn-edge-cloud-lossy-r01"
LOCAL1 = "syn-local-baseline-r01"


def load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def base(rid=LOSSY1):
    return copy.deepcopy(load(os.path.join(VALID_DIR, rid + ".json")))


def codes(doc):
    return v.validate_document(doc).codes


def pairs(doc):
    return [(d.code, d.path) for d in v.validate_document(doc).diagnostics]


def set_path(doc, path, value):
    cur = doc
    for k in path[:-1]:
        cur = cur[k]
    cur[path[-1]] = value


def del_path(doc, path):
    cur = doc
    for k in path[:-1]:
        cur = cur[k]
    del cur[path[-1]]


def walk(doc, prefix=()):
    """Yield (path tuple, node) for every node below the root."""
    if isinstance(doc, dict):
        for k, val in doc.items():
            yield prefix + (k,), val
            yield from walk(val, prefix + (k,))
    elif isinstance(doc, list):
        for i, val in enumerate(doc):
            yield prefix + (i,), val
            yield from walk(val, prefix + (i,))


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def copy_fixture_tree():
    """Copy fixtures/ to a temp dir; returns (tmp_root, fixtures_dir, run_dir). Caller removes tmp_root."""
    tmp = tempfile.mkdtemp(prefix="rm-test-")
    dst = os.path.join(tmp, "fixtures")
    shutil.copytree(FIX, dst)
    return tmp, dst, os.path.join(dst, "run-files")


def write_json(path, doc):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")


def rehash_manifest(fixtures_dir, rid, runfile_path=None):
    """After editing a manifest copy, refresh manifest_sha256 in its run file."""
    mpath = os.path.join(fixtures_dir, "manifests", "valid", rid + ".json")
    rpath = runfile_path or os.path.join(fixtures_dir, "run-files", rid + ".json")
    run = load(rpath)
    with open(mpath, "rb") as fh:
        run["manifest_sha256"] = hashlib.sha256(fh.read()).hexdigest()
    write_json(rpath, run)


# ---- tiny JSON Schema subset interpreter (draft 2020-12 keywords used by our schemas) ----


def _is_type(val, t):
    if t == "object":
        return isinstance(val, dict)
    if t == "array":
        return isinstance(val, list)
    if t == "string":
        return isinstance(val, str)
    if t == "boolean":
        return isinstance(val, bool)
    if t == "integer":
        return isinstance(val, int) and not isinstance(val, bool)
    if t == "number":
        return isinstance(val, (int, float)) and not isinstance(val, bool)
    if t == "null":
        return val is None
    raise AssertionError("unsupported type " + t)


def schema_errors(val, sch, path=""):
    """Return a list of violation strings; supports only the keywords our schemas use."""
    if sch is True:
        return []
    if sch is False:
        return [f"{path}: forbidden"]
    errs = []
    if "const" in sch and not (val == sch["const"] and type(val) is type(sch["const"])):
        errs.append(f"{path}: const")
    if "enum" in sch and not (isinstance(val, str) and val in sch["enum"]):
        errs.append(f"{path}: enum")
    if "type" in sch:
        ts = sch["type"] if isinstance(sch["type"], list) else [sch["type"]]
        if not any(_is_type(val, t) for t in ts):
            return errs + [f"{path}: type"]
    if isinstance(val, str):
        if "minLength" in sch and len(val) < sch["minLength"]:
            errs.append(f"{path}: minLength")
        if "maxLength" in sch and len(val) > sch["maxLength"]:
            errs.append(f"{path}: maxLength")
        if "pattern" in sch:
            pat = sch["pattern"]
            assert pat.endswith("$")
            if re.search(pat[:-1] + r"\Z", val) is None:  # ECMA `$` has no trailing-newline slack
                errs.append(f"{path}: pattern")
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        if "minimum" in sch and val < sch["minimum"]:
            errs.append(f"{path}: minimum")
        if "maximum" in sch and val > sch["maximum"]:
            errs.append(f"{path}: maximum")
        if "exclusiveMinimum" in sch and val <= sch["exclusiveMinimum"]:
            errs.append(f"{path}: exclusiveMinimum")
    if isinstance(val, list):
        if "minItems" in sch and len(val) < sch["minItems"]:
            errs.append(f"{path}: minItems")
        if "maxItems" in sch and len(val) > sch["maxItems"]:
            errs.append(f"{path}: maxItems")
        if "items" in sch:
            for i, item in enumerate(val):
                errs += schema_errors(item, sch["items"], f"{path}/{i}")
    if isinstance(val, dict):
        for k in sch.get("required", []):
            if k not in val:
                errs.append(f"{path}/{k}: required")
        props = sch.get("properties", {})
        pprops = sch.get("patternProperties", {})
        for k, item in val.items():
            if k in props:
                errs += schema_errors(item, props[k], f"{path}/{k}")
                continue
            hit = [p for p in pprops if re.search(p, k)]
            if hit:
                for p in hit:
                    errs += schema_errors(item, pprops[p], f"{path}/{k}")
            elif sch.get("additionalProperties", True) is False:
                errs.append(f"{path}/{k}: additionalProperties")
    for sub in sch.get("allOf", []):
        errs += schema_errors(val, sub, path)
    if "if" in sch and not schema_errors(val, sch["if"], path):
        errs += schema_errors(val, sch.get("then", True), path)
    elif "if" in sch and "else" in sch:
        errs += schema_errors(val, sch["else"], path)
    return errs
