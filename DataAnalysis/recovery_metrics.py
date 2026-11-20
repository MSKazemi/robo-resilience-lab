#!/usr/bin/env python3
"""Pod-recovery metrics: pure parsing and computation (standard library only).

This module never runs kubectl and never touches a cluster. It turns an
*observation* (a JSON record of what a measurement run saw, see README.md) into
rows of a versioned schema. The live collector is ``RH.py``; the offline entry
point is the ``analyze`` command below:

    python3 DataAnalysis/recovery_metrics.py analyze \\
        --observation DataAnalysis/fixtures_recovery/success_basic.json

Three DISTINCT things are measured, and they are never merged:

1. deletion request -> replacement pod observed        (Kubernetes object exists)
2. replacement pod observed -> Ready condition observed (Kubernetes readiness)
3. disruption -> first restored ROS message             (application recovery)

(3) is reported as ``not_measured`` unless a message-probe input is supplied.
A pod ``Started`` event or ``Ready=True`` is NOT evidence that a ROS application
works again, and no value here is derived from a ``Started`` event.

Primary intervals use the *monotonic local* clock of the machine that ran the
collector. Values computed from timestamps stored by Kubernetes (creationTimestamp,
Ready lastTransitionTime, event times) or from the local wall clock are labelled
``auxiliary`` and carry the clock they come from.
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import hashlib
import io
import json
import math
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

SCHEMA_VERSION = "recovery-metrics/1"
OBSERVATION_SCHEMA = "recovery-observation/1"
PROBE_SCHEMA = "recovery-message-probe/1"

MAX_MONO_S = 1e9           # sanity bound for monotonic readings and probe values
MAX_POLLS = 100_000        # bound on polls stored in one observation
MAX_INPUT_BYTES = 64 * 1024 * 1024

EXIT_OK = 0
EXIT_MEASUREMENT_FAILED = 1
EXIT_USAGE = 2
EXIT_KUBECTL = 3
EXIT_TIMEOUT = 4
EXIT_IO = 5
EXIT_INTERRUPTED = 130


# --------------------------------------------------------------------------- errors
class RecoveryError(Exception):
    """Base class. ``code`` is a stable machine-readable reason."""

    code = "recovery_error"
    exit_code = EXIT_MEASUREMENT_FAILED


class MalformedInputError(RecoveryError):
    code = "malformed_input"
    exit_code = EXIT_USAGE


class DuplicateRecordError(MalformedInputError):
    code = "duplicate_record"


class TimestampError(RecoveryError):
    code = "bad_timestamp"


class MissingTimestampError(RecoveryError):
    code = "missing_timestamp"


class TargetPodNotFoundError(RecoveryError):
    code = "target_pod_not_found"


class NoReplacementPodError(RecoveryError):
    code = "no_replacement_pod"


class AmbiguousMatchError(RecoveryError):
    code = "ambiguous_match"


class ReplacementDisappearedError(RecoveryError):
    code = "replacement_pod_disappeared"


class NoReadyConditionError(RecoveryError):
    code = "no_ready_condition"


class ReadyNotObservedError(RecoveryError):
    code = "ready_not_observed"


class MissingEventError(RecoveryError):
    code = "scheduled_event_missing"


class PollTimeoutError(RecoveryError):
    code = "poll_timeout"
    exit_code = EXIT_TIMEOUT


class KubectlError(RecoveryError):
    code = "kubectl_failed"
    exit_code = EXIT_KUBECTL


class GuardError(RecoveryError):
    """A safety rule refused the operation (nothing was changed)."""

    code = "guard_refused"
    exit_code = EXIT_USAGE


# --------------------------------------------------------------------------- strict JSON
def _reject_constant(name: str) -> Any:
    raise MalformedInputError(f"non-finite JSON constant {name} is not allowed")


def _parse_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise MalformedInputError("non-finite JSON number is not allowed")
    return value


def _no_duplicate_keys(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise DuplicateRecordError(f"duplicate JSON key {_short(key)}")
        out[key] = value
    return out


def load_json_strict(data: Any) -> Any:
    """json.loads that rejects duplicate keys, NaN/Infinity and non-JSON input."""
    if not isinstance(data, (str, bytes, bytearray)):
        raise MalformedInputError(f"expected JSON text, got {type(data).__name__}")
    try:
        return json.loads(
            data,
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=_reject_constant,
            parse_float=_parse_float,
        )
    except RecoveryError:
        raise
    except (ValueError, RecursionError) as exc:
        raise MalformedInputError(f"invalid JSON: {_short(str(exc))}") from None


def _clean(text: str, limit: int) -> str:
    text = "".join(ch if 32 <= ord(ch) < 127 else "?" for ch in text)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _short(value: Any, limit: int = 80) -> str:
    """Bounded, printable-ASCII repr of untrusted input for error messages."""
    return _clean(repr(value), limit)


# --------------------------------------------------------------------------- scalars
_TS_RE = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})"
    r"(?:\.([0-9]{1,9}))?(Z|[+-][0-9]{2}:[0-9]{2})"
)


def parse_k8s_timestamp(value: Any, field: str = "timestamp") -> datetime:
    """Parse an RFC 3339 timestamp with an EXPLICIT offset into an aware datetime.

    Accepted: ``2026-01-01T12:00:02Z``, ``...+02:00``, ``...-05:30``, optional
    fraction of 1-9 digits (digits beyond microseconds are truncated). Rejected:
    naive timestamps, non-strings, trailing characters, ``-00:00``, offsets beyond
    23:59, impossible dates. The parsed offset is preserved (never replaced).
    """
    if not isinstance(value, str):
        raise TimestampError(f"{field}: expected a string, got {type(value).__name__}")
    match = _TS_RE.fullmatch(value)
    if match is None:
        raise TimestampError(
            f"{field}: not an RFC 3339 timestamp with an explicit Z or +hh:mm offset: {_short(value)}"
        )
    year, month, day, hour, minute, second, frac, offset = match.groups()
    micro = int((frac or "0").ljust(6, "0")[:6])
    if offset == "Z":
        tz = timezone.utc
    else:
        if offset == "-00:00":
            raise TimestampError(f"{field}: offset -00:00 (unknown local offset) is rejected")
        off_h, off_m = int(offset[1:3]), int(offset[4:6])
        if off_h > 23 or off_m > 59:
            raise TimestampError(f"{field}: offset out of range: {_short(value)}")
        delta = timedelta(hours=off_h, minutes=off_m)
        tz = timezone(delta if offset[0] == "+" else -delta)
    try:
        return datetime(
            int(year), int(month), int(day), int(hour), int(minute), int(second), micro, tzinfo=tz
        )
    except ValueError:
        raise TimestampError(f"{field}: not a valid date/time: {_short(value)}") from None


def require_seconds(value: Any, field: str, *, maximum: float = MAX_MONO_S) -> float:
    """A finite, non-negative real number (bool, NaN, inf, negative, huge are rejected)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MalformedInputError(f"{field}: expected a number, got {type(value).__name__}")
    number = float(value)
    if not math.isfinite(number) or number < 0 or number > maximum:
        raise MalformedInputError(f"{field}: must be finite, >= 0 and <= {maximum:g}: {_short(value)}")
    return number


_NAME_RE = re.compile(r"[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?")        # DNS-1123 subdomain
_NAMESPACE_RE = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?")     # DNS-1123 label
_UID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}")
_SAMPLE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_LABEL_KEY_RE = re.compile(
    r"([A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?/)?[A-Za-z0-9]([A-Za-z0-9._-]{0,61}[A-Za-z0-9])?"
)
_LABEL_VALUE_RE = re.compile(r"[A-Za-z0-9]([A-Za-z0-9._-]{0,61}[A-Za-z0-9])?")
_CONTEXT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9@._:/-]{0,252}")
_EVIDENCE_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/#=:+-]{0,255}")


def _require_pattern(value: Any, pattern: "re.Pattern[str]", field: str) -> str:
    if not isinstance(value, str):
        raise MalformedInputError(f"{field}: expected a string, got {type(value).__name__}")
    if pattern.fullmatch(value) is None:
        raise MalformedInputError(f"{field}: invalid value {_short(value)}")
    return value


def validate_namespace(value: Any) -> str:
    return _require_pattern(value, _NAMESPACE_RE, "namespace")


def validate_sample_id(value: Any) -> str:
    return _require_pattern(value, _SAMPLE_ID_RE, "sample_id")


def validate_context(value: Any) -> str:
    return _require_pattern(value, _CONTEXT_RE, "context")


def validate_pod_name(value: Any, field: str = "pod name") -> str:
    return _require_pattern(value, _NAME_RE, field)


def validate_evidence_ref(value: Any) -> str:
    return _require_pattern(value, _EVIDENCE_REF_RE, "evidence_ref")


def parse_selector(text: Any) -> Dict[str, str]:
    """Equality-only label selector ``key=value[,key=value...]`` -> dict.

    Set-based/negated selectors, empty values, duplicate keys and whitespace are
    rejected so that a selector always means exactly what it says.
    """
    if not isinstance(text, str) or not text:
        raise MalformedInputError("selector: a non-empty string is required")
    out: Dict[str, str] = {}
    for part in text.split(","):
        key, sep, value = part.partition("=")
        if not sep or "=" in value:
            raise MalformedInputError(f"selector: expected key=value, got {_short(part)}")
        if _LABEL_KEY_RE.fullmatch(key) is None or _LABEL_VALUE_RE.fullmatch(value) is None:
            raise MalformedInputError(f"selector: invalid key or value in {_short(part)}")
        if key in out:
            raise MalformedInputError(f"selector: duplicate key {_short(key)}")
        out[key] = value
    return out


def format_selector(selector: Mapping[str, str]) -> str:
    return ",".join(f"{k}={v}" for k, v in selector.items())


# --------------------------------------------------------------------------- pods / events
def _dict(value: Any, field: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise MalformedInputError(f"{field}: expected an object, got {type(value).__name__}")
    return value


def _list(value: Any, field: str) -> List[Any]:
    if not isinstance(value, list):
        raise MalformedInputError(f"{field}: expected a list, got {type(value).__name__}")
    return value


def _opt_str(container: Mapping[str, Any], key: str, field: str) -> Optional[str]:
    value = container.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise MalformedInputError(f"{field}.{key}: expected a string, got {type(value).__name__}")
    return value


@dataclass(frozen=True)
class ReadyCondition:
    status: str                       # "True" | "False" | "Unknown"
    last_transition_raw: Optional[str]


@dataclass(frozen=True)
class PodView:
    name: str
    namespace: str
    uid: str
    labels: Tuple[Tuple[str, str], ...]
    controller_uid: Optional[str]
    phase: Optional[str]
    creation_raw: Optional[str]
    deletion_raw: Optional[str]
    ready: Optional[ReadyCondition]

    @property
    def terminating(self) -> bool:
        return self.deletion_raw is not None

    @property
    def is_ready(self) -> bool:
        return self.ready is not None and self.ready.status == "True"

    def to_record(self) -> Dict[str, Any]:
        """Minimal sanitized record (re-parsable). Spec, annotations, env, messages are dropped."""
        meta: Dict[str, Any] = {
            "name": self.name,
            "namespace": self.namespace,
            "uid": self.uid,
            "labels": dict(self.labels),
        }
        if self.controller_uid is not None:
            meta["ownerReferences"] = [{"uid": self.controller_uid, "controller": True}]
        if self.creation_raw is not None:
            meta["creationTimestamp"] = self.creation_raw
        if self.deletion_raw is not None:
            meta["deletionTimestamp"] = self.deletion_raw
        status: Dict[str, Any] = {}
        if self.phase is not None:
            status["phase"] = self.phase
        if self.ready is not None:
            cond: Dict[str, Any] = {"type": "Ready", "status": self.ready.status}
            if self.ready.last_transition_raw is not None:
                cond["lastTransitionTime"] = self.ready.last_transition_raw
            status["conditions"] = [cond]
        return {"metadata": meta, "status": status}


def parse_pod(item: Any, where: str = "pod") -> PodView:
    pod = _dict(item, where)
    meta = _dict(pod.get("metadata"), f"{where}.metadata")
    name = validate_pod_name(meta.get("name"), f"{where}.metadata.name")
    namespace = _require_pattern(meta.get("namespace"), _NAMESPACE_RE, f"{where}.metadata.namespace")
    uid = _require_pattern(meta.get("uid"), _UID_RE, f"{where}.metadata.uid")

    labels_raw = meta.get("labels")
    labels: List[Tuple[str, str]] = []
    if labels_raw is not None:
        for key, value in _dict(labels_raw, f"{where}.metadata.labels").items():
            if not isinstance(value, str):
                raise MalformedInputError(f"{where}.metadata.labels[{_short(key)}]: expected a string")
            labels.append((key, value))
    labels.sort()

    controller_uid: Optional[str] = None
    owners = meta.get("ownerReferences")
    if owners is not None:
        for i, owner_raw in enumerate(_list(owners, f"{where}.metadata.ownerReferences")):
            owner = _dict(owner_raw, f"{where}.metadata.ownerReferences[{i}]")
            flag = owner.get("controller")
            if flag is not None and not isinstance(flag, bool):
                raise MalformedInputError(f"{where}.metadata.ownerReferences[{i}].controller: expected bool")
            if flag is True:
                if controller_uid is not None:
                    raise DuplicateRecordError(f"{where}: more than one controller ownerReference")
                controller_uid = _require_pattern(
                    owner.get("uid"), _UID_RE, f"{where}.metadata.ownerReferences[{i}].uid"
                )

    status = pod.get("status")
    status = {} if status is None else _dict(status, f"{where}.status")
    phase = _opt_str(status, "phase", f"{where}.status")

    ready: Optional[ReadyCondition] = None
    conditions = status.get("conditions")
    if conditions is not None:
        for i, cond_raw in enumerate(_list(conditions, f"{where}.status.conditions")):
            cond = _dict(cond_raw, f"{where}.status.conditions[{i}]")
            ctype = cond.get("type")
            cstatus = cond.get("status")
            if not isinstance(ctype, str) or not isinstance(cstatus, str):
                raise MalformedInputError(f"{where}.status.conditions[{i}]: type and status must be strings")
            transition = _opt_str(cond, "lastTransitionTime", f"{where}.status.conditions[{i}]")
            if ctype == "Ready":
                if ready is not None:
                    raise DuplicateRecordError(f"{where}: more than one Ready condition")
                if cstatus not in ("True", "False", "Unknown"):
                    raise MalformedInputError(f"{where}: Ready status must be True/False/Unknown: {_short(cstatus)}")
                ready = ReadyCondition(cstatus, transition)

    return PodView(
        name=name,
        namespace=namespace,
        uid=uid,
        labels=tuple(labels),
        controller_uid=controller_uid,
        phase=phase,
        creation_raw=_opt_str(meta, "creationTimestamp", f"{where}.metadata"),
        deletion_raw=_opt_str(meta, "deletionTimestamp", f"{where}.metadata"),
        ready=ready,
    )


def parse_pod_list(obj: Any, where: str = "pod list") -> List[PodView]:
    """Parse ``kubectl get pods -o json`` (or a sanitized record). Duplicate pods are rejected."""
    items = _list(_dict(obj, where).get("items"), f"{where}.items")
    views = [parse_pod(item, f"{where}.items[{i}]") for i, item in enumerate(items)]
    seen_uid: set = set()
    seen_name: set = set()
    for view in views:
        if view.uid in seen_uid:
            raise DuplicateRecordError(f"{where}: duplicate pod uid {view.uid}")
        if (view.namespace, view.name) in seen_name:
            raise DuplicateRecordError(f"{where}: duplicate pod name {view.namespace}/{view.name}")
        seen_uid.add(view.uid)
        seen_name.add((view.namespace, view.name))
    return views


def sanitize_pod_list(obj: Any, where: str = "pod list") -> Dict[str, Any]:
    return {"items": [v.to_record() for v in parse_pod_list(obj, where)]}


def select_pods(
    views: Sequence[PodView],
    namespace: str,
    selector: Mapping[str, str],
    owner_uid: Optional[str] = None,
) -> List[PodView]:
    """Pods in ``namespace`` whose labels contain every selector pair (and the owner, if given)."""
    out = []
    for view in views:
        if view.namespace != namespace:
            continue
        labels = dict(view.labels)
        if any(labels.get(k) != v for k, v in selector.items()):
            continue
        if owner_uid is not None and view.controller_uid != owner_uid:
            continue
        out.append(view)
    return out


@dataclass(frozen=True)
class EventView:
    kind: Optional[str]
    pod_uid: Optional[str]
    reason: str
    time_raw: Optional[str]


def parse_event_list(obj: Any, where: str = "event list") -> List[EventView]:
    items = _list(_dict(obj, where).get("items"), f"{where}.items")
    out: List[EventView] = []
    for i, raw in enumerate(items):
        w = f"{where}.items[{i}]"
        ev = _dict(raw, w)
        involved = _dict(ev.get("involvedObject"), f"{w}.involvedObject")
        uid = involved.get("uid")
        if uid is not None:
            uid = _require_pattern(uid, _UID_RE, f"{w}.involvedObject.uid")
        reason = ev.get("reason")
        if not isinstance(reason, str):
            raise MalformedInputError(f"{w}.reason: expected a string")
        time_raw = None
        for key in ("eventTime", "firstTimestamp", "lastTimestamp"):
            candidate = _opt_str(ev, key, w)
            if candidate is not None:
                time_raw = candidate
                break
        out.append(EventView(_opt_str(involved, "kind", f"{w}.involvedObject"), uid, reason, time_raw))
    return out


def sanitize_event_list(obj: Any, where: str = "event list") -> Dict[str, Any]:
    items = []
    for ev in parse_event_list(obj, where):
        rec: Dict[str, Any] = {"involvedObject": {}, "reason": ev.reason}
        if ev.kind is not None:
            rec["involvedObject"]["kind"] = ev.kind
        if ev.pod_uid is not None:
            rec["involvedObject"]["uid"] = ev.pod_uid
        if ev.time_raw is not None:
            rec["eventTime"] = ev.time_raw
        items.append(rec)
    return {"items": items}


# --------------------------------------------------------------------------- replacement logic
@dataclass(frozen=True)
class Replacement:
    uid: str
    first_poll_index: int


def new_pod_candidates(
    baseline_uids: "set[str]", views: Sequence[PodView], target: PodView
) -> List[str]:
    """UIDs in ``views`` that could be the replacement of ``target`` (not in the baseline snapshot,
    and with the same controller owner UID when the target has one)."""
    out = []
    for view in views:
        if view.uid in baseline_uids:
            continue
        if target.controller_uid is not None and view.controller_uid != target.controller_uid:
            continue
        out.append(view.uid)
    return out


def find_replacement(
    baseline: Sequence[PodView],
    polls: Sequence[Sequence[PodView]],
    target: PodView,
) -> Replacement:
    """Identify THE replacement pod by UID.

    A replacement is a selected pod whose UID was not in the baseline snapshot and
    (if the target has a controller owner) that has the same controller owner UID.
    The old pod (same or different name, possibly still Terminating) is excluded by
    UID. Zero candidates -> NoReplacementPodError. More than one distinct candidate
    over all polls -> AmbiguousMatchError (we do not guess which one it is).
    """
    baseline_uids = {v.uid for v in baseline}
    found: Dict[str, int] = {}
    for index, views in enumerate(polls):
        for uid in new_pod_candidates(baseline_uids, views, target):
            found.setdefault(uid, index)
    if not found:
        raise NoReplacementPodError(
            f"no pod with a new UID matching the selector appeared in {len(polls)} poll(s)"
        )
    if len(found) > 1:
        uids = ", ".join(sorted(found)[:5])
        raise AmbiguousMatchError(f"{len(found)} distinct new pods match the selector ({uids}); refusing to guess")
    (uid, index), = found.items()
    return Replacement(uid, index)


# --------------------------------------------------------------------------- measurements
@dataclass(frozen=True)
class Outcome:
    value: Optional[float]
    warnings: Tuple[str, ...] = ()


def _interval(start: datetime, end: datetime, warning_prefix: str) -> Outcome:
    seconds = (end - start).total_seconds()
    if seconds < 0:
        # Skewed clocks or 1 s timestamp truncation: degrade to null + warning, never a negative duration.
        return Outcome(None, (f"{warning_prefix}_negative_interval",))
    return Outcome(round(seconds, 6))


@dataclass(frozen=True)
class Observation:
    sample_id: str
    synthetic: bool
    label: Optional[str]
    namespace: str
    selector: Dict[str, str]
    owner_uid: Optional[str]
    context: Optional[str]
    baseline: List[PodView]
    target_uid: str
    target_name: Optional[str]
    delete_mono: float
    delete_wall: Optional[datetime]
    delete_wall_raw: Optional[str]
    poll_times: List[float]
    polls: List[List[PodView]]
    events: Optional[List[EventView]]
    exit_status: int
    run_error_code: Optional[str]
    probe: Optional[Dict[str, Any]]


_OBS_KEYS = {
    "schema", "sample_id", "synthetic", "label", "namespace", "selector", "owner_uid",
    "kubectl_context", "baseline_pods", "target_uid", "target_pod", "delete_requested_mono_s",
    "delete_request_wall", "polls", "events", "exit_status", "run_error", "probe", "tool",
}


def parse_observation(obj: Any) -> Observation:
    """Strictly validate an observation record (corrupt input raises MalformedInputError)."""
    obs = _dict(obj, "observation")
    unknown = sorted(set(obs) - _OBS_KEYS)
    if unknown:
        raise MalformedInputError(f"observation: unknown key(s) {_short(unknown)}")
    if obs.get("schema") != OBSERVATION_SCHEMA:
        raise MalformedInputError(f"observation.schema must be {OBSERVATION_SCHEMA!r}")
    sample_id = validate_sample_id(obs.get("sample_id"))
    synthetic = obs.get("synthetic", False)
    if not isinstance(synthetic, bool):
        raise MalformedInputError("observation.synthetic: expected bool")
    label = obs.get("label")
    if label is not None and not isinstance(label, str):
        raise MalformedInputError("observation.label: expected a string")
    namespace = validate_namespace(obs.get("namespace"))
    selector = parse_selector(obs.get("selector"))
    owner_uid = obs.get("owner_uid")
    if owner_uid is not None:
        owner_uid = _require_pattern(owner_uid, _UID_RE, "observation.owner_uid")
    context = obs.get("kubectl_context")
    if context is not None:
        context = validate_context(context)
    target_uid = _require_pattern(obs.get("target_uid"), _UID_RE, "observation.target_uid")
    target_name = obs.get("target_pod")
    if target_name is not None:
        target_name = validate_pod_name(target_name, "observation.target_pod")

    baseline = select_pods(
        parse_pod_list(obs.get("baseline_pods"), "baseline_pods"), namespace, selector, owner_uid
    )
    delete_mono = require_seconds(obs.get("delete_requested_mono_s"), "delete_requested_mono_s")
    wall_raw = obs.get("delete_request_wall")
    wall = None
    if wall_raw is not None:
        try:
            wall = parse_k8s_timestamp(wall_raw, "delete_request_wall")
        except TimestampError as exc:
            raise MalformedInputError(str(exc)) from None

    polls_raw = _list(obs.get("polls"), "polls")
    if len(polls_raw) > MAX_POLLS:
        raise MalformedInputError(f"polls: more than {MAX_POLLS} entries")
    poll_times: List[float] = []
    polls: List[List[PodView]] = []
    last = delete_mono
    for i, raw in enumerate(polls_raw):
        entry = _dict(raw, f"polls[{i}]")
        if set(entry) != {"mono_s", "pods"}:
            raise MalformedInputError(f"polls[{i}]: keys must be exactly mono_s and pods")
        t = require_seconds(entry["mono_s"], f"polls[{i}].mono_s")
        if t < last:
            raise MalformedInputError(f"polls[{i}].mono_s goes backwards or precedes the delete request")
        last = t
        poll_times.append(t)
        polls.append(select_pods(parse_pod_list(entry["pods"], f"polls[{i}].pods"), namespace, selector, owner_uid))

    events_raw = obs.get("events")
    events = None if events_raw is None else parse_event_list(events_raw, "events")

    exit_status = obs.get("exit_status")
    if isinstance(exit_status, bool) or not isinstance(exit_status, int) or not 0 <= exit_status <= 255:
        raise MalformedInputError("observation.exit_status: expected an integer 0..255")

    run_error = obs.get("run_error")
    run_error_code = None
    if run_error is not None:
        run_error = _dict(run_error, "observation.run_error")
        code = run_error.get("code")
        if not isinstance(code, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) is None:
            raise MalformedInputError("observation.run_error.code: expected a snake_case string")
        run_error_code = code

    probe = obs.get("probe")
    if probe is not None:
        probe = _dict(probe, "observation.probe")

    return Observation(
        sample_id, synthetic, label, namespace, selector, owner_uid, context, baseline, target_uid,
        target_name, delete_mono, wall, wall_raw, poll_times, polls, events, exit_status,
        run_error_code, probe,
    )


def _find_target(obs: Observation) -> PodView:
    matches = [v for v in obs.baseline if v.uid == obs.target_uid]
    if not matches:
        raise TargetPodNotFoundError("target pod UID is not in the baseline snapshot")
    return matches[0]


def _replacement_view(obs: Observation, index: int, uid: str) -> Optional[PodView]:
    for view in obs.polls[index]:
        if view.uid == uid:
            return view
    return None


def measure_replacement_observed(obs: Observation, replacement: Replacement) -> Outcome:
    """PRIMARY (monotonic local): delete request issued -> first poll that shows the replacement."""
    seconds = obs.poll_times[replacement.first_poll_index] - obs.delete_mono
    return Outcome(round(seconds, 6))


def _first_ready(obs: Observation, replacement: Replacement) -> Tuple[int, PodView]:
    saw_condition = False
    for index in range(replacement.first_poll_index, len(obs.polls)):
        view = _replacement_view(obs, index, replacement.uid)
        if view is None:
            raise ReplacementDisappearedError(
                f"replacement pod vanished from poll {index} before Ready=True was observed"
            )
        if view.ready is not None:
            saw_condition = True
        if view.is_ready:
            return index, view
    if not saw_condition:
        raise NoReadyConditionError("the replacement pod never reported a Ready condition")
    raise ReadyNotObservedError("the replacement pod was never observed with Ready=True")


def measure_ready_observed(obs: Observation, replacement: Replacement) -> Outcome:
    """PRIMARY (monotonic local): replacement first observed -> first poll with Ready=True.

    Resolution is one poll interval. ``0`` means it was already Ready at first sight.
    """
    index, _ = _first_ready(obs, replacement)
    seconds = obs.poll_times[index] - obs.poll_times[replacement.first_poll_index]
    warnings = ("ready_at_first_observation",) if index == replacement.first_poll_index else ()
    return Outcome(round(seconds, 6), warnings)


def measure_wallclock_creation(obs: Observation, replacement: Replacement) -> Outcome:
    """AUXILIARY: replacement creationTimestamp (API server) minus local wall-clock delete request."""
    if obs.delete_wall is None:
        raise _NotMeasured("no_wall_clock_request_time", "the observation has no delete_request_wall")
    view = _replacement_view(obs, replacement.first_poll_index, replacement.uid)
    if view is None or view.creation_raw is None:
        raise MissingTimestampError("the replacement pod has no creationTimestamp")
    created = parse_k8s_timestamp(view.creation_raw, "replacement creationTimestamp")
    return _interval(obs.delete_wall, created, "k8s_creation")


def measure_creation_to_ready_transition(obs: Observation, replacement: Replacement) -> Outcome:
    """AUXILIARY: replacement creationTimestamp -> Ready lastTransitionTime (both server clock)."""
    _, ready_view = _first_ready(obs, replacement)
    first_view = _replacement_view(obs, replacement.first_poll_index, replacement.uid)
    created_raw = (first_view or ready_view).creation_raw
    if created_raw is None:
        raise MissingTimestampError("the replacement pod has no creationTimestamp")
    if ready_view.ready is None or ready_view.ready.last_transition_raw is None:
        raise MissingTimestampError("the Ready condition has no lastTransitionTime")
    created = parse_k8s_timestamp(created_raw, "replacement creationTimestamp")
    ready = parse_k8s_timestamp(ready_view.ready.last_transition_raw, "Ready lastTransitionTime")
    return _interval(created, ready, "k8s_ready")


def measure_creation_to_scheduled_event(obs: Observation, replacement: Replacement) -> Outcome:
    """AUXILIARY: replacement creationTimestamp -> its ``Scheduled`` event time (scheduling only)."""
    if obs.events is None:
        raise _NotMeasured("events_not_collected", "the run did not collect events")
    view = _replacement_view(obs, replacement.first_poll_index, replacement.uid)
    if view is None or view.creation_raw is None:
        raise MissingTimestampError("the replacement pod has no creationTimestamp")
    matches = [
        e for e in obs.events
        if e.reason == "Scheduled" and e.pod_uid == replacement.uid and e.kind in (None, "Pod")
    ]
    if not matches:
        raise MissingEventError("no Scheduled event for the replacement pod UID")
    if len(matches) > 1:
        raise AmbiguousMatchError(f"{len(matches)} Scheduled events for the replacement pod UID")
    if matches[0].time_raw is None:
        raise MissingTimestampError("the Scheduled event has no timestamp")
    created = parse_k8s_timestamp(view.creation_raw, "replacement creationTimestamp")
    when = parse_k8s_timestamp(matches[0].time_raw, "Scheduled event time")
    return _interval(created, when, "k8s_scheduled")


def measure_message_recovery(probe: Optional[Any]) -> Outcome:
    """PLACEHOLDER for application-level recovery (disruption -> first restored ROS message).

    Without a probe input this raises ``_NotMeasured``. A probe is
    ``{"schema": "recovery-message-probe/1", "source": str, "disruption_started_s": number,
    "first_restored_message_s": number}`` with both numbers on the PROBE's own clock.
    The pod lifecycle never substitutes for it. This repository does not yet contain a
    producer of such a probe; the contract is exercised with SYNTHETIC input only.
    """
    if probe is None:
        raise _NotMeasured("no_message_probe", "no message-probe input was supplied")
    probe = _dict(probe, "probe")
    if set(probe) != {"schema", "source", "disruption_started_s", "first_restored_message_s"}:
        raise MalformedInputError("probe: keys must be schema, source, disruption_started_s, first_restored_message_s")
    if probe["schema"] != PROBE_SCHEMA:
        raise MalformedInputError(f"probe.schema must be {PROBE_SCHEMA!r}")
    if not isinstance(probe["source"], str) or not probe["source"]:
        raise MalformedInputError("probe.source: a non-empty string describing the probe is required")
    start = require_seconds(probe["disruption_started_s"], "probe.disruption_started_s")
    restored = probe["first_restored_message_s"]
    if restored is None:
        raise _NotMeasured("no_message_observed", "the probe reported no message after the disruption")
    restored = require_seconds(restored, "probe.first_restored_message_s")
    if restored < start:
        raise MalformedInputError("probe: first_restored_message_s precedes disruption_started_s")
    return Outcome(round(restored - start, 6))


class _NotMeasured(Exception):
    """Internal: a legitimately absent input (status ``not_measured``), not a failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- rows / schema
MEASUREMENTS: Tuple[Tuple[str, str, str, str], ...] = (
    (
        "deletion_request_to_replacement_pod_observed", "primary", "monotonic_local",
        "Monotonic seconds from issuing the pod delete request to the first poll that lists a pod "
        "with a new UID matching namespace+selector. Kubernetes object existence only; upper bound "
        "with one poll interval of resolution; includes kubectl start-up and API latency.",
    ),
    (
        "replacement_pod_observed_to_ready_observed", "primary", "monotonic_local",
        "Monotonic seconds from the first poll that lists the replacement pod to the first poll that "
        "shows its Ready condition True. Kubernetes readiness only, NOT ROS application recovery; "
        "resolution of one poll interval (0 = already Ready when first seen).",
    ),
    (
        "aux_wallclock_request_to_replacement_creation_timestamp", "auxiliary", "local_wall_clock_vs_kubernetes_server",
        "Replacement metadata.creationTimestamp (API-server clock, 1 s resolution) minus the local "
        "wall-clock time of the delete request. Affected by clock skew; null if negative.",
    ),
    (
        "aux_replacement_creation_to_ready_transition", "auxiliary", "kubernetes_server_timestamps",
        "Ready condition lastTransitionTime minus replacement creationTimestamp, both Kubernetes "
        "timestamps with 1 s resolution. Auxiliary; readiness only; null if negative.",
    ),
    (
        "aux_replacement_creation_to_scheduled_event", "auxiliary", "kubernetes_server_timestamps",
        "Time of the replacement pod's Scheduled event minus its creationTimestamp. Scheduling only. "
        "Events are best-effort and may be missing. No Started event is used anywhere.",
    ),
    (
        "disruption_to_first_restored_ros_message", "application", "probe_supplied",
        "Seconds from disruption to the first ROS message restored, taken from a supplied message "
        "probe on the probe's own clock. NOT MEASURED unless a probe input is supplied.",
    ),
)

COLUMNS: Tuple[str, ...] = (
    "schema_version", "sample_id", "synthetic", "measurement", "role", "definition", "clock", "unit",
    "status", "value", "error_code", "error_message", "warnings", "exit_status", "run_error_code",
    "namespace", "selector", "target_pod", "target_uid", "replacement_pod", "replacement_uid",
    "owner_uid", "evidence_ref",
)

_MEASURE_FUNCS = {
    "deletion_request_to_replacement_pod_observed": measure_replacement_observed,
    "replacement_pod_observed_to_ready_observed": measure_ready_observed,
    "aux_wallclock_request_to_replacement_creation_timestamp": measure_wallclock_creation,
    "aux_replacement_creation_to_ready_transition": measure_creation_to_ready_transition,
    "aux_replacement_creation_to_scheduled_event": measure_creation_to_scheduled_event,
}


def analyze_observation(obj: Any, *, evidence_ref: str) -> List[Dict[str, Any]]:
    """Compute every measurement row for one observation.

    Corrupt input raises ``MalformedInputError``. A measurement that cannot be computed
    gets ``status`` ``failed`` (typed ``error_code``) or ``not_measured`` and ``value``
    ``None``; it never carries a fabricated duration.
    """
    validate_evidence_ref(evidence_ref)
    obs = parse_observation(obj)

    target: Optional[PodView] = None
    replacement: Optional[Replacement] = None
    shared_error: Optional[RecoveryError] = None
    try:
        target = _find_target(obs)
        replacement = find_replacement(obs.baseline, obs.polls, target)
    except RecoveryError as exc:
        shared_error = exc

    repl_name = None
    if replacement is not None:
        view = _replacement_view(obs, replacement.first_poll_index, replacement.uid)
        repl_name = view.name if view is not None else None

    rows: List[Dict[str, Any]] = []
    for name, role, clock, definition in MEASUREMENTS:
        status, value, code, message, warnings = "measured", None, None, None, ()
        try:
            if name == "disruption_to_first_restored_ros_message":
                outcome = measure_message_recovery(obs.probe)
            elif shared_error is not None:
                raise shared_error
            else:
                assert replacement is not None
                outcome = _MEASURE_FUNCS[name](obs, replacement)
            value, warnings = outcome.value, outcome.warnings
            if value is None:
                status, code = "not_measured", "value_not_computable"
        except _NotMeasured as exc:
            status, code, message = "not_measured", exc.code, str(exc)
        except RecoveryError as exc:
            status, code, message = "failed", exc.code, str(exc)
        rows.append({
            "schema_version": SCHEMA_VERSION,
            "sample_id": obs.sample_id,
            "synthetic": obs.synthetic,
            "measurement": name,
            "role": role,
            "definition": definition,
            "clock": clock,
            "unit": "s",
            "status": status,
            "value": value,
            "error_code": code,
            "error_message": None if message is None else _clean(message, 240),
            "warnings": list(warnings),
            "exit_status": obs.exit_status,
            "run_error_code": obs.run_error_code,
            "namespace": obs.namespace,
            "selector": format_selector(obs.selector),
            "target_pod": target.name if target else obs.target_name,
            "target_uid": obs.target_uid,
            "replacement_pod": repl_name,
            "replacement_uid": replacement.uid if replacement else None,
            "owner_uid": target.controller_uid if target else obs.owner_uid,
            "evidence_ref": evidence_ref,
        })
    return rows


def primaries_ok(rows: Sequence[Mapping[str, Any]]) -> bool:
    """True iff both primary Kubernetes measurements were measured."""
    primary = [r for r in rows if r["role"] == "primary"]
    return len(primary) == 2 and all(r["status"] == "measured" for r in primary)


# --------------------------------------------------------------------------- serialization
def _csv_safe(text: str) -> str:
    """Neutralise spreadsheet formula injection in free-text cells."""
    return "'" + text if text and text[0] in "=+-@\t\r" else text


def _csv_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.6f}"
    if isinstance(value, list):
        return _csv_safe(";".join(str(v) for v in value))
    return _csv_safe(str(value))


def rows_to_csv(rows: Sequence[Mapping[str, Any]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(COLUMNS)
    for row in rows:
        writer.writerow([_csv_cell(row[c]) for c in COLUMNS])
    return buf.getvalue()


def rows_to_json(rows: Sequence[Mapping[str, Any]]) -> str:
    doc = {
        "schema_version": SCHEMA_VERSION,
        "columns": list(COLUMNS),
        "rows": [{c: row[c] for c in COLUMNS} for row in rows],
    }
    return json.dumps(doc, indent=2, allow_nan=False) + "\n"


def evidence_ref_for(file_name: str, data: bytes) -> str:
    ref = f"{file_name}#sha256={hashlib.sha256(data).hexdigest()}"
    return validate_evidence_ref(ref)


# --------------------------------------------------------------------------- CLI (offline)
def _read_input(path: Path) -> bytes:
    try:
        with open(path, "rb") as fh:
            data = fh.read(MAX_INPUT_BYTES + 1)
    except OSError as exc:
        raise MalformedInputError(f"cannot read {path}: {exc.strerror}") from None
    if len(data) > MAX_INPUT_BYTES:
        raise MalformedInputError(f"{path}: larger than {MAX_INPUT_BYTES} bytes")
    return data


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="recovery_metrics.py",
        allow_abbrev=False,
        description="Offline analysis of a recovery observation record (no kubectl, no cluster).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    an = sub.add_parser("analyze", help="compute measurement rows from an observation JSON file")
    an.add_argument("--observation", required=True, type=Path, help="observation JSON (see README)")
    an.add_argument("--format", choices=("csv", "json"), default="csv", help="output format (default: csv)")
    an.add_argument("--output", type=Path, default=None, help="write here (refuses to overwrite); default stdout")
    an.add_argument("--evidence-ref", default=None, help="override the raw-evidence reference in every row")
    return parser


def main(argv: Optional[Sequence[str]] = None, *, stdout=None, stderr=None) -> int:
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    parser = build_parser()
    try:
        with contextlib.redirect_stderr(stderr):
            args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    try:
        data = _read_input(args.observation)
        ref = args.evidence_ref or evidence_ref_for(args.observation.name, data)
        rows = analyze_observation(load_json_strict(data), evidence_ref=ref)
        text = rows_to_csv(rows) if args.format == "csv" else rows_to_json(rows)
        if args.output is None:
            stdout.write(text)
        else:
            try:
                with open(args.output, "x", encoding="utf-8", newline="") as fh:
                    fh.write(text)
            except OSError as exc:
                raise MalformedInputError(f"cannot write {args.output}: {exc.strerror}") from None
    except RecoveryError as exc:
        print(f"error [{exc.code}]: {exc}", file=stderr)
        return exc.exit_code
    bad = [r for r in rows if r["role"] != "application" and r["status"] == "failed"]
    for row in bad:
        print(f"measurement failed: {row['measurement']} [{row['error_code']}]", file=stderr)
    return EXIT_OK if primaries_ok(rows) else EXIT_MEASUREMENT_FAILED


if __name__ == "__main__":
    sys.exit(main())
