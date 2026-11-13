"""Case validator: schema, closed taxonomy, duplicate ids, type confusion, NaN, forbidden content.

``validate_case`` never raises: every problem becomes a typed ``Diag(code, path, message)``.
Type checks always run before membership tests, hashing, ``sorted`` or comparisons, so a list or dict
where a scalar is expected is a ``WRONG_TYPE`` diagnostic and never a ``TypeError``.

Rules for numbers: ``bool`` is rejected where an integer is required (``type(v) is int``); floats are
rejected everywhere (no field in the schema is fractional); NaN and infinity get their own code.
Strings: printable ASCII only, matched with ``fullmatch`` so a trailing newline is rejected.
"""

from __future__ import annotations

import json
import math
import os
import re
from collections import namedtuple
from pathlib import Path

from . import taxonomy as T

Diag = namedtuple("Diag", "code path message")

# Diagnostic codes (closed set; tests assert on these, not on message text).
UNKNOWN_KEY = "UNKNOWN_KEY"
MISSING_KEY = "MISSING_KEY"
WRONG_TYPE = "WRONG_TYPE"
BAD_VALUE = "BAD_VALUE"
OUT_OF_RANGE = "OUT_OF_RANGE"
NON_FINITE = "NON_FINITE"
BAD_ID = "BAD_ID"
BAD_TEXT = "BAD_TEXT"
INCONSISTENT = "INCONSISTENT"
DUPLICATE_ID = "DUPLICATE_ID"
DUPLICATE_KEY = "DUPLICATE_KEY"
SPLIT_CONFLICT = "SPLIT_CONFLICT"
UNKNOWN_CITATION = "UNKNOWN_CITATION"
FORBIDDEN_CONTENT = "FORBIDDEN_CONTENT"
UNREADABLE = "UNREADABLE"
FILE_TOO_LARGE = "FILE_TOO_LARGE"
BAD_FILENAME = "BAD_FILENAME"
UNEXPECTED_FILE = "UNEXPECTED_FILE"
TOO_MANY_FILES = "TOO_MANY_FILES"
NOT_SYNTHETIC = "NOT_SYNTHETIC"

CASE_ID_RE = re.compile(r"case-[0-9]{3}")
FAMILY_RE = re.compile(r"fam-[a-z0-9]+(?:-[a-z0-9]+)*")
FILENAME_RE = re.compile(r"case_[0-9]{3}\.json")
SOURCE_RE = re.compile(r"SYNTHETIC: [ -~]{1,200}")
IPV4_RE = re.compile(r"(?<![0-9])[0-9]{1,3}(?:\.[0-9]{1,3}){3}(?![0-9])")
# a long run of token characters that mixes letters and digits (plain snake_case identifiers do not match)
TOKEN_RE = re.compile(r"(?=[A-Za-z0-9+/_=-]*[0-9])(?=[A-Za-z0-9+/_=-]*[A-Za-z])[A-Za-z0-9+/_=-]{32,}")
EMAIL_RE = re.compile(r"[A-Za-z0-9._-]+@[A-Za-z0-9.-]+")
URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")

_JSON_TYPES = {dict: "an object", list: "an array", str: "a string", int: "an integer",
               float: "a number", bool: "a boolean", type(None): "null"}


def _tname(v):
    return _JSON_TYPES.get(type(v), type(v).__name__)


# ---------------------------------------------------------------------------------------------
# primitive checkers: each returns True only if the value is usable by the caller
# ---------------------------------------------------------------------------------------------
def _int(v, path, lo, hi, d):
    if type(v) is float and not math.isfinite(v):
        d.append(Diag(NON_FINITE, path, "NaN/infinity is not allowed"))
        return False
    if type(v) is not int:  # also rejects bool, float, list, dict, str, None
        d.append(Diag(WRONG_TYPE, path, f"expected an integer, got {_tname(v)}"))
        return False
    if not lo <= v <= hi:
        d.append(Diag(OUT_OF_RANGE, path, f"{v} is outside {lo}..{hi}"))
        return False
    return True


def _bool(v, path, d):
    if type(v) is not bool:
        d.append(Diag(WRONG_TYPE, path, f"expected a boolean, got {_tname(v)}"))
        return False
    return True


def _enum(v, path, allowed, d, nullable=False):
    if v is None and nullable:
        return True
    if type(v) is not str:
        d.append(Diag(WRONG_TYPE, path, f"expected a string, got {_tname(v)}"))
        return False
    if v not in allowed:
        d.append(Diag(BAD_VALUE, path, f"{v!r} is not one of {', '.join(allowed)}"))
        return False
    return True


def _text(v, path, maxlen, d):
    if type(v) is not str:
        d.append(Diag(WRONG_TYPE, path, f"expected a string, got {_tname(v)}"))
        return False
    if len(v) > maxlen:
        d.append(Diag(BAD_TEXT, path, f"longer than {maxlen} characters"))
        return False
    if re.fullmatch(r"[ -~]+", v) is None:
        d.append(Diag(BAD_TEXT, path, "must be non-empty printable ASCII (no newline, tab or non-ASCII)"))
        return False
    return True


def _pattern(v, path, rx, d, what):
    if type(v) is not str:
        d.append(Diag(WRONG_TYPE, path, f"expected a string, got {_tname(v)}"))
        return False
    if len(v) > 64 or rx.fullmatch(v) is None:
        d.append(Diag(BAD_ID, path, f"{what} does not match {rx.pattern}"))
        return False
    return True


def _object(v, path, required, optional, d):
    """Return the dict if it is an object; report every unknown or missing key."""
    if type(v) is not dict:
        d.append(Diag(WRONG_TYPE, path, f"expected an object, got {_tname(v)}"))
        return None
    for k in v:
        if type(k) is not str or (k not in required and k not in optional):
            d.append(Diag(UNKNOWN_KEY, f"{path}.{k}" if path else str(k), "key is not part of the schema"))
    for k in required:
        if k not in v:
            d.append(Diag(MISSING_KEY, f"{path}.{k}" if path else k, "required key is missing"))
    return v


def _array(v, path, maxlen, d, minlen=0):
    if type(v) is not list:
        d.append(Diag(WRONG_TYPE, path, f"expected an array, got {_tname(v)}"))
        return None
    if not minlen <= len(v) <= maxlen:
        d.append(Diag(OUT_OF_RANGE, path, f"array length {len(v)} is outside {minlen}..{maxlen}"))
        return None
    return v


# ---------------------------------------------------------------------------------------------
# evidence sections
# ---------------------------------------------------------------------------------------------
def _pod(v, path, d):
    req = ("phase", "ready", "restarts", "reason", "last_termination")
    o = _object(v, path, req, (), d)
    if o is None:
        return
    if "phase" in o:
        _enum(o["phase"], f"{path}.phase", T.POD_PHASES, d)
    if "ready" in o:
        _bool(o["ready"], f"{path}.ready", d)
    if "restarts" in o:
        _int(o["restarts"], f"{path}.restarts", 0, T.MAX_RESTARTS, d)
    if "reason" in o:
        _enum(o["reason"], f"{path}.reason", T.POD_REASONS, d, nullable=True)
    if "last_termination" in o:
        _enum(o["last_termination"], f"{path}.last_termination", T.POD_LAST_TERMINATIONS, d, nullable=True)


def _endpoint(v, path, d):
    o = _object(v, path, ("domain_id", "rmw", "qos"), (), d)
    if o is None:
        return
    if "domain_id" in o:
        _int(o["domain_id"], f"{path}.domain_id", 0, T.MAX_DOMAIN_ID, d)
    if "rmw" in o:
        _enum(o["rmw"], f"{path}.rmw", T.RMWS, d)
    if "qos" in o:
        q = _object(o["qos"], f"{path}.qos", ("reliability", "durability", "depth"), (), d)
        if q is not None:
            if "reliability" in q:
                _enum(q["reliability"], f"{path}.qos.reliability", T.RELIABILITIES, d)
            if "durability" in q:
                _enum(q["durability"], f"{path}.qos.durability", T.DURABILITIES, d)
            if "depth" in q:
                _int(q["depth"], f"{path}.qos.depth", 1, T.MAX_DEPTH, d)


def _discovery(v, path, d):
    o = _object(v, path, ("mode", "server_check"), (), d)
    if o is None:
        return
    mode_ok = "mode" in o and _enum(o["mode"], f"{path}.mode", T.DISCOVERY_MODES, d)
    sc_ok = "server_check" in o and _enum(o["server_check"], f"{path}.server_check", T.SERVER_CHECKS, d, nullable=True)
    if mode_ok and sc_ok:
        if o["mode"] == "multicast_default" and o["server_check"] is not None:
            d.append(Diag(INCONSISTENT, f"{path}.server_check", "must be null when mode is multicast_default"))
        if o["mode"] == "discovery_server" and o["server_check"] is None:
            d.append(Diag(INCONSISTENT, f"{path}.server_check", "must be set when mode is discovery_server"))


def _udp(v, path, d):
    o = _object(v, path, ("unicast", "multicast"), (), d)
    if o is None:
        return
    for k in ("unicast", "multicast"):
        if k in o:
            _enum(o[k], f"{path}.{k}", T.UDP_RESULTS, d)


def _ranges(v, path, expected, missing, d):
    arr = _array(v, path, 1000, d)
    if arr is None:
        return
    prev_end = -1
    total = 0
    ok = True
    for i, r in enumerate(arr):
        p = f"{path}[{i}]"
        pair = _array(r, p, 2, d, minlen=2)
        if pair is None:
            ok = False
            continue
        a_ok = _int(pair[0], f"{p}[0]", 0, T.MAX_COUNT, d)
        b_ok = _int(pair[1], f"{p}[1]", 0, T.MAX_COUNT, d)
        if not (a_ok and b_ok):
            ok = False
            continue
        a, b = pair
        if a > b or (expected is not None and b >= expected) or a <= prev_end:
            d.append(Diag(INCONSISTENT, p, "ranges must be sorted, non-overlapping, start<=end and inside 0..expected_count-1"))
            ok = False
            continue
        prev_end = b
        total += b - a + 1
    if ok and missing is not None and total != missing:
        d.append(Diag(INCONSISTENT, path, f"ranges cover {total} ids but missing is {missing}"))


def _probe(v, path, d):
    req = ("expected_count", "received_total", "unique", "duplicates", "reordered", "missing",
           "missing_ranges", "completion", "timeout_reason", "publisher_start_events", "ok")
    o = _object(v, path, req, (), d)
    if o is None:
        return
    ints = {}
    for k in ("expected_count", "received_total", "unique", "duplicates", "reordered", "missing"):
        if k in o:
            lo = 1 if k == "expected_count" else 0
            if _int(o[k], f"{path}.{k}", lo, T.MAX_COUNT, d):
                ints[k] = o[k]
    if "publisher_start_events" in o:
        _int(o["publisher_start_events"], f"{path}.publisher_start_events", 0, 100, d)
    comp_ok = "completion" in o and _enum(o["completion"], f"{path}.completion", T.COMPLETIONS, d)
    to_ok = "timeout_reason" in o and _enum(o["timeout_reason"], f"{path}.timeout_reason", T.TIMEOUT_REASONS, d, nullable=True)
    ok_ok = "ok" in o and _bool(o["ok"], f"{path}.ok", d)
    if "missing_ranges" in o:
        _ranges(o["missing_ranges"], f"{path}.missing_ranges", ints.get("expected_count"), ints.get("missing"), d)
    # cross-field rules that follow the probe's own Verdict definition (see tracker.py / README "Verdict")
    e, rt, u, dup, ro, mi = (ints.get(k) for k in ("expected_count", "received_total", "unique", "duplicates", "reordered", "missing"))
    if e is not None and u is not None:
        if u > e:
            d.append(Diag(INCONSISTENT, f"{path}.unique", "unique cannot exceed expected_count"))
        elif mi is not None and mi != e - u:
            d.append(Diag(INCONSISTENT, f"{path}.missing", "missing must equal expected_count - unique"))
    if rt is not None and u is not None and dup is not None and u + dup > rt:
        d.append(Diag(INCONSISTENT, f"{path}.received_total", "unique + duplicates cannot exceed received_total"))
    if u is not None and ro is not None and ro > u:
        d.append(Diag(INCONSISTENT, f"{path}.reordered", "reordered cannot exceed unique"))
    if comp_ok and e is not None and u is not None and (o["completion"] == "complete") != (u == e):
        d.append(Diag(INCONSISTENT, f"{path}.completion", "complete exactly when unique == expected_count"))
    if comp_ok and to_ok:
        if o["completion"] == "complete" and o["timeout_reason"] is not None:
            d.append(Diag(INCONSISTENT, f"{path}.timeout_reason", "must be null when completion is complete"))
        if o["completion"] == "timeout" and o["timeout_reason"] is None:
            d.append(Diag(INCONSISTENT, f"{path}.timeout_reason", "must be set when completion is timeout"))
    if ok_ok and o["ok"] and None not in (e, rt, u, dup, ro, mi) and comp_ok:
        clean = o["completion"] == "complete" and dup == 0 and ro == 0 and mi == 0 and rt == u
        if not clean:
            d.append(Diag(INCONSISTENT, f"{path}.ok", "ok is true only for a complete run with no duplicate, reordered, unexpected or missing message"))


def _latency(v, path, d):
    o = _object(v, path, ("clock_domain", "samples", "negative_samples", "min_ns", "max_ns"), (), d)
    if o is None:
        return
    if "clock_domain" in o:
        _enum(o["clock_domain"], f"{path}.clock_domain", T.CLOCK_DOMAINS, d)
    vals = {}
    for k, lo, hi in (("samples", 1, T.MAX_COUNT), ("negative_samples", 0, T.MAX_COUNT),
                      ("min_ns", -T.MAX_NS, T.MAX_NS), ("max_ns", -T.MAX_NS, T.MAX_NS)):
        if k in o and _int(o[k], f"{path}.{k}", lo, hi, d):
            vals[k] = o[k]
    if "samples" in vals and "negative_samples" in vals and vals["negative_samples"] > vals["samples"]:
        d.append(Diag(INCONSISTENT, f"{path}.negative_samples", "cannot exceed samples"))
    if "min_ns" in vals and "max_ns" in vals and vals["min_ns"] > vals["max_ns"]:
        d.append(Diag(INCONSISTENT, f"{path}.max_ns", "max_ns cannot be below min_ns"))
    if "negative_samples" in vals and "min_ns" in vals and (vals["negative_samples"] > 0) != (vals["min_ns"] < 0):
        d.append(Diag(INCONSISTENT, f"{path}.min_ns", "min_ns is negative exactly when negative_samples > 0"))


def _unique_enum_list(v, path, allowed, maxlen, d):
    arr = _array(v, path, maxlen, d)
    if arr is None:
        return None
    good = True
    for i, x in enumerate(arr):
        if not _enum(x, f"{path}[{i}]", allowed, d):
            good = False
    if good and len(set(arr)) != len(arr):
        d.append(Diag(INCONSISTENT, path, "duplicate entries"))
        return None
    return arr if good else None


EVIDENCE_KEYS = ("pods", "ros", "discovery", "udp_check", "probe_summary", "latency_summary",
                 "clock_offset_check", "fault_markers", "log_excerpt")


def validate_evidence(ev, path="evidence"):
    """Validate one evidence bundle (also used by the triager before it reads anything)."""
    d = []
    o = _object(ev, path, (), EVIDENCE_KEYS, d)
    if o is None:
        return d
    if "pods" in o:
        p = _object(o["pods"], f"{path}.pods", (), ("publisher", "subscriber"), d)
        if p is not None:
            for role in ("publisher", "subscriber"):
                if role in p:
                    _pod(p[role], f"{path}.pods.{role}", d)
    if "ros" in o:
        r = _object(o["ros"], f"{path}.ros", (), ("publisher", "subscriber"), d)
        if r is not None:
            for role in ("publisher", "subscriber"):
                if role in r:
                    _endpoint(r[role], f"{path}.ros.{role}", d)
    if "discovery" in o:
        _discovery(o["discovery"], f"{path}.discovery", d)
    if "udp_check" in o:
        _udp(o["udp_check"], f"{path}.udp_check", d)
    if "probe_summary" in o:
        _probe(o["probe_summary"], f"{path}.probe_summary", d)
    if "latency_summary" in o:
        _latency(o["latency_summary"], f"{path}.latency_summary", d)
    if "clock_offset_check" in o:
        _enum(o["clock_offset_check"], f"{path}.clock_offset_check", T.OFFSET_CHECKS, d)
    if "fault_markers" in o:
        _unique_enum_list(o["fault_markers"], f"{path}.fault_markers", T.FAULT_MARKERS, 8, d)
    if "log_excerpt" in o:
        arr = _array(o["log_excerpt"], f"{path}.log_excerpt", 5, d)
        if arr is not None:
            for i, line in enumerate(arr):
                _text(line, f"{path}.log_excerpt[{i}]", T.MAX_LOG_LINE, d)
    return d


# ---------------------------------------------------------------------------------------------
# whole case
# ---------------------------------------------------------------------------------------------
CASE_KEYS = ("schema", "case_id", "incident_family_id", "split", "synthetic", "source", "case_role",
             "looks_like", "symptom", "evidence", "expected", "cites")


def _strings(v, depth=0):
    """Yield every string value (not keys) in a validated structure; depth-capped."""
    if depth > 8:
        return
    if type(v) is str:
        yield v
    elif type(v) is dict:
        for x in v.values():
            yield from _strings(x, depth + 1)
    elif type(v) is list:
        for x in v:
            yield from _strings(x, depth + 1)


def validate_case(case):
    """Return a list of Diag for one case object (empty list = valid). Never raises."""
    d = []
    o = _object(case, "", CASE_KEYS, (), d)
    if o is None:
        return d
    if "schema" in o:
        _enum(o["schema"], "schema", (T.SCHEMA,), d)
    if "case_id" in o:
        _pattern(o["case_id"], "case_id", CASE_ID_RE, d, "case_id")
    if "incident_family_id" in o:
        _pattern(o["incident_family_id"], "incident_family_id", FAMILY_RE, d, "incident_family_id")
    if "split" in o:
        _enum(o["split"], "split", T.SPLITS, d)
    if "synthetic" in o and (type(o["synthetic"]) is not bool or o["synthetic"] is not True):
        d.append(Diag(NOT_SYNTHETIC, "synthetic", "must be the boolean true: every case is SYNTHETIC"))
    if "source" in o:
        if type(o["source"]) is not str:
            d.append(Diag(WRONG_TYPE, "source", f"expected a string, got {_tname(o['source'])}"))
        elif SOURCE_RE.fullmatch(o["source"]) is None:
            d.append(Diag(NOT_SYNTHETIC, "source", "must match 'SYNTHETIC: <printable ASCII, 1..200 chars>'"))
    role_ok = "case_role" in o and _enum(o["case_role"], "case_role", T.ROLES, d)
    ll_ok = False
    if "looks_like" in o:
        ll_ok = _enum(o["looks_like"], "looks_like", T.LABELS, d, nullable=True)
    if "symptom" in o:
        _text(o["symptom"], "symptom", T.MAX_TEXT, d)
    if "evidence" in o:
        d.extend(validate_evidence(o["evidence"]))
    label_ok = False
    sec_ok = False
    if "expected" in o:
        e = _object(o["expected"], "expected", ("label", "secondary_labels", "rationale", "safe_next_step"), (), d)
        if e is not None:
            if "label" in e:
                label_ok = _enum(e["label"], "expected.label", T.LABELS, d)
            if "secondary_labels" in e:
                arr = _array(e["secondary_labels"], "expected.secondary_labels", 3, d)
                if arr is not None:
                    sec_ok = all([_enum(x, f"expected.secondary_labels[{i}]", T.LABELS, d) for i, x in enumerate(arr)])
                    if sec_ok and len(set(arr)) != len(arr):
                        d.append(Diag(INCONSISTENT, "expected.secondary_labels", "duplicate entries"))
                        sec_ok = False
            if "rationale" in e:
                _text(e["rationale"], "expected.rationale", T.MAX_TEXT, d)
            if "safe_next_step" in e:
                if _enum(e["safe_next_step"], "expected.safe_next_step", tuple(T.NEXT_STEPS), d) and label_ok:
                    if T.LABEL_NEXT_STEP[e["label"]] != e["safe_next_step"]:
                        d.append(Diag(INCONSISTENT, "expected.safe_next_step",
                                      f"label {e['label']} uses step {T.LABEL_NEXT_STEP[e['label']]}"))
            if label_ok and sec_ok:
                sec = e["secondary_labels"]
                if e["label"] in sec:
                    d.append(Diag(INCONSISTENT, "expected.secondary_labels", "must not repeat the primary label"))
                if sec and (e["label"] == T.ABSTAIN or T.ABSTAIN in sec):
                    d.append(Diag(INCONSISTENT, "expected.secondary_labels", "an abstain case has no secondary labels and abstain is never secondary"))
                if sec and role_ok and o["case_role"] != "positive":
                    d.append(Diag(INCONSISTENT, "expected.secondary_labels", "only positive cases may list secondary labels"))
            if label_ok and role_ok:
                if o["case_role"] == "ambiguous" and e["label"] != T.ABSTAIN:
                    d.append(Diag(INCONSISTENT, "expected.label", "an ambiguous case must be labelled insufficient_evidence"))
                if o["case_role"] == "positive" and e["label"] == T.ABSTAIN:
                    d.append(Diag(INCONSISTENT, "expected.label", "a positive case cannot be labelled insufficient_evidence"))
    if role_ok and ll_ok:
        if o["case_role"] == "hard_negative":
            if o["looks_like"] is None:
                d.append(Diag(INCONSISTENT, "looks_like", "a hard_negative must name the fault it resembles"))
            elif label_ok and o["looks_like"] == o["expected"]["label"]:
                d.append(Diag(INCONSISTENT, "looks_like", "must differ from the expected label"))
        elif o["looks_like"] is not None:
            d.append(Diag(INCONSISTENT, "looks_like", "must be null unless case_role is hard_negative"))
    if "cites" in o:
        arr = _array(o["cites"], "cites", 4, d, minlen=1)
        if arr is not None:
            good = True
            for i, c in enumerate(arr):
                if type(c) is not str:
                    d.append(Diag(WRONG_TYPE, f"cites[{i}]", f"expected a string, got {_tname(c)}"))
                    good = False
                elif c not in T.CITATIONS:
                    d.append(Diag(UNKNOWN_CITATION, f"cites[{i}]", f"{c!r} is not in the citation registry"))
                    good = False
            if good and len(set(arr)) != len(arr):
                d.append(Diag(INCONSISTENT, "cites", "duplicate entries"))
    if not d:  # only scan once the structure is known to be sound (bounded depth, only strings)
        free_text = {"symptom": o["symptom"], "source": o["source"], "rationale": o["expected"]["rationale"],
                     "log_excerpt": o["evidence"].get("log_excerpt", [])}
        for s in _strings(free_text):
            for rx, what in ((IPV4_RE, "an IPv4 address"), (TOKEN_RE, "a long token-like string"),
                             (EMAIL_RE, "an e-mail address"), (URL_RE, "a URL")):
                if rx.search(s):
                    d.append(Diag(FORBIDDEN_CONTENT, "", f"a string contains {what}; cases must not carry addresses, tokens or URLs"))
                    break
    return d


# ---------------------------------------------------------------------------------------------
# loading and directories
# ---------------------------------------------------------------------------------------------
class _DupKey(Exception):
    pass


def _no_dup_pairs(pairs):
    seen = {}
    for k, v in pairs:
        if k in seen:
            raise _DupKey(k)
        seen[k] = v
    return seen


def _reject_constant(name):
    raise ValueError(f"{name} is not valid JSON for this casebook")


def parse_json_text(text):
    """Parse strict JSON text: no duplicate keys, no NaN/Infinity. Returns (obj, diags)."""
    try:
        return json.loads(text, object_pairs_hook=_no_dup_pairs, parse_constant=_reject_constant), []
    except _DupKey as exc:
        return None, [Diag(DUPLICATE_KEY, "", f"duplicate JSON key {str(exc)!r}")]
    except RecursionError:
        return None, [Diag(UNREADABLE, "", "JSON nesting is too deep")]
    except ValueError as exc:  # json.JSONDecodeError is a ValueError
        code = NON_FINITE if "not valid JSON for this casebook" in str(exc) else UNREADABLE
        return None, [Diag(code, "", f"cannot parse JSON: {exc}")]


def read_json_file(path):
    """Read one file with a size cap. Returns (obj, text, diags); never raises."""
    p = Path(path)
    try:
        if p.is_symlink() or not p.is_file():
            return None, None, [Diag(UNREADABLE, "", "not a regular file")]
        if p.stat().st_size > T.MAX_FILE_BYTES:
            return None, None, [Diag(FILE_TOO_LARGE, "", f"larger than {T.MAX_FILE_BYTES} bytes")]
        raw = p.read_bytes()
    except OSError as exc:
        return None, None, [Diag(UNREADABLE, "", f"cannot read: {exc.strerror or exc}")]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, None, [Diag(UNREADABLE, "", "not valid UTF-8")]
    obj, diags = parse_json_text(text)
    return obj, text, diags


FileDiag = namedtuple("FileDiag", "file code path message")


class DirReport:
    def __init__(self):
        self.entries_seen = 0          # every directory entry looked at (nothing is skipped silently)
        self.case_files = 0
        self.valid_cases = []          # case dicts that validated cleanly
        self.diags = []                # list of FileDiag

    @property
    def ok(self):
        return not self.diags


def validate_dir(directory):
    """Validate every case file in ``directory``. Unknown entries are reported, not ignored."""
    rep = DirReport()
    root = Path(directory)
    try:
        names = sorted(os.listdir(root))
    except OSError as exc:
        rep.diags.append(FileDiag(str(directory), UNREADABLE, "", f"cannot list directory: {exc.strerror or exc}"))
        return rep
    rep.entries_seen = len(names)
    if len(names) > T.MAX_CASES + 1:
        rep.diags.append(FileDiag(str(directory), TOO_MANY_FILES, "", f"more than {T.MAX_CASES + 1} entries"))
        return rep
    ids = {}
    fam_split = {}
    for name in names:
        if name == "MANIFEST.json":
            continue
        if FILENAME_RE.fullmatch(name) is None:
            rep.diags.append(FileDiag(name, UNEXPECTED_FILE, "", "only case_NNN.json and MANIFEST.json are allowed here"))
            continue
        rep.case_files += 1
        obj, _text_, diags = read_json_file(root / name)
        found = list(diags)
        if not diags:
            found.extend(validate_case(obj))
        for dg in found:
            rep.diags.append(FileDiag(name, dg.code, dg.path, dg.message))
        if found:
            continue
        good = True
        cid = obj["case_id"]
        if name != f"case_{cid[5:]}.json":
            rep.diags.append(FileDiag(name, BAD_FILENAME, "case_id", f"file name {name} does not match case_id {cid}"))
            good = False
        if cid in ids:
            rep.diags.append(FileDiag(name, DUPLICATE_ID, "case_id", f"also used by {ids[cid]}"))
            good = False
        else:
            ids[cid] = name
        fam = obj["incident_family_id"]
        if fam in fam_split and fam_split[fam][0] != obj["split"]:
            rep.diags.append(FileDiag(name, SPLIT_CONFLICT, "split",
                                      f"family {fam} is in split {fam_split[fam][0]} in {fam_split[fam][1]}: siblings must share a split"))
            good = False
        else:
            fam_split.setdefault(fam, (obj["split"], name))
        if good:
            rep.valid_cases.append(obj)
    return rep
