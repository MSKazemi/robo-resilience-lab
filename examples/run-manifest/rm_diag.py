"""Diagnostics, strict JSON loading and typed field helpers (stdlib only).

Design rules (each has a regression test):

* Helpers check the *type first* and never raise: a list/dict/bool where a
  scalar belongs yields a typed diagnostic, never TypeError/KeyError.
* ``bool`` is rejected where a number is required.
* Regexes use ``fullmatch`` and explicit ASCII classes (no ``$``, no ``\\d``).
* Values are echoed with ``ascii()`` and truncated, so a hostile string cannot
  break the terminal or crash ``print`` (lone surrogates, control characters).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import rm_contract as c


@dataclass(frozen=True)
class Diagnostic:
    code: str
    path: str
    message: str
    hint: str
    severity: str = "error"

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity,
            "path": self.path,
            "message": self.message,
            "hint": self.hint,
        }


@dataclass(frozen=True)
class Result:
    diagnostics: tuple
    truncated: bool = False

    @property
    def errors(self) -> tuple:
        return tuple(d for d in self.diagnostics if d.severity == "error")

    @property
    def valid(self) -> bool:
        return not self.errors

    @property
    def codes(self) -> list:
        return [d.code for d in self.diagnostics]


def ptr(base: str, *parts: Any) -> str:
    """RFC 6901 JSON pointer; the document root is the empty string."""
    out = base
    for p in parts:
        out += "/" + str(p).replace("~", "~0").replace("/", "~1")
    return out


def show(v: Any, limit: int = 60) -> str:
    """Safe, bounded, ASCII rendering of an untrusted value for messages."""
    try:
        s = ascii(v)
    except Exception:  # pragma: no cover - ascii() on exotic objects
        s = "<unprintable>"
    return s if len(s) <= limit else s[: limit - 3] + "..."


def type_name(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return type(v).__name__


class Collector:
    def __init__(self) -> None:
        self.items: list = []
        self.truncated = False

    def add(self, code: str, path: str, message: str, hint: Optional[str] = None) -> None:
        if len(self.items) >= c.MAX_DIAGNOSTICS:
            self.truncated = True
            return
        sev = "warning" if code in c.WARNING_CODES else "error"
        self.items.append(Diagnostic(code, path, message, hint or c.hint_for(code), sev))

    def result(self) -> Result:
        return Result(tuple(self.items), self.truncated)


# --------------------------------------------------------------------------- JSON


class DupDict(dict):
    """dict that remembers keys repeated in the source text."""

    def __init__(self) -> None:
        super().__init__()
        self.dups: list = []


def _pairs_hook(pairs: list) -> DupDict:
    d = DupDict()
    for k, v in pairs:
        if k in d:
            d.dups.append(k)
        else:
            d[k] = v
    return d


def _constant(name: str) -> float:
    # Keep NaN/Infinity as floats; the structural scan reports them with a path.
    return float(name)  # "NaN", "Infinity", "-Infinity"


def parse_json_text(text: str) -> tuple:
    """Return ``(doc, diagnostics)``; never raises. ``doc`` is None on failure."""
    if len(text.encode("utf-8", "replace")) > c.MAX_FILE_BYTES:
        return None, [Diagnostic("FILE_TOO_LARGE", "", f"more than {c.MAX_FILE_BYTES} bytes", c.hint_for("FILE_TOO_LARGE"))]
    try:
        return json.loads(text, object_pairs_hook=_pairs_hook, parse_constant=_constant), []
    except json.JSONDecodeError as exc:
        msg = f"line {exc.lineno} column {exc.colno}: {exc.msg}"
    except RecursionError:
        return None, [Diagnostic("NESTING_TOO_DEEP", "", "nesting too deep to parse", c.hint_for("NESTING_TOO_DEEP"))]
    except ValueError as exc:  # e.g. an integer literal with thousands of digits
        msg = f"unparseable value: {show(str(exc))}"
    return None, [Diagnostic("MALFORMED_JSON", "", msg, c.hint_for("MALFORMED_JSON"))]


def read_json_file(path: str) -> tuple:
    """Read + parse one file. Returns ``(doc, diagnostics)``; never raises."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(c.MAX_FILE_BYTES + 1)
    except OSError as exc:
        return None, [Diagnostic("FILE_UNREADABLE", "", f"{type(exc).__name__}: {show(exc.strerror or str(exc))}", c.hint_for("FILE_UNREADABLE"))]
    if len(raw) > c.MAX_FILE_BYTES:
        return None, [Diagnostic("FILE_TOO_LARGE", "", f"more than {c.MAX_FILE_BYTES} bytes", c.hint_for("FILE_TOO_LARGE"))]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return None, [Diagnostic("MALFORMED_JSON", "", f"not UTF-8: {show(exc.reason)}", c.hint_for("MALFORMED_JSON"))]
    return parse_json_text(text)


# --------------------------------------------------------------------------- scalar predicates


def is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def finite(v: Any) -> bool:
    """True for a finite real; huge ints that overflow float count as finite ints."""
    if isinstance(v, int) and not isinstance(v, bool):
        return True
    try:
        return math.isfinite(v)
    except (OverflowError, TypeError):
        return False


# --------------------------------------------------------------------------- typed field helpers
# Each helper returns the validated value or None (after recording diagnostics).


def get_obj(ctx: Collector, v: Any, path: str, required=(), optional=(), missing_codes=None, unknown_code="UNKNOWN_FIELD") -> Optional[dict]:
    if not isinstance(v, dict):
        ctx.add("WRONG_TYPE", path, f"expected an object, got {type_name(v)}")
        return None
    allowed = set(required) | set(optional)
    for k in v:
        if not isinstance(k, str):
            ctx.add("WRONG_TYPE", path, f"object key {show(k)} is not a string")
        elif k not in allowed and not k.startswith("x_"):
            ctx.add(unknown_code, ptr(path, k), f"{show(k)} is not part of the contract")
    mc = missing_codes or {}
    for k in required:
        if k not in v:
            ctx.add(mc.get(k, "MISSING_FIELD"), ptr(path, k), f"required field {k!r} is absent")
    return v


def get_str(ctx: Collector, v: Any, path: str, pattern=None, bad_code="STRING_INVALID", what="value") -> Optional[str]:
    if not isinstance(v, str):
        ctx.add("WRONG_TYPE", path, f"{what} must be a string, got {type_name(v)} {show(v)}")
        return None
    if v == "":
        ctx.add("EMPTY_STRING", path, f"{what} must not be empty")
        return None
    if len(v) > c.MAX_TEXT:
        ctx.add("STRING_TOO_LONG", path, f"{what} is {len(v)} characters; the limit is {c.MAX_TEXT}")
        return None
    if pattern is not None and pattern.fullmatch(v) is None:
        ctx.add(bad_code, path, f"{what} {show(v)} does not match the required format")
        return None
    return v


def get_id(ctx: Collector, v: Any, path: str, what="identifier", placeholder_check=False) -> Optional[str]:
    s = get_str(ctx, v, path, c.ID_RE, "ID_MALFORMED", what)
    if s is not None and placeholder_check and s.lower() in c.PLACEHOLDER_IDS:
        ctx.add("PLACEHOLDER_VALUE", path, f"{what} {show(s)} is a placeholder")
        return None
    return s


def get_version(ctx: Collector, v: Any, path: str, what="version", pattern=None) -> Optional[str]:
    return get_str(ctx, v, path, pattern or c.VERSION_RE, "VERSION_MALFORMED", what)


def get_text(ctx: Collector, v: Any, path: str, what="text") -> Optional[str]:
    return get_str(ctx, v, path, c.TEXT_RE, "STRING_INVALID", what)


def get_enum(ctx: Collector, v: Any, path: str, allowed: tuple, what="value") -> Optional[str]:
    if not isinstance(v, str):  # check the type BEFORE `in` (a list would be unhashable)
        ctx.add("WRONG_TYPE", path, f"{what} must be a string, got {type_name(v)} {show(v)}")
        return None
    if v not in allowed:
        ctx.add("ENUM_INVALID", path, f"{what} {show(v)} is not one of {list(allowed)}")
        return None
    return v


def get_bool(ctx: Collector, v: Any, path: str, what="value") -> Optional[bool]:
    if not isinstance(v, bool):
        ctx.add("WRONG_TYPE", path, f"{what} must be true or false, got {type_name(v)} {show(v)}")
        return None
    return v


def get_int(ctx: Collector, v: Any, path: str, lo: int, hi: int, what="value") -> Optional[int]:
    if isinstance(v, bool):
        ctx.add("BOOL_AS_NUMBER", path, f"{what} must be an integer, got boolean {v}")
        return None
    if isinstance(v, float):
        if not math.isfinite(v):
            return None  # reported once by the structural scan (NON_FINITE_VALUE)
        ctx.add("WRONG_TYPE", path, f"{what} must be an integer, got number {show(v)}")
        return None
    if not isinstance(v, int):
        ctx.add("WRONG_TYPE", path, f"{what} must be an integer, got {type_name(v)} {show(v)}")
        return None
    if v < lo:
        ctx.add("NEGATIVE_VALUE" if v < 0 <= lo else "VALUE_OUT_OF_RANGE", path, f"{what} {show(v)} is below the minimum {lo}")
        return None
    if v > hi:
        ctx.add("VALUE_OUT_OF_RANGE", path, f"{what} is above the maximum {hi}")
        return None
    return v


def get_number(ctx: Collector, v: Any, path: str, what="value", positive=False, hi=c.MAX_QUANTITY) -> Optional[float]:
    if isinstance(v, bool):
        ctx.add("BOOL_AS_NUMBER", path, f"{what} must be a number, got boolean {v}")
        return None
    if not isinstance(v, (int, float)):
        ctx.add("WRONG_TYPE", path, f"{what} must be a number, got {type_name(v)} {show(v)}")
        return None
    if isinstance(v, float) and not math.isfinite(v):
        return None  # reported once by the structural scan (NON_FINITE_VALUE)
    try:
        f = float(v)
    except OverflowError:
        ctx.add("VALUE_OUT_OF_RANGE", path, f"{what} is too large")
        return None
    if f < 0:
        ctx.add("NEGATIVE_VALUE", path, f"{what} {show(v)} is negative")
        return None
    if f == 0 and positive:
        ctx.add("NON_POSITIVE_VALUE", path, f"{what} must be > 0")
        return None
    if f > hi:
        ctx.add("VALUE_OUT_OF_RANGE", path, f"{what} {show(v)} is above the maximum {hi:g}")
        return None
    return f


def get_hex(ctx: Collector, v: Any, path: str, pattern, what: str, prefix: str = "") -> Optional[str]:
    """Lowercase-hex identifier of fixed length; placeholders (repeating patterns) rejected."""
    s = get_str(ctx, v, path, pattern, "HASH_MALFORMED", what)
    if s is None:
        return None
    body = s[len(prefix):]
    for period in (1, 2, 4, 8):
        if len(body) % period == 0 and body == body[:period] * (len(body) // period):
            ctx.add("PLACEHOLDER_VALUE", path, f"{what} is a repeating pattern, not a real hash")
            return None
    return s


def get_utc(ctx: Collector, v: Any, path: str, what="timestamp") -> Optional[datetime]:
    if not isinstance(v, str):
        ctx.add("WRONG_TYPE", path, f"{what} must be a string, got {type_name(v)} {show(v)}")
        return None
    m = c.TS_RE.fullmatch(v)
    if m is None:
        if c.TS_OTHER_ZONE_RE.fullmatch(v) is not None:
            ctx.add("TIMESTAMP_NOT_UTC", path, f"{what} {show(v)} is not marked UTC with 'Z'")
        else:
            ctx.add("TIMESTAMP_MALFORMED", path, f"{what} {show(v)} is not YYYY-MM-DDTHH:MM:SS[.f]Z")
        return None
    y, mo, d, h, mi, s = (int(m.group(i)) for i in range(1, 7))
    frac = m.group(7)
    us = int((frac[1:] + "000000")[:6]) if frac else 0
    try:
        if not 2000 <= y <= 2200:
            raise ValueError("year")
        return datetime(y, mo, d, h, mi, s, us, tzinfo=timezone.utc)
    except ValueError:
        ctx.add("TIMESTAMP_MALFORMED", path, f"{what} {show(v)} is not a real calendar time (year 2000..2200)")
        return None


def get_quantity(ctx: Collector, v: Any, path: str, kind: str, positive=False, what="quantity", ratio_max=None) -> Optional[float]:
    """``{"value": number, "unit": str}``. Returns the value in the base unit.

    A bare number is the typical mistake: it gets ``UNIT_MISSING``.
    """
    units = c.UNITS[kind]
    if is_num(v):
        ctx.add("UNIT_MISSING", path, f"{what} is a bare number; use {{\"value\": x, \"unit\": one of {list(units)}}}")
        return None
    obj = get_obj(ctx, v, path, required=("value", "unit"), missing_codes={"unit": "UNIT_MISSING"})
    if obj is None:
        return None
    unit = None
    if "unit" in obj:
        raw = obj["unit"]
        if not isinstance(raw, str):
            ctx.add("WRONG_TYPE", ptr(path, "unit"), f"unit must be a string, got {type_name(raw)} {show(raw)}")
        elif raw not in units:
            ctx.add("UNIT_NOT_ALLOWED", ptr(path, "unit"), f"unit {show(raw)} is not valid for a {kind} quantity; use one of {list(units)}")
        else:
            unit = raw
    val = None
    if "value" in obj:
        val = get_number(ctx, obj["value"], ptr(path, "value"), what=f"{what} value", positive=positive)
    if val is None or unit is None:
        return None
    base = val * units[unit]
    if ratio_max is not None and base > ratio_max:
        ctx.add("VALUE_OUT_OF_RANGE", ptr(path, "value"), f"{what} {show(val)} {unit} exceeds the maximum of a ratio")
        return None
    return base
