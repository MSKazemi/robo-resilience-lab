"""Deterministic, fail-closed reviewer for simulated skill proposals.

SIMULATION ONLY. The reviewer turns ONE strict JSON object (as an LLM might
emit it; in this repo only ever a fixture) into a typed verdict:

* ``ACCEPT``      - a valid typed call to a read-only or stop-only catalogue skill;
* ``NEEDS_HUMAN`` - valid but consequential (human approval required), or the
                    proposer asked for clarification;
* ``REJECT``      - everything else, always with typed reasons.

The verdict is computed from the parsed typed fields and the caller supplied
simulated ``context`` only. Free text (``rationale``) is data: it is length and
character checked, hashed into the audit record and never interpreted. An
optional ``advisory`` (for example a model's opinion) is recorded in the audit
record and can never change the verdict: this layer cannot be talked into a
different answer.

An ACCEPT does NOT authorise execution. It only means "well-formed call to an
allowlisted skill". The downstream deterministic action gate and a simulator
executor (not part of this package) decide what happens next.

``review`` never raises; an unexpected internal error becomes a REJECT with
reason ``INTERNAL_ERROR``.
"""

import hashlib
import math
import unicodedata
from dataclasses import dataclass, field

from . import strict_json
from .catalogue import (
    CATALOGUE,
    IDENT_RE,
    MAX_STATE_AGE_MS,
    PROPOSAL_VERSION,
    ROBOT_STATES,
    SAFETY_CONSEQUENTIAL,
    SAFETY_READ_ONLY,
    SAFETY_STOP_ONLY,
    SUPPORTED_ROBOTS,
    TASK_ID_RE,
)

REVIEWER_VERSION = "skill-review/v1"
AUDIT_VERSION = "skill-review-audit/v1"
LABEL = "SYNTHETIC teaching reviewer - not a security guarantee, not an action gate"

ACCEPT = "ACCEPT"
REJECT = "REJECT"
NEEDS_HUMAN = "NEEDS_HUMAN"
VERDICTS = (ACCEPT, REJECT, NEEDS_HUMAN)

MAX_TEXT_CHARS = 280
MAX_REPORTED = 8  # cap on repeated per-key reasons so a hostile input cannot flood the audit

ENVELOPE_KEYS = frozenset(
    {"proposal_version", "skill", "robot_id", "args", "rationale", "clarification_request"}
)
CLARIFY_ONLY_KEYS = frozenset({"proposal_version", "skill", "clarification_request", "rationale"})
CONTEXT_KEYS = frozenset({"robot_id", "robot_state", "state_age_ms", "active_task_id", "estop_sim"})
_PATH_URL_CHARS = ("/", "\\", ":")

REASON_CODES = frozenset(
    {
        # parse stage (strict_json)
        "INPUT_NOT_TEXT", "INPUT_TOO_LARGE", "INVALID_ENCODING", "EMPTY_INPUT",
        "NOT_STRICT_JSON", "NOT_A_JSON_OBJECT", "DUPLICATE_KEY", "NON_FINITE_NUMBER",
        "NUMBER_TOO_LARGE", "NESTING_TOO_DEEP",
        # envelope / skill / robot / args
        "UNKNOWN_FIELD", "MISSING_FIELD", "WRONG_TYPE", "UNSUPPORTED_VERSION",
        "CONFLICTING_FIELDS", "RATIONALE_INVALID", "MALFORMED_IDENTIFIER", "UNKNOWN_SKILL",
        "UNSUPPORTED_ROBOT", "UNKNOWN_ARGUMENT", "MISSING_ARGUMENT", "PATH_OR_URL_ARGUMENT",
        "UNKNOWN_ARG_VALUE", "OUT_OF_RANGE",
        # context / preconditions
        "CONTEXT_INVALID", "ROBOT_CONTEXT_MISMATCH", "STALE_STATE", "PRECONDITION_FAILED",
        "ESTOP_ACTIVE", "NO_SUCH_TASK", "UNKNOWN_PRECONDITION",
        # non-reject outcomes and fail-closed fallbacks
        "HUMAN_APPROVAL_REQUIRED", "CLARIFICATION_REQUESTED", "UNKNOWN_SAFETY_LEVEL",
        "INTERNAL_ERROR",
    }
)


@dataclass(frozen=True)
class Reason:
    code: str
    field: str = ""
    detail: str = ""

    def __post_init__(self):
        if self.code not in REASON_CODES:  # developer error; review() turns it into INTERNAL_ERROR
            raise ValueError(f"unregistered reason code {self.code!r}")

    def as_dict(self):
        return {"code": self.code, "field": self.field, "detail": self.detail}


@dataclass(frozen=True)
class SkillCall:
    """A validated, typed call. Only ever produced for ACCEPT / pending approval."""

    skill: str
    robot_id: str
    args: tuple  # sorted ((name, value), ...) so it is hashable and deterministic
    safety_level: str

    def as_dict(self):
        return {
            "skill": self.skill,
            "robot_id": self.robot_id,
            "args": dict(self.args),
            "safety_level": self.safety_level,
        }


@dataclass(frozen=True)
class ReviewResult:
    verdict: str
    reasons: tuple
    call: object = None  # SkillCall, only when verdict == ACCEPT
    pending_call: object = None  # SkillCall, only for NEEDS_HUMAN / HUMAN_APPROVAL_REQUIRED
    audit: dict = field(default_factory=dict)

    @property
    def primary_reason(self):
        return self.reasons[0].code if self.reasons else None

    def as_dict(self):
        return {
            "verdict": self.verdict,
            "reasons": [r.as_dict() for r in self.reasons],
            "call": self.call.as_dict() if self.call else None,
            "pending_call": self.pending_call.as_dict() if self.pending_call else None,
            "audit": self.audit,
        }


def _safe(value, limit=48):
    try:
        return ascii(value)[:limit]
    except Exception:  # never let diagnostics raise
        return "<unprintable>"


def _bad_text(value):
    """True if ``value`` holds control, format, surrogate, private-use, unassigned or line-separator chars."""
    for ch in value:
        cat = unicodedata.category(ch)
        if cat[0] == "C" or cat in ("Zl", "Zp"):
            return True
    return False


def _check_text(value, field_name):
    if type(value) is not str:
        return Reason("WRONG_TYPE", field_name, f"expected string, got {type(value).__name__}")
    if len(value) > MAX_TEXT_CHARS:
        return Reason("RATIONALE_INVALID", field_name, f"longer than {MAX_TEXT_CHARS} characters")
    if _bad_text(value):
        return Reason("RATIONALE_INVALID", field_name, "contains control/format/surrogate characters")
    return None


def _check_identifier(value, field_name):
    """Return a Reason if ``value`` is not a plain lowercase ASCII identifier (checked type first)."""
    if type(value) is not str:
        return Reason("WRONG_TYPE", field_name, f"expected string, got {type(value).__name__}")
    if IDENT_RE.fullmatch(value) is None:
        return Reason("MALFORMED_IDENTIFIER", field_name, f"not a lowercase ASCII identifier: {_safe(value)}")
    return None


# --------------------------------------------------------------------------- envelope


def _check_envelope(proposal):
    reasons = []
    unknown = sorted(k for k in proposal if k not in ENVELOPE_KEYS)
    for key in unknown[:MAX_REPORTED]:
        reasons.append(Reason("UNKNOWN_FIELD", _safe(key), "field is not part of skill-proposal/v1"))
    if len(unknown) > MAX_REPORTED:
        reasons.append(Reason("UNKNOWN_FIELD", "*", f"{len(unknown) - MAX_REPORTED} more unknown fields"))

    if "proposal_version" not in proposal:
        reasons.append(Reason("MISSING_FIELD", "proposal_version", "required"))
    elif type(proposal["proposal_version"]) is not str:
        reasons.append(Reason("WRONG_TYPE", "proposal_version", "expected string"))
    elif proposal["proposal_version"] != PROPOSAL_VERSION:
        reasons.append(Reason("UNSUPPORTED_VERSION", "proposal_version", f"expected {PROPOSAL_VERSION}"))

    if "skill" not in proposal:
        reasons.append(Reason("MISSING_FIELD", "skill", "required"))
        return reasons

    if "rationale" in proposal:
        r = _check_text(proposal["rationale"], "rationale")
        if r:
            reasons.append(r)

    if proposal["skill"] is None:
        if "clarification_request" not in proposal:
            reasons.append(Reason("MISSING_FIELD", "clarification_request", "required when skill is null"))
        else:
            r = _check_text(proposal["clarification_request"], "clarification_request")
            if r:
                reasons.append(r)
        extra = sorted(k for k in proposal if k in ENVELOPE_KEYS and k not in CLARIFY_ONLY_KEYS)
        for key in extra:
            reasons.append(Reason("CONFLICTING_FIELDS", key, "not allowed together with skill=null"))
    else:
        if "clarification_request" in proposal:
            reasons.append(Reason("CONFLICTING_FIELDS", "clarification_request", "only allowed with skill=null"))
        for key in ("robot_id", "args"):
            if key not in proposal:
                reasons.append(Reason("MISSING_FIELD", key, "required"))
    return reasons


# --------------------------------------------------------------------------- arguments


def _check_arg_value(spec, value):
    """Return (cleaned_value, None) or (None, Reason). Type is always checked first."""
    name = spec.name
    if spec.kind == "enum":
        if type(value) is not str:
            return None, Reason("WRONG_TYPE", name, f"expected string, got {type(value).__name__}")
        if "/" in value or "\\" in value or ":" in value or ".." in value:
            return None, Reason("PATH_OR_URL_ARGUMENT", name, f"path/URL-like value refused: {_safe(value)}")
        if IDENT_RE.fullmatch(value) is None:
            return None, Reason("MALFORMED_IDENTIFIER", name, f"not a lowercase ASCII identifier: {_safe(value)}")
        if value not in spec.allowed:
            return None, Reason("UNKNOWN_ARG_VALUE", name, f"{_safe(value)} is not in the closed value set")
        return value, None
    if spec.kind == "task_id":
        if type(value) is not str:
            return None, Reason("WRONG_TYPE", name, f"expected string, got {type(value).__name__}")
        if TASK_ID_RE.fullmatch(value) is None:
            return None, Reason("MALFORMED_IDENTIFIER", name, f"not a task id: {_safe(value)}")
        return value, None
    if spec.kind == "number":
        if type(value) not in (int, float):  # excludes bool, str, list, dict, None
            return None, Reason("WRONG_TYPE", name, f"expected number, got {type(value).__name__}")
        if not math.isfinite(value):
            return None, Reason("NON_FINITE_NUMBER", name, "not finite")
        if not spec.minimum <= value <= spec.maximum:
            return None, Reason("OUT_OF_RANGE", name, f"outside {spec.minimum:g}..{spec.maximum:g} {spec.unit}")
        return float(value), None
    if spec.kind == "integer":
        if type(value) is not int:  # excludes bool and floats such as 5.0
            return None, Reason("WRONG_TYPE", name, f"expected integer, got {type(value).__name__}")
        if not spec.minimum <= value <= spec.maximum:
            return None, Reason("OUT_OF_RANGE", name, f"outside {spec.minimum:g}..{spec.maximum:g} {spec.unit}")
        return value, None
    return None, Reason("UNKNOWN_ARG_VALUE", name, "unsupported argument kind")  # fail closed


def _check_args(spec, args):
    """Return (cleaned dict, reasons)."""
    if type(args) is not dict:
        return None, [Reason("WRONG_TYPE", "args", f"expected object, got {type(args).__name__}")]
    reasons = []
    known = {a.name for a in spec.args}
    unknown = sorted(k for k in args if k not in known)
    for key in unknown[:MAX_REPORTED]:
        reasons.append(Reason("UNKNOWN_ARGUMENT", _safe(key), f"{spec.name} takes no such argument"))
    if len(unknown) > MAX_REPORTED:
        reasons.append(Reason("UNKNOWN_ARGUMENT", "*", f"{len(unknown) - MAX_REPORTED} more unknown arguments"))
    cleaned = {}
    for a in spec.args:
        if a.name not in args:
            if a.required:
                reasons.append(Reason("MISSING_ARGUMENT", a.name, "required"))
            continue
        value, reason = _check_arg_value(a, args[a.name])
        if reason:
            reasons.append(reason)
        else:
            cleaned[a.name] = value
    return cleaned, reasons


# --------------------------------------------------------------------------- context


def validate_context(context):
    """Validate the caller-supplied simulated context. Returns a list of Reasons."""
    if type(context) is not dict:
        return [Reason("CONTEXT_INVALID", "context", f"expected object, got {type(context).__name__}")]
    reasons = []
    for key in sorted(k for k in context if k not in CONTEXT_KEYS and type(k) is str):
        reasons.append(Reason("CONTEXT_INVALID", _safe(key), "unknown context key"))
    if any(type(k) is not str for k in context):
        reasons.append(Reason("CONTEXT_INVALID", "context", "non-string key"))
    for key in sorted(CONTEXT_KEYS):
        if key not in context:
            reasons.append(Reason("CONTEXT_INVALID", key, "required"))
    if reasons:
        return reasons
    if type(context["robot_id"]) is not str or context["robot_id"] not in SUPPORTED_ROBOTS:
        reasons.append(Reason("CONTEXT_INVALID", "robot_id", "not a supported simulated robot"))
    if type(context["robot_state"]) is not str or context["robot_state"] not in ROBOT_STATES:
        reasons.append(Reason("CONTEXT_INVALID", "robot_state", f"expected one of {ROBOT_STATES}"))
    age = context["state_age_ms"]
    if type(age) is not int or not 0 <= age <= 10**9:
        reasons.append(Reason("CONTEXT_INVALID", "state_age_ms", "expected integer 0..1e9"))
    task = context["active_task_id"]
    if task is not None and (type(task) is not str or TASK_ID_RE.fullmatch(task) is None):
        reasons.append(Reason("CONTEXT_INVALID", "active_task_id", "expected null or task-<digits>"))
    if type(context["estop_sim"]) is not bool:
        reasons.append(Reason("CONTEXT_INVALID", "estop_sim", "expected boolean"))
    return reasons


def _pre_state_fresh(context, args):
    if context["state_age_ms"] > MAX_STATE_AGE_MS:
        return Reason("STALE_STATE", "state_age_ms", f"state older than {MAX_STATE_AGE_MS} ms")
    return None


def _pre_robot_idle(context, args):
    if context["robot_state"] != "idle":
        return Reason("PRECONDITION_FAILED", "robot_state", "robot must be idle")
    return None


def _pre_no_estop(context, args):
    if context["estop_sim"]:
        return Reason("ESTOP_ACTIVE", "estop_sim", "simulated e-stop is active")
    return None


def _pre_active_task_matches(context, args):
    if context["active_task_id"] is None or context["active_task_id"] != args.get("task_id"):
        return Reason("NO_SUCH_TASK", "task_id", "no active task with that id")
    return None


_PRECONDITIONS = {
    "state_fresh": _pre_state_fresh,
    "robot_idle": _pre_robot_idle,
    "no_estop": _pre_no_estop,
    "active_task_matches": _pre_active_task_matches,
}


# --------------------------------------------------------------------------- audit / entry


def _fingerprint(data):
    if type(data) is bytes:
        raw = data
    elif type(data) is str:
        raw = data.encode("utf-8", "surrogatepass")
        # lone surrogates are hashed via surrogatepass so the fingerprint never raises
    else:
        return {"type": type(data).__name__, "sha256": None, "bytes": None, "preview": ""}
    return {
        "type": type(data).__name__,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "preview": ascii(data[:80]),  # escaped, truncated; never the raw text
    }


def _advisory_record(advisory):
    """Record an advisory opinion. It is NEVER consulted for the verdict."""
    rec = {"provided": advisory is not None, "used_for_verdict": False}
    if type(advisory) is dict:
        opinion = advisory.get("opinion")
        rec["opinion"] = opinion if opinion in ("approve", "reject", "abstain") else "malformed_ignored"
        conf = advisory.get("confidence")
        if type(conf) is float and math.isfinite(conf):
            rec["confidence"] = conf
    elif advisory is not None:
        rec["opinion"] = "malformed_ignored"
    return rec


class _Run:
    def __init__(self, data, advisory):
        self.stages = []
        self.rationale_sha256 = None
        self.skill = None
        self.base = {
            "audit_version": AUDIT_VERSION,
            "reviewer_version": REVIEWER_VERSION,
            "label": LABEL,
            "authorizes_execution": False,
            "input": _fingerprint(data),
            "advisory": _advisory_record(advisory),
        }

    def stage(self, name, reasons):
        self.stages.append({"stage": name, "outcome": "fail" if reasons else "pass"})

    def finish(self, verdict, reasons, call=None, pending=None):
        audit = dict(self.base)
        audit["stages"] = list(self.stages)
        audit["skill"] = self.skill
        audit["rationale_sha256"] = self.rationale_sha256
        audit["verdict"] = verdict
        audit["reason_codes"] = [r.code for r in reasons]
        return ReviewResult(verdict, tuple(reasons), call, pending, audit)


def _review(data, context, advisory, catalogue):
    run = _Run(data, advisory)

    try:
        proposal = strict_json.parse_object(data)
    except strict_json.StrictJSONError as exc:
        reasons = [Reason(exc.code, "", exc.detail)]
        run.stage("parse", reasons)
        return run.finish(REJECT, reasons)
    run.stage("parse", [])

    rationale = proposal.get("rationale")
    if type(rationale) is str:
        run.rationale_sha256 = hashlib.sha256(rationale.encode("utf-8", "surrogatepass")).hexdigest()

    reasons = _check_envelope(proposal)
    run.stage("envelope", reasons)
    if reasons:
        return run.finish(REJECT, reasons)

    if proposal["skill"] is None:  # structured clarification request: a human reads it
        return run.finish(NEEDS_HUMAN, [Reason("CLARIFICATION_REQUESTED", "clarification_request", "no skill chosen")])

    skill_name = proposal["skill"]
    r = _check_identifier(skill_name, "skill")
    if r is None and skill_name not in catalogue:
        r = Reason("UNKNOWN_SKILL", "skill", f"{_safe(skill_name)} is not in the catalogue")
    run.stage("skill", [r] if r else [])
    if r:
        return run.finish(REJECT, [r])
    spec = catalogue[skill_name]
    run.skill = skill_name

    robot_id = proposal["robot_id"]
    r = _check_identifier(robot_id, "robot_id")
    if r is None and robot_id not in SUPPORTED_ROBOTS:
        r = Reason("UNSUPPORTED_ROBOT", "robot_id", f"{_safe(robot_id)} is not a supported simulated robot")
    run.stage("robot", [r] if r else [])
    if r:
        return run.finish(REJECT, [r])

    cleaned, reasons = _check_args(spec, proposal["args"])
    run.stage("args", reasons)
    if reasons:
        return run.finish(REJECT, reasons)

    reasons = validate_context(context)
    if not reasons and context["robot_id"] != robot_id:
        reasons = [Reason("ROBOT_CONTEXT_MISMATCH", "robot_id", "proposal robot differs from context robot")]
    run.stage("context", reasons)
    if reasons:
        return run.finish(REJECT, reasons)

    reasons = []
    for pid in spec.preconditions:
        check = _PRECONDITIONS.get(pid)
        if check is None:  # unknown precondition id: fail closed
            reasons.append(Reason("UNKNOWN_PRECONDITION", pid, "no evaluator for this precondition"))
            continue
        failed = check(context, cleaned)
        if failed:
            reasons.append(failed)
    run.stage("preconditions", reasons)
    if reasons:
        return run.finish(REJECT, reasons)

    call = SkillCall(spec.name, robot_id, tuple(sorted(cleaned.items())), spec.safety_level)
    if spec.safety_level in (SAFETY_READ_ONLY, SAFETY_STOP_ONLY):
        return run.finish(ACCEPT, [], call=call)
    if spec.safety_level == SAFETY_CONSEQUENTIAL:
        reason = Reason("HUMAN_APPROVAL_REQUIRED", "skill", f"{spec.name} is consequential")
        return run.finish(NEEDS_HUMAN, [reason], pending=call)
    return run.finish(REJECT, [Reason("UNKNOWN_SAFETY_LEVEL", "skill", "unrecognised safety level")])


def review(data, context, advisory=None, *, catalogue=CATALOGUE):
    """Review one proposal. Pure function of its arguments; never raises; mutates nothing."""
    try:
        return _review(data, context, advisory, catalogue)
    except Exception as exc:  # fail closed on any bug, including RecursionError/MemoryError
        reason = Reason("INTERNAL_ERROR", "", type(exc).__name__)
        audit = {
            "audit_version": AUDIT_VERSION,
            "reviewer_version": REVIEWER_VERSION,
            "label": LABEL,
            "authorizes_execution": False,
            "input": {"type": type(data).__name__, "sha256": None, "bytes": None, "preview": ""},
            "advisory": {"provided": advisory is not None, "used_for_verdict": False},
            "stages": [],
            "skill": None,
            "rationale_sha256": None,
            "verdict": REJECT,
            "reason_codes": [reason.code],
        }
        return ReviewResult(REJECT, (reason,), None, None, audit)
