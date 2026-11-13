"""Fail-closed SIMULATED action gate (issue #59). Python standard library only.

SIMULATION ONLY. Nothing here talks to a robot, ROS 2, a network or a model. The
gate decides ALLOW / HOLD / REJECT for a *proposed simulated action*; a
separate simulated sink (``sim_sink.py``) records what would be dispatched.
This is an educational design, not a safety-certified function, not real-time
and not authentication (``source_id`` is a label checked against an allow-list).

Fail-closed rule: ALLOW is produced only when every check returns the PASS
sentinel. A missing field, wrong type, unknown skill, unknown mode, unparseable
text, a check that raises, a check that returns anything else, an audit
failure, a clock problem or an internal error all produce a blocked decision
(REJECT or HOLD) with a typed reason. Public methods never raise on bad input.

Time is logical: callers pass an integer tick ``now``. Semantics (all tested):

* a message is valid while ``created_tick <= now < expires_tick`` (half-open);
* an observation is valid while ``now < observation.tick + observation_ttl_ticks``;
* ``sequence_id`` must be strictly greater than the last ALLOWED one in the
  current epoch (equal -> DUPLICATE_SEQUENCE, lower -> STALE_SEQUENCE; gaps are
  allowed so a lost message cannot wedge the gate);
* ``now`` may stay equal but must never decrease (CLOCK_REGRESSION).
"""

from __future__ import annotations

import enum
import importlib.util
import json
import math
import re
from collections import OrderedDict, deque
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Optional


# --------------------------------------------------------------------------
# Joint layout: read from the sibling mock-policy contract (single source of
# truth). Loaded under a private module name so it cannot collide with other
# top-level modules called "contract" or "validate".
# --------------------------------------------------------------------------
def _load_mock_contract() -> Any:
    path = Path(__file__).resolve().parent.parent / "mock-policy" / "contract.py"
    spec = importlib.util.spec_from_file_location("_mock_policy_contract", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load contract from {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_C = _load_mock_contract()
JOINT_NAMES = tuple(_C.JOINT_NAMES)
JOINT_UNITS = tuple(_C.JOINT_UNITS)
JOINT_LIMITS = tuple(tuple(x) for x in _C.JOINT_LIMITS)
ACTION_DIM = int(_C.ACTION_DIM)
ACTION_SPACE = str(_C.ACTION_SPACE)
if not (len(JOINT_NAMES) == len(JOINT_UNITS) == len(JOINT_LIMITS) == ACTION_DIM):
    raise ImportError("mock-policy contract is internally inconsistent")

SCHEMA = "action-gate/v1"
COMMANDS = ("set_joint_targets", "hold")  # the ONLY skills this gate knows
MAX_INT = 2**53
MAX_KEYS = 32
MAX_TEXT = 65536
DEFAULT_MAX_STEP = (0.5, 0.5, 0.5, 0.5, 0.5, 0.04)  # per-joint |target - observed| bound
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")


# --------------------------------------------------------------------------
# Typed vocabulary
# --------------------------------------------------------------------------
class Verdict(str, enum.Enum):
    ALLOW = "ALLOW"
    HOLD = "HOLD"      # gate is in a non-actuating state; nothing was dispatched
    REJECT = "REJECT"  # this command is defective or unauthorised; nothing was dispatched


class Mode(str, enum.Enum):
    """Gate operating modes. Only AUTONOMOUS ever lets a command through."""

    AUTONOMOUS = "autonomous"
    HOLD = "hold"
    INTERVENTION = "intervention"
    SAFE_STOP = "safe_stop"
    LOCKED = "locked"


class Category(str, enum.Enum):
    MALFORMED = "malformed"
    PROVENANCE = "provenance"
    UNAVAILABLE_COMMAND = "unavailable_command"
    MODEL_FAILURE = "model_failure"
    STALE_RESPONSE = "stale_response"
    INTERVENTION = "intervention"
    HOLD = "hold"
    GATE_FAULT = "gate_fault"


class Reason(str, enum.Enum):
    # malformed envelope / timeline
    MALFORMED_MESSAGE = "MALFORMED_MESSAGE"
    MISSING_FIELD = "MISSING_FIELD"
    UNKNOWN_FIELD = "UNKNOWN_FIELD"
    WRONG_TYPE = "WRONG_TYPE"
    BAD_IDENTIFIER = "BAD_IDENTIFIER"
    INTEGER_OUT_OF_RANGE = "INTEGER_OUT_OF_RANGE"
    UNSUPPORTED_SCHEMA = "UNSUPPORTED_SCHEMA"
    UNKNOWN_MODE = "UNKNOWN_MODE"
    INVALID_EXPIRY = "INVALID_EXPIRY"
    INVALID_TIMELINE = "INVALID_TIMELINE"
    DUPLICATE_OBSERVATION = "DUPLICATE_OBSERVATION"
    # provenance
    UNAUTHORIZED_SOURCE = "UNAUTHORIZED_SOURCE"
    # unavailable command
    UNKNOWN_SKILL = "UNKNOWN_SKILL"
    COMMAND_DISABLED = "COMMAND_DISABLED"
    CONTROL_DOMAIN_DENIED = "CONTROL_DOMAIN_DENIED"
    NOT_SIMULATED = "NOT_SIMULATED"
    # model failure (the model proposed something unusable)
    MODEL_NOT_APPROVED = "MODEL_NOT_APPROVED"
    BAD_ARGUMENT_FIELDS = "BAD_ARGUMENT_FIELDS"
    BAD_ARGUMENT_TYPE = "BAD_ARGUMENT_TYPE"
    WRONG_ACTION_SPACE = "WRONG_ACTION_SPACE"
    WRONG_DIMENSION = "WRONG_DIMENSION"
    WRONG_UNITS = "WRONG_UNITS"
    NON_FINITE_VALUE = "NON_FINITE_VALUE"
    OUT_OF_BOUNDS = "OUT_OF_BOUNDS"
    STEP_TOO_LARGE = "STEP_TOO_LARGE"
    # stale / replayed response
    STALE_OBSERVATION = "STALE_OBSERVATION"
    UNKNOWN_OBSERVATION = "UNKNOWN_OBSERVATION"
    EXPIRED = "EXPIRED"
    FUTURE_TIMESTAMP = "FUTURE_TIMESTAMP"
    DUPLICATE_SEQUENCE = "DUPLICATE_SEQUENCE"
    STALE_SEQUENCE = "STALE_SEQUENCE"
    STALE_EPOCH = "STALE_EPOCH"
    EPOCH_NOT_GRANTED = "EPOCH_NOT_GRANTED"
    # operator intervention
    INTERVENTION_ACTIVE = "INTERVENTION_ACTIVE"
    # gate is holding (watchdog, restart, operator hold, safe stop, lost state)
    GATE_HOLD = "GATE_HOLD"
    SAFE_STOP_ACTIVE = "SAFE_STOP_ACTIVE"
    GATE_LOCKED = "GATE_LOCKED"
    MODE_MISMATCH = "MODE_MISMATCH"
    # gate faults (always blocked)
    INTERNAL_ERROR = "INTERNAL_ERROR"
    CHECK_RETURNED_INVALID = "CHECK_RETURNED_INVALID"
    AUDIT_FAILURE = "AUDIT_FAILURE"
    CLOCK_REGRESSION = "CLOCK_REGRESSION"
    INVALID_CLOCK = "INVALID_CLOCK"
    INVALID_TRANSITION = "INVALID_TRANSITION"
    EPOCH_PERSIST_FAILED = "EPOCH_PERSIST_FAILED"
    EPOCH_EXHAUSTED = "EPOCH_EXHAUSTED"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"


_M, _P, _U, _F, _S = (Category.MALFORMED, Category.PROVENANCE, Category.UNAVAILABLE_COMMAND,
                      Category.MODEL_FAILURE, Category.STALE_RESPONSE)
_I, _H, _G = Category.INTERVENTION, Category.HOLD, Category.GATE_FAULT
REASON_CATEGORY: dict = {
    Reason.MALFORMED_MESSAGE: _M, Reason.MISSING_FIELD: _M, Reason.UNKNOWN_FIELD: _M,
    Reason.WRONG_TYPE: _M, Reason.BAD_IDENTIFIER: _M, Reason.INTEGER_OUT_OF_RANGE: _M,
    Reason.UNSUPPORTED_SCHEMA: _M, Reason.UNKNOWN_MODE: _M, Reason.INVALID_EXPIRY: _M,
    Reason.INVALID_TIMELINE: _M, Reason.DUPLICATE_OBSERVATION: _M,
    Reason.UNAUTHORIZED_SOURCE: _P,
    Reason.UNKNOWN_SKILL: _U, Reason.COMMAND_DISABLED: _U, Reason.CONTROL_DOMAIN_DENIED: _U,
    Reason.NOT_SIMULATED: _U,
    Reason.MODEL_NOT_APPROVED: _F, Reason.BAD_ARGUMENT_FIELDS: _F, Reason.BAD_ARGUMENT_TYPE: _F,
    Reason.WRONG_ACTION_SPACE: _F, Reason.WRONG_DIMENSION: _F, Reason.WRONG_UNITS: _F,
    Reason.NON_FINITE_VALUE: _F, Reason.OUT_OF_BOUNDS: _F, Reason.STEP_TOO_LARGE: _F,
    Reason.STALE_OBSERVATION: _S, Reason.UNKNOWN_OBSERVATION: _S, Reason.EXPIRED: _S,
    Reason.FUTURE_TIMESTAMP: _S, Reason.DUPLICATE_SEQUENCE: _S, Reason.STALE_SEQUENCE: _S,
    Reason.STALE_EPOCH: _S, Reason.EPOCH_NOT_GRANTED: _S,
    Reason.INTERVENTION_ACTIVE: _I,
    Reason.GATE_HOLD: _H, Reason.SAFE_STOP_ACTIVE: _H, Reason.GATE_LOCKED: _H,
    Reason.MODE_MISMATCH: _H,
    Reason.INTERNAL_ERROR: _G, Reason.CHECK_RETURNED_INVALID: _G, Reason.AUDIT_FAILURE: _G,
    Reason.CLOCK_REGRESSION: _G, Reason.INVALID_CLOCK: _G, Reason.INVALID_TRANSITION: _G,
    Reason.EPOCH_PERSIST_FAILED: _G, Reason.EPOCH_EXHAUSTED: _G, Reason.INVALID_ARGUMENT: _G,
}
if set(REASON_CATEGORY) != set(Reason):  # loud at import: every reason needs a category
    raise ImportError("REASON_CATEGORY must cover every Reason")


def verdict_for(reason: Reason) -> Verdict:
    """HOLD for gate-state blocks (hold, intervention); REJECT for everything else."""
    if REASON_CATEGORY[reason] in (Category.HOLD, Category.INTERVENTION):
        return Verdict.HOLD
    return Verdict.REJECT


_MODE_REASON = {
    Mode.HOLD: Reason.GATE_HOLD,
    Mode.INTERVENTION: Reason.INTERVENTION_ACTIVE,
    Mode.SAFE_STOP: Reason.SAFE_STOP_ACTIVE,
    Mode.LOCKED: Reason.GATE_LOCKED,
}
_MODE_VALUES = frozenset(m.value for m in Mode)


@dataclass(frozen=True)
class Failure:
    """What a check returns when it blocks."""

    reason: Reason
    field: str
    detail: str = ""


class _Pass:
    def __repr__(self) -> str:
        return "PASS"


PASS = _Pass()  # the ONLY value a check may return to let a message continue


@dataclass(frozen=True)
class Decision:
    """Immutable result of one evaluation. Dispatch uses only these fields (never
    the original message), so mutating the message after the check changes nothing."""

    verdict: Verdict
    reason: Optional[Reason]
    category: Optional[str]
    field: str
    detail: str
    decision_id: int
    tick: int
    audit_id: int
    command: Optional[str] = None
    targets: Optional[tuple] = None
    epoch: Optional[int] = None
    sequence_id: Optional[int] = None
    expires_tick: Optional[int] = None

    @property
    def allowed(self) -> bool:
        return self.verdict is Verdict.ALLOW

    def as_dict(self) -> dict:
        return {
            "verdict": self.verdict.value,
            "reason": self.reason.value if self.reason else None,
            "category": self.category,
            "field": self.field,
            "detail": self.detail,
            "decision_id": self.decision_id,
            "tick": self.tick,
            "audit_id": self.audit_id,
            "command": self.command,
            "epoch": self.epoch,
            "sequence_id": self.sequence_id,
        }


@dataclass(frozen=True)
class OpResult:
    """Outcome of an operator/lifecycle call (not a command decision)."""

    ok: bool
    reason: Optional[Reason] = None
    detail: str = ""
    epoch: Optional[int] = None


# --------------------------------------------------------------------------
# Small helpers (type checks are EXACT: bool, subclasses and numpy-like types
# are never accepted where int/float/str/dict is required)
# --------------------------------------------------------------------------
def _is_int(v: Any) -> bool:
    return type(v) is int


def _is_num(v: Any) -> bool:
    return type(v) is int or type(v) is float


def _printable(s: Any, limit: int = 64) -> str:
    """Sanitised, bounded text for logs (no control characters, no log injection)."""
    if type(s) is not str:
        return "<not-a-string>"
    out = "".join(ch if 32 <= ord(ch) < 127 else "?" for ch in s[:limit])
    return out + ("+" if len(s) > limit else "")


def _tname(v: Any) -> str:
    try:
        return _printable(type(v).__name__, 32)
    except Exception:
        return "<unknown>"


def _ident_ok(v: Any) -> bool:
    return type(v) is str and len(v) <= 64 and _ID_RE.fullmatch(v) is not None


def _number_failure(v: Any, lo: float, hi: float, path: str, type_reason: Reason) -> Optional[Failure]:
    """None if ``v`` is a finite number within [lo, hi]; else a Failure. Checks the
    type first, finiteness before any comparison, and never converts huge ints."""
    if not _is_num(v):
        return Failure(type_reason, path, f"expected a number, got {_tname(v)}")
    if type(v) is float and not math.isfinite(v):
        return Failure(Reason.NON_FINITE_VALUE, path, "NaN or infinity")
    if not (lo <= v <= hi):
        return Failure(Reason.OUT_OF_BOUNDS, path, "outside the allowed limits")
    return None


# --------------------------------------------------------------------------
# Configuration (trusted; invalid config raises ValueError at construction)
# --------------------------------------------------------------------------
def _pos_int(name: str, v: Any, minimum: int = 1) -> int:
    if not _is_int(v) or v < minimum or v > MAX_INT:
        raise ValueError(f"{name} must be an int >= {minimum}")
    return v


@dataclass(frozen=True)
class GateConfig:
    """Defaults are fail-closed: no allowed source and no approved model, so every
    action is blocked until the operator explicitly configures both."""

    allowed_sources: Any = frozenset()
    approved_models: Any = MappingProxyType({})  # model_id -> revisions
    allowed_domains: Any = frozenset({"sim-arm"})
    enabled_commands: Any = frozenset(COMMANDS)
    max_step: tuple = DEFAULT_MAX_STEP
    observation_ttl_ticks: int = 5
    max_message_ttl_ticks: int = 20
    watchdog_ticks: int = 10
    observation_registry_size: int = 64
    audit_capacity: int = 1000
    pending_capacity: int = 64

    def __post_init__(self) -> None:
        def strset(name: str, v: Any, allowed: Optional[tuple] = None) -> frozenset:
            if isinstance(v, (str, bytes)) or not hasattr(v, "__iter__"):
                raise ValueError(f"{name} must be a collection of strings")
            out = frozenset(v)
            for x in out:
                if type(x) is not str or not x or len(x) > 64:
                    raise ValueError(f"{name} entries must be non-empty strings <= 64 chars")
                if allowed is not None and x not in allowed:
                    raise ValueError(f"{name} contains unknown entry {_printable(x)!r}")
            return out

        object.__setattr__(self, "allowed_sources", strset("allowed_sources", self.allowed_sources))
        object.__setattr__(self, "allowed_domains", strset("allowed_domains", self.allowed_domains))
        object.__setattr__(self, "enabled_commands",
                           strset("enabled_commands", self.enabled_commands, COMMANDS))
        if not hasattr(self.approved_models, "items"):
            raise ValueError("approved_models must be a mapping model_id -> revisions")
        models = {}
        for mid, revs in self.approved_models.items():
            if type(mid) is not str or not mid:
                raise ValueError("approved_models keys must be non-empty strings")
            models[mid] = strset("approved_models revisions", revs)
        object.__setattr__(self, "approved_models", MappingProxyType(models))
        if (not isinstance(self.max_step, tuple) or len(self.max_step) != ACTION_DIM
                or any(not _is_num(x) or not math.isfinite(x) or x <= 0 for x in self.max_step)):
            raise ValueError(f"max_step must be a tuple of {ACTION_DIM} positive finite numbers")
        for name in ("observation_ttl_ticks", "max_message_ttl_ticks", "watchdog_ticks",
                     "observation_registry_size", "audit_capacity", "pending_capacity"):
            _pos_int(name, getattr(self, name))


# --------------------------------------------------------------------------
# Durable epoch store (simulated). The only thing that must survive a crash.
# --------------------------------------------------------------------------
class EpochStore:
    """Simulated durable monotonic counter. Written only when an epoch is granted
    (write-ahead: the epoch is persisted before it is used). ``lose`` and
    ``corrupt`` simulate lost or damaged state; ``fail_saves`` a failing disk."""

    def __init__(self, value: Any = 0) -> None:
        self._value = value
        self.fail_saves = False

    @classmethod
    def provisioned(cls) -> "EpochStore":
        return cls(0)

    def load(self) -> Any:
        return self._value

    def save(self, value: int) -> None:
        if self.fail_saves:
            raise OSError("simulated write failure")
        self._value = value

    def lose(self) -> None:
        self._value = None

    def corrupt(self) -> None:
        self._value = "corrupt"


@dataclass
class _Obs:
    tick: int
    state: tuple


class _Ctx:
    __slots__ = ("gate", "cfg", "msg", "now", "out")

    def __init__(self, gate: "ActionGate", msg: Any, now: int) -> None:
        self.gate = gate
        self.cfg = gate.cfg
        self.msg = msg
        self.now = now
        self.out: dict = {}


class _Unparseable:
    """Stand-in message for text that could not be parsed."""


_UNPARSEABLE = _Unparseable()


# --------------------------------------------------------------------------
# Checks. Each takes a _Ctx and returns PASS or a Failure. They read state but
# never change it; state is committed only after every check passed.
# --------------------------------------------------------------------------
_REQUIRED = (
    ("schema", "str"), ("simulated", "bool"), ("source_id", "ident"), ("model_id", "ident"),
    ("model_revision", "ident"), ("control_domain", "str"), ("command", "str"), ("mode", "str"),
    ("epoch", "uint1"), ("sequence_id", "uint1"), ("observation_id", "ident"),
    ("created_tick", "uint0"), ("expires_tick", "uint0"), ("args", "dict"),
)
_FIELD_NAMES = frozenset(n for n, _ in _REQUIRED)


def _kind_failure(kind: str, v: Any, path: str) -> Optional[Failure]:
    if kind == "str":
        if type(v) is not str:
            return Failure(Reason.WRONG_TYPE, path, f"expected string, got {_tname(v)}")
    elif kind == "ident":
        if type(v) is not str:
            return Failure(Reason.WRONG_TYPE, path, f"expected string, got {_tname(v)}")
        if not _ident_ok(v):
            return Failure(Reason.BAD_IDENTIFIER, path, "invalid characters or length")
    elif kind == "bool":
        if type(v) is not bool:
            return Failure(Reason.WRONG_TYPE, path, f"expected boolean, got {_tname(v)}")
    elif kind == "dict":
        if type(v) is not dict:
            return Failure(Reason.WRONG_TYPE, path, f"expected object, got {_tname(v)}")
    else:  # uint0 / uint1
        if not _is_int(v):
            return Failure(Reason.WRONG_TYPE, path, f"expected integer, got {_tname(v)}")
        if v < (1 if kind == "uint1" else 0) or v > MAX_INT:
            return Failure(Reason.INTEGER_OUT_OF_RANGE, path, "integer outside the allowed range")
    return None


def check_gate_mode(ctx: _Ctx):
    mode = ctx.gate.mode
    if mode is Mode.AUTONOMOUS:
        return PASS
    return Failure(_MODE_REASON.get(mode, Reason.GATE_HOLD), "gate.mode",
                   f"gate mode is {mode.value} (cause: {ctx.gate.hold_cause})")


def check_envelope(ctx: _Ctx):
    msg = ctx.msg
    if msg is _UNPARSEABLE:
        return Failure(Reason.MALFORMED_MESSAGE, "msg", "unparseable text")
    if type(msg) is not dict:
        return Failure(Reason.MALFORMED_MESSAGE, "msg", f"message must be an object, got {_tname(msg)}")
    if len(msg) > MAX_KEYS:
        return Failure(Reason.MALFORMED_MESSAGE, "msg", "too many keys")
    for k in msg:
        if type(k) is not str:
            return Failure(Reason.MALFORMED_MESSAGE, "msg", "non-string key")
    for name, _kind in _REQUIRED:
        if name not in msg:
            return Failure(Reason.MISSING_FIELD, f"msg.{name}", "required field missing")
    unknown = sorted(k for k in msg if k not in _FIELD_NAMES)
    if unknown:
        return Failure(Reason.UNKNOWN_FIELD, f"msg.{_printable(unknown[0], 32)}", "unknown field")
    for name, kind in _REQUIRED:
        bad = _kind_failure(kind, msg[name], f"msg.{name}")
        if bad is not None:
            return bad
    return PASS


def check_schema(ctx: _Ctx):
    if ctx.msg["schema"] != SCHEMA:
        return Failure(Reason.UNSUPPORTED_SCHEMA, "msg.schema", "unsupported schema version")
    if ctx.msg["simulated"] is not True:
        return Failure(Reason.NOT_SIMULATED, "msg.simulated", "only simulated actions are accepted")
    return PASS


def check_provenance(ctx: _Ctx):
    cfg, msg = ctx.cfg, ctx.msg
    if msg["source_id"] not in cfg.allowed_sources:
        return Failure(Reason.UNAUTHORIZED_SOURCE, "msg.source_id", "source is not on the allow-list")
    revisions = cfg.approved_models.get(msg["model_id"])
    if revisions is None:
        return Failure(Reason.MODEL_NOT_APPROVED, "msg.model_id", "model is not approved")
    if msg["model_revision"] not in revisions:
        return Failure(Reason.MODEL_NOT_APPROVED, "msg.model_revision", "model revision is not approved")
    return PASS


def check_domain_and_command(ctx: _Ctx):
    cfg, msg = ctx.cfg, ctx.msg
    domain = msg["control_domain"]
    if len(domain) > 64 or domain not in cfg.allowed_domains:
        return Failure(Reason.CONTROL_DOMAIN_DENIED, "msg.control_domain",
                       "control domain is not an allowed simulated domain")
    cmd = msg["command"]
    if len(cmd) > 64 or cmd not in COMMANDS:
        return Failure(Reason.UNKNOWN_SKILL, "msg.command", "command is not on the skill allow-list")
    if cmd not in cfg.enabled_commands:
        return Failure(Reason.COMMAND_DISABLED, "msg.command", "command is allow-listed but disabled")
    return PASS


def check_mode(ctx: _Ctx):
    m = ctx.msg["mode"]
    if len(m) > 32 or m not in _MODE_VALUES:
        return Failure(Reason.UNKNOWN_MODE, "msg.mode", "unknown mode")
    if m != ctx.gate.mode.value:
        return Failure(Reason.MODE_MISMATCH, "msg.mode", "message mode differs from the gate mode")
    return PASS


def _args_shape(args: dict, required: tuple) -> Optional[Failure]:
    if len(args) > MAX_KEYS:
        return Failure(Reason.BAD_ARGUMENT_FIELDS, "msg.args", "too many keys")
    for k in args:
        if type(k) is not str:
            return Failure(Reason.BAD_ARGUMENT_FIELDS, "msg.args", "non-string key")
    for name in required:
        if name not in args:
            return Failure(Reason.BAD_ARGUMENT_FIELDS, f"msg.args.{name}", "required field missing")
    unknown = sorted(k for k in args if k not in required)
    if unknown:
        return Failure(Reason.BAD_ARGUMENT_FIELDS, f"msg.args.{_printable(unknown[0], 32)}", "unknown field")
    return None


def _args_set_joint_targets(ctx: _Ctx):
    args = ctx.msg["args"]
    bad = _args_shape(args, ("action_space", "units", "values"))
    if bad is not None:
        return bad
    space = args["action_space"]
    if type(space) is not str:
        return Failure(Reason.BAD_ARGUMENT_TYPE, "msg.args.action_space", "expected string")
    if space != ACTION_SPACE:
        return Failure(Reason.WRONG_ACTION_SPACE, "msg.args.action_space", "unsupported action space")
    units, values = args["units"], args["values"]
    if type(units) is not list:
        return Failure(Reason.BAD_ARGUMENT_TYPE, "msg.args.units", f"expected list, got {_tname(units)}")
    if type(values) is not list:
        return Failure(Reason.BAD_ARGUMENT_TYPE, "msg.args.values", f"expected list, got {_tname(values)}")
    if len(units) != ACTION_DIM:
        return Failure(Reason.WRONG_DIMENSION, "msg.args.units", f"expected {ACTION_DIM} entries")
    if len(values) != ACTION_DIM:
        return Failure(Reason.WRONG_DIMENSION, "msg.args.values", f"expected {ACTION_DIM} entries")
    for i in range(ACTION_DIM):
        u = units[i]
        if type(u) is not str:
            return Failure(Reason.BAD_ARGUMENT_TYPE, f"msg.args.units[{i}]", "expected string")
        if u != JOINT_UNITS[i]:
            return Failure(Reason.WRONG_UNITS, f"msg.args.units[{i}]", f"expected unit {JOINT_UNITS[i]}")
    targets = []
    for i in range(ACTION_DIM):
        lo, hi = JOINT_LIMITS[i]
        bad = _number_failure(values[i], lo, hi, f"msg.args.values[{i}]", Reason.BAD_ARGUMENT_TYPE)
        if bad is not None:
            return bad
        targets.append(float(values[i]))  # safe: bounds already checked, so no huge ints reach here
    ctx.out["targets"] = tuple(targets)
    return PASS


def _args_hold(ctx: _Ctx):
    bad = _args_shape(ctx.msg["args"], ())
    if bad is not None:
        return bad
    ctx.out["targets"] = None
    return PASS


_ARG_CHECKS: dict = {"set_joint_targets": _args_set_joint_targets, "hold": _args_hold}


def check_args(ctx: _Ctx):
    fn = _ARG_CHECKS.get(ctx.msg["command"])
    if fn is None:
        return Failure(Reason.UNKNOWN_SKILL, "msg.command", "no argument schema for command")
    return fn(ctx)


def check_epoch_and_sequence(ctx: _Ctx):
    gate, msg = ctx.gate, ctx.msg
    if msg["epoch"] < gate.epoch:
        return Failure(Reason.STALE_EPOCH, "msg.epoch", "epoch is older than the current epoch")
    if msg["epoch"] > gate.epoch:
        return Failure(Reason.EPOCH_NOT_GRANTED, "msg.epoch", "epoch has not been granted by this gate")
    seq = msg["sequence_id"]
    if seq == gate.last_sequence:
        return Failure(Reason.DUPLICATE_SEQUENCE, "msg.sequence_id", "sequence already accepted")
    if seq < gate.last_sequence:
        return Failure(Reason.STALE_SEQUENCE, "msg.sequence_id", "sequence is older than the last accepted")
    return PASS


def check_timeline(ctx: _Ctx):
    msg, now, cfg = ctx.msg, ctx.now, ctx.cfg
    created, expires = msg["created_tick"], msg["expires_tick"]
    if created > now:
        return Failure(Reason.FUTURE_TIMESTAMP, "msg.created_tick", "created in the future")
    if expires <= created or expires - created > cfg.max_message_ttl_ticks:
        return Failure(Reason.INVALID_EXPIRY, "msg.expires_tick", "expiry must be after creation and bounded")
    if now >= expires:
        return Failure(Reason.EXPIRED, "msg.expires_tick", "message expired (expiry tick is exclusive)")
    obs = ctx.gate.lookup_observation(msg["observation_id"])
    if obs is None:
        return Failure(Reason.UNKNOWN_OBSERVATION, "msg.observation_id", "observation is not registered")
    if obs.tick > created:
        return Failure(Reason.INVALID_TIMELINE, "msg.created_tick", "action predates its observation")
    if now - obs.tick >= cfg.observation_ttl_ticks:
        return Failure(Reason.STALE_OBSERVATION, "msg.observation_id", "source observation too old")
    ctx.out["obs"] = obs
    return PASS


def check_trajectory(ctx: _Ctx):
    if ctx.msg["command"] != "set_joint_targets":
        return PASS
    targets, obs = ctx.out["targets"], ctx.out["obs"]
    for i in range(ACTION_DIM):
        if abs(targets[i] - obs.state[i]) > ctx.cfg.max_step[i]:
            return Failure(Reason.STEP_TOO_LARGE, f"msg.args.values[{i}]",
                           "step from the observed state exceeds the per-joint bound")
    return PASS


DEFAULT_CHECKS = (
    ("gate_mode", check_gate_mode),
    ("envelope", check_envelope),
    ("schema", check_schema),
    ("provenance", check_provenance),
    ("domain_and_command", check_domain_and_command),
    ("mode", check_mode),
    ("args", check_args),
    ("epoch_and_sequence", check_epoch_and_sequence),
    ("timeline", check_timeline),
    ("trajectory", check_trajectory),
)


def _no_duplicate_keys(pairs: list) -> dict:
    out: dict = {}
    for k, v in pairs:
        if k in out:
            raise ValueError("duplicate JSON key")
        out[k] = v
    return out


def _no_constants(name: str) -> Any:
    raise ValueError("NaN/Infinity literals are not valid JSON")


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------
class ActionGate:
    """Decides ALLOW / HOLD / REJECT. Starts (and restarts) in HOLD with a NEW
    epoch taken from the durable store; it never serves from an unreadable store
    (it LOCKS instead)."""

    def __init__(self, config: GateConfig, store: Any, *, audit_hook: Optional[Callable] = None,
                 pre_checks: tuple = (), extra_checks: tuple = ()) -> None:
        if not isinstance(config, GateConfig):
            raise ValueError("config must be a GateConfig")
        self.cfg = config
        self._store = store
        self._audit_hook = audit_hook
        # The default checks can only be ADDED to (before or after), never removed through the API.
        self._checks = tuple(pre_checks) + DEFAULT_CHECKS + tuple(extra_checks)
        for item in self._checks:
            if not (type(item) is tuple and len(item) == 2 and type(item[0]) is str and callable(item[1])):
                raise ValueError("checks must be (name, callable) pairs")
        self._mode = Mode.HOLD
        self.hold_cause = "startup"
        self._epoch = 0
        self._last_seq = 0
        self._last_now = 0
        self._last_contact = 0
        self._registry: OrderedDict = OrderedDict()
        self._pending: OrderedDict = OrderedDict()
        self._audit: deque = deque(maxlen=config.audit_capacity)
        self._audit_seq = 0
        self._decision_seq = 0
        self.audit_dropped = 0
        self.audit_hook_failures = 0
        self.observations_evicted = 0  # silent drops are bugs: every eviction is counted
        self.pending_evicted = 0
        self._boot()

    # ---- observable state -------------------------------------------------
    @property
    def mode(self) -> Mode:
        return self._mode

    @property
    def epoch(self) -> int:
        return self._epoch

    @property
    def last_sequence(self) -> int:
        return self._last_seq

    @property
    def audit_log(self) -> list:
        return list(self._audit)

    def snapshot(self) -> tuple:
        """Everything a rejected action must leave unchanged (clock excluded)."""
        return (self._mode, self.hold_cause, self._epoch, self._last_seq, self._last_contact,
                tuple(self._registry), tuple(self._pending))

    # ---- boot / restart ---------------------------------------------------
    def _boot(self) -> None:
        try:
            stored = self._store.load()
        except Exception:
            stored = None
        if not (_is_int(stored) and 0 <= stored < MAX_INT):
            self._lock("epoch store missing or unreadable", 0)
            return
        try:
            self._store.save(stored + 1)  # write-ahead: persist before using
        except Exception:
            self._lock("epoch store not writable", 0)
            return
        self._epoch = stored + 1
        self._note("transition", 0, "boot", f"hold with epoch {self._epoch}")

    def _lock(self, detail: str, now: int) -> None:
        self._mode = Mode.LOCKED
        self.hold_cause = "state_unrecoverable"
        self._note("transition", now, "locked", detail)

    # ---- audit ------------------------------------------------------------
    def _next_audit_id(self) -> int:
        self._audit_seq += 1
        return self._audit_seq

    def _note(self, kind: str, now: int, what: str, detail: str) -> None:
        rec = {"audit_id": self._next_audit_id(), "kind": kind, "tick": now, "what": what,
               "detail": _printable(detail, 120), "mode": self._mode.value, "epoch": self._epoch}
        try:
            self._append(rec)
        except Exception:
            self.audit_hook_failures += 1  # a failing audit hook never changes a gate state change

    def _append(self, rec: dict) -> None:
        if len(self._audit) == self._audit.maxlen:
            self.audit_dropped += 1
        self._audit.append(rec)
        if self._audit_hook is not None:
            self._audit_hook(dict(rec))

    def _enter(self, mode: Mode, cause: str, now: int) -> None:
        old = self._mode
        self._mode = mode
        self.hold_cause = cause
        if mode is not Mode.AUTONOMOUS:
            self._pending.clear()  # decisions issued before a hold/stop/intervention are void
        self._note("transition", now, f"{old.value}->{mode.value}", cause)

    # ---- clock ------------------------------------------------------------
    def _clock_failure(self, now: Any) -> Optional[Failure]:
        if not _is_int(now) or now < 0 or now > MAX_INT:
            return Failure(Reason.INVALID_CLOCK, "now", "tick must be an int in range")
        if now < self._last_now:
            return Failure(Reason.CLOCK_REGRESSION, "now", "logical clock went backwards")
        return None

    def _watchdog(self, now: int) -> None:
        if self._mode is Mode.AUTONOMOUS and now - self._last_contact >= self.cfg.watchdog_ticks:
            self._enter(Mode.HOLD, "watchdog", now)

    def tick(self, now: Any) -> OpResult:
        """Advance the clock and run the watchdog (silence for ``watchdog_ticks`` -> HOLD)."""
        try:
            bad = self._clock_failure(now)
            if bad is not None:
                return OpResult(False, bad.reason, bad.detail)
            self._last_now = now
            self._watchdog(now)
            return OpResult(True)
        except Exception as exc:
            return OpResult(False, Reason.INTERNAL_ERROR, _tname(exc))

    # ---- operator / lifecycle (each returns OpResult, never raises) -------
    def _op(self, now: Any, fn: Callable[[int], OpResult]) -> OpResult:
        try:
            bad = self._clock_failure(now)
            if bad is not None:
                return OpResult(False, bad.reason, bad.detail)
            self._last_now = now
            self._watchdog(now)
            return fn(now)
        except Exception as exc:
            return OpResult(False, Reason.INTERNAL_ERROR, _tname(exc))

    def _bad_transition(self, what: str) -> OpResult:
        return OpResult(False, Reason.INVALID_TRANSITION, f"{what} not allowed from {self._mode.value}")

    def grant_epoch(self, now: Any) -> OpResult:
        """New sender session: persist epoch+1, restart sequence numbering, force HOLD."""
        def run(now: int) -> OpResult:
            if self._mode is Mode.LOCKED:
                return OpResult(False, Reason.GATE_LOCKED, "gate is locked; operator_reset required")
            new = self._epoch + 1
            if new > MAX_INT:
                return OpResult(False, Reason.EPOCH_EXHAUSTED, "epoch counter exhausted")
            try:
                self._store.save(new)
            except Exception:
                return OpResult(False, Reason.EPOCH_PERSIST_FAILED, "epoch not persisted; nothing changed")
            self._epoch = new
            self._last_seq = 0
            if self._mode is Mode.AUTONOMOUS:
                self._enter(Mode.HOLD, "new_epoch", now)
            self._note("operation", now, "grant_epoch", f"epoch {new}")
            return OpResult(True, epoch=new)
        return self._op(now, run)

    def resume(self, now: Any) -> OpResult:
        def run(now: int) -> OpResult:
            if self._mode is not Mode.HOLD:
                return self._bad_transition("resume")
            self._last_contact = now
            self._enter(Mode.AUTONOMOUS, "resume", now)
            return OpResult(True, epoch=self._epoch)
        return self._op(now, run)

    def hold(self, now: Any, cause: str = "operator") -> OpResult:
        def run(now: int) -> OpResult:
            if self._mode is Mode.HOLD:
                return OpResult(True)
            if self._mode is not Mode.AUTONOMOUS:
                return self._bad_transition("hold")
            self._enter(Mode.HOLD, _printable(cause, 32), now)
            return OpResult(True)
        return self._op(now, run)

    def intervene(self, now: Any) -> OpResult:
        def run(now: int) -> OpResult:
            if self._mode is Mode.INTERVENTION:
                return OpResult(True)
            if self._mode not in (Mode.AUTONOMOUS, Mode.HOLD):
                return self._bad_transition("intervene")
            self._enter(Mode.INTERVENTION, "operator_intervention", now)
            return OpResult(True)
        return self._op(now, run)

    def release_intervention(self, now: Any) -> OpResult:
        def run(now: int) -> OpResult:
            if self._mode is not Mode.INTERVENTION:
                return self._bad_transition("release_intervention")
            self._enter(Mode.HOLD, "intervention_released", now)  # never straight to autonomous
            return OpResult(True)
        return self._op(now, run)

    def safe_stop(self, now: Any) -> OpResult:
        def run(now: int) -> OpResult:
            if self._mode is Mode.SAFE_STOP:
                return OpResult(True)
            if self._mode is Mode.LOCKED:
                return self._bad_transition("safe_stop")
            self._enter(Mode.SAFE_STOP, "safe_stop_latched", now)
            return OpResult(True)
        return self._op(now, run)

    def reset_safe_stop(self, now: Any) -> OpResult:
        def run(now: int) -> OpResult:
            if self._mode is not Mode.SAFE_STOP:
                return self._bad_transition("reset_safe_stop")
            self._enter(Mode.HOLD, "safe_stop_reset", now)
            return OpResult(True)
        return self._op(now, run)

    def operator_reset(self, now: Any, epoch_floor: Any) -> OpResult:
        """Leave LOCKED. The operator supplies a floor that must be >= every epoch
        ever used (the gate cannot verify that: residual risk, see THREAT-MODEL)."""
        def run(now: int) -> OpResult:
            if self._mode is not Mode.LOCKED:
                return self._bad_transition("operator_reset")
            if not _is_int(epoch_floor) or epoch_floor < 0 or epoch_floor >= MAX_INT:
                return OpResult(False, Reason.INVALID_ARGUMENT, "epoch_floor must be an int in range")
            try:
                self._store.save(epoch_floor + 1)
            except Exception:
                return OpResult(False, Reason.EPOCH_PERSIST_FAILED, "epoch not persisted; still locked")
            self._epoch = epoch_floor + 1
            self._last_seq = 0
            self._registry.clear()
            self._pending.clear()
            self._enter(Mode.HOLD, "operator_reset", now)
            return OpResult(True, epoch=self._epoch)
        return self._op(now, run)

    # ---- observations -----------------------------------------------------
    def lookup_observation(self, observation_id: str) -> Optional[_Obs]:
        return self._registry.get(observation_id)

    def register_observation(self, obs: Any, now: Any) -> OpResult:
        """Register a source observation: {observation_id, tick, state[6]}.
        Rejects (without changing anything) malformed, future or duplicate ones."""
        def run(now: int) -> OpResult:
            if (type(obs) is not dict or len(obs) > MAX_KEYS or any(type(k) is not str for k in obs)
                    or set(obs) != {"observation_id", "tick", "state"}):
                return OpResult(False, Reason.MALFORMED_MESSAGE, "observation must be an object with exact keys")
            oid, tick, state = obs["observation_id"], obs["tick"], obs["state"]
            if not _ident_ok(oid):
                return OpResult(False, Reason.BAD_IDENTIFIER, "observation_id")
            if not _is_int(tick) or tick < 0 or tick > MAX_INT:
                return OpResult(False, Reason.WRONG_TYPE, "tick must be a non-negative int")
            if tick > now:
                return OpResult(False, Reason.FUTURE_TIMESTAMP, "observation from the future")
            if type(state) is not list or len(state) != ACTION_DIM:
                return OpResult(False, Reason.WRONG_DIMENSION, f"state must be a list of {ACTION_DIM}")
            vals = []
            for i in range(ACTION_DIM):
                lo, hi = JOINT_LIMITS[i]
                bad = _number_failure(state[i], lo, hi, f"state[{i}]", Reason.WRONG_TYPE)
                if bad is not None:
                    return OpResult(False, bad.reason, f"{bad.field}: {bad.detail}")
                vals.append(float(state[i]))
            if oid in self._registry:
                return OpResult(False, Reason.DUPLICATE_OBSERVATION, "observation_id already registered")
            self._registry[oid] = _Obs(tick, tuple(vals))
            while len(self._registry) > self.cfg.observation_registry_size:
                self._registry.popitem(last=False)  # evicted ids then fail closed: UNKNOWN_OBSERVATION
                self.observations_evicted += 1
            return OpResult(True)
        return self._op(now, run)

    # ---- the decision -----------------------------------------------------
    def evaluate_text(self, text: Any, now: Any) -> Decision:
        """Parse JSON text strictly (no duplicate keys, no NaN/Infinity literals, bounded
        size) and evaluate it. Unparseable input is a typed REJECT, never an exception."""
        try:
            if type(text) is not str or len(text) > MAX_TEXT:
                msg: Any = _UNPARSEABLE
            else:
                msg = json.loads(text, object_pairs_hook=_no_duplicate_keys, parse_constant=_no_constants)
        except Exception:  # includes RecursionError and the int-digit limit ValueError
            msg = _UNPARSEABLE
        return self.evaluate(msg, now)

    def evaluate(self, msg: Any, now: Any) -> Decision:
        try:
            return self._evaluate(msg, now)
        except Exception as exc:
            return self._fallback(now, exc)

    def _evaluate(self, msg: Any, now: Any) -> Decision:
        failure = self._clock_failure(now)
        ctx = None
        if failure is None:
            self._last_now = now
            self._watchdog(now)  # lazy: a late message cannot slip past a missed tick()
            ctx = _Ctx(self, msg, now)
            failure = self._run_checks(ctx)
        tick = now if _is_int(now) and now >= 0 else self._last_now
        if failure is not None:
            return self._blocked(msg, tick, failure)
        return self._allowed(msg, tick, ctx)

    def _run_checks(self, ctx: _Ctx) -> Optional[Failure]:
        for name, fn in self._checks:
            try:
                res = fn(ctx)
            except Exception as exc:  # a crashing check blocks; it never allows
                return Failure(Reason.INTERNAL_ERROR, f"check.{_printable(name, 32)}",
                               f"check raised {_tname(exc)}")
            if res is PASS:
                continue
            if type(res) is Failure and type(res.reason) is Reason:
                return res
            return Failure(Reason.CHECK_RETURNED_INVALID, f"check.{_printable(name, 32)}",
                           f"check returned {_tname(res)} instead of PASS or Failure")
        return None

    def _next_decision_id(self) -> int:
        self._decision_seq += 1
        return self._decision_seq

    @staticmethod
    def _describe(msg: Any) -> dict:
        """Sanitised facts about an untrusted message, for the audit record only."""
        out = {"command": None, "source_id": None, "sequence_id": None}
        try:
            if type(msg) is dict:
                c, s, q = msg.get("command"), msg.get("source_id"), msg.get("sequence_id")
                out["command"] = _printable(c) if type(c) is str else None
                out["source_id"] = _printable(s) if type(s) is str else None
                out["sequence_id"] = q if _is_int(q) and 0 <= q <= MAX_INT else None
        except Exception:
            pass
        return out

    def _record(self, kind: str, tick: int, verdict: Verdict, reason: Optional[Reason], field: str,
                detail: str, msg: Any) -> dict:
        d = self._describe(msg)
        return {
            "audit_id": self._next_audit_id(), "kind": kind, "tick": tick, "verdict": verdict.value,
            "reason": reason.value if reason else None,
            "category": REASON_CATEGORY[reason].value if reason else None,
            "field": _printable(field, 80), "detail": _printable(detail, 120),
            "gate_mode": self._mode.value, "epoch": self._epoch, "command": d["command"],
            "source_id": d["source_id"], "sequence_id": d["sequence_id"], "dispatched": False,
        }

    def _blocked(self, msg: Any, tick: int, failure: Failure) -> Decision:
        reason = failure.reason
        verdict = verdict_for(reason)  # only HOLD or REJECT: never ALLOW
        rec = self._record("decision", tick, verdict, reason, failure.field, failure.detail, msg)
        try:
            self._append(rec)
        except Exception:
            self.audit_hook_failures += 1  # already blocked; count the lost audit copy
        return Decision(verdict, reason, REASON_CATEGORY[reason].value, _printable(failure.field, 80),
                        _printable(failure.detail, 120), self._next_decision_id(), tick, rec["audit_id"])

    def _allowed(self, msg: Any, tick: int, ctx: _Ctx) -> Decision:
        m = ctx.msg  # exact, type-checked dict; all values below were validated by the checks
        command = m["command"]
        targets = ctx.out.get("targets")
        if command not in COMMANDS or (command == "set_joint_targets" and
                                       not (type(targets) is tuple and len(targets) == ACTION_DIM)):
            return self._blocked(msg, tick, Failure(Reason.INTERNAL_ERROR, "gate", "no validated targets"))
        rec = self._record("decision", tick, Verdict.ALLOW, None, "", "all checks passed", msg)
        try:
            self._append(rec)  # no audit, no actuation
        except Exception:
            self.audit_hook_failures += 1
            return self._blocked(msg, tick, Failure(Reason.AUDIT_FAILURE, "audit", "audit record not stored"))
        decision = Decision(Verdict.ALLOW, None, None, "", "", self._next_decision_id(), tick,
                            rec["audit_id"], command, targets, m["epoch"], m["sequence_id"],
                            m["expires_tick"])
        # Commit state LAST, only for an allowed action.
        self._last_seq = m["sequence_id"]
        self._last_contact = tick
        self._pending[decision.decision_id] = decision
        while len(self._pending) > self.cfg.pending_capacity:
            self._pending.popitem(last=False)  # an evicted decision can no longer be dispatched
            self.pending_evicted += 1
        return decision

    def _fallback(self, now: Any, exc: BaseException) -> Decision:
        """Last resort if the gate's own bookkeeping failed: a constant REJECT."""
        tick = now if _is_int(now) and now >= 0 else 0
        try:
            detail = f"gate error {_tname(exc)}"
        except Exception:
            detail = "gate error"
        return Decision(Verdict.REJECT, Reason.INTERNAL_ERROR, Category.GATE_FAULT.value, "gate",
                        detail, -1, tick, -1)

    # ---- dispatch confirmation (used by the sink) ---------------------------
    def confirm_dispatch(self, decision: Any) -> tuple:
        """Single-use: True only for an ALLOW decision this gate issued, not yet used, whose
        epoch is current, while the gate is still AUTONOMOUS. Returns (ok, why)."""
        try:
            if type(decision) is not Decision:
                return (False, "not_a_decision")
            issued = self._pending.get(decision.decision_id)
            if issued is None or issued is not decision:
                return (False, "decision_not_pending")  # forged, copied, foreign or already used
            del self._pending[decision.decision_id]  # single use, consumed even if refused below
            if self._mode is not Mode.AUTONOMOUS:
                return (False, "gate_not_autonomous")
            if decision.epoch != self._epoch:
                return (False, "epoch_changed")
            return (True, "ok")
        except Exception:
            return (False, "confirm_error")
