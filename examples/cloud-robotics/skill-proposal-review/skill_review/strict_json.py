"""Strict parser for one JSON proposal object.

Uses only ``json.loads`` with hooks (no ``eval``, no YAML, no regular-expression
"extraction" of JSON out of surrounding text). The whole input must be exactly
one JSON object; anything else is a typed error, never a best-effort guess.

Rejected: duplicate keys (after unescaping), NaN/Infinity, overflow to
infinity, integers longer than 18 digits, nesting deeper than ``MAX_DEPTH``,
inputs larger than ``MAX_INPUT_BYTES``, invalid UTF-8, lone surrogates in the
raw text, a leading BOM, any text before or after the object.
"""

import json
import math

MAX_INPUT_BYTES = 4096
MAX_INT_DIGITS = 18
MAX_FLOAT_CHARS = 32
# envelope (1) -> args (2) -> one more container so that a list/dict given as
# an argument value still gets a precise WRONG_TYPE diagnostic.
MAX_DEPTH = 3


class StrictJSONError(Exception):
    """Typed parse failure. ``code`` is one of the reviewer's reason codes."""

    def __init__(self, code, detail):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _safe(text, limit=40):
    return ascii(text)[:limit]


def _pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise StrictJSONError("DUPLICATE_KEY", f"duplicate object key {_safe(key)}")
        out[key] = value
    return out


def _parse_constant(name):
    raise StrictJSONError("NON_FINITE_NUMBER", f"{name} is not allowed")


def _parse_float(text):
    if len(text) > MAX_FLOAT_CHARS:
        raise StrictJSONError("NUMBER_TOO_LARGE", "number literal too long")
    value = float(text)
    if not math.isfinite(value):
        raise StrictJSONError("NON_FINITE_NUMBER", "number overflows to infinity")
    return value


def _parse_int(text):
    if len(text.lstrip("-")) > MAX_INT_DIGITS:
        raise StrictJSONError("NUMBER_TOO_LARGE", f"integer longer than {MAX_INT_DIGITS} digits")
    return int(text)


def max_depth(obj):
    """Iterative container depth (no recursion, so deep input cannot crash it)."""
    deepest = 0
    stack = [(obj, 1)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, dict):
            deepest = max(deepest, depth)
            stack.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            deepest = max(deepest, depth)
            stack.extend((v, depth + 1) for v in item)
    return deepest


def to_text(data):
    """Return validated text for ``data`` (str or bytes) or raise StrictJSONError."""
    kind = type(data)  # exact types only: subclasses could override behaviour
    if kind is bytes:
        if len(data) > MAX_INPUT_BYTES:
            raise StrictJSONError("INPUT_TOO_LARGE", f"more than {MAX_INPUT_BYTES} bytes")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            raise StrictJSONError("INVALID_ENCODING", "input is not valid UTF-8") from None
    if kind is str:
        if len(data) > MAX_INPUT_BYTES:  # cheap cap before encoding anything large
            raise StrictJSONError("INPUT_TOO_LARGE", f"more than {MAX_INPUT_BYTES} characters")
        try:
            encoded = data.encode("utf-8")
        except UnicodeEncodeError:
            raise StrictJSONError("INVALID_ENCODING", "text holds lone surrogates") from None
        if len(encoded) > MAX_INPUT_BYTES:
            raise StrictJSONError("INPUT_TOO_LARGE", f"more than {MAX_INPUT_BYTES} bytes")
        return data
    raise StrictJSONError("INPUT_NOT_TEXT", f"expected str or bytes, got {kind.__name__}")


def parse_object(data):
    """Parse ``data`` into exactly one JSON object (a dict) or raise StrictJSONError."""
    text = to_text(data)
    if text.strip(" \t\n\r") == "":
        raise StrictJSONError("EMPTY_INPUT", "no content")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_pairs,
            parse_float=_parse_float,
            parse_int=_parse_int,
            parse_constant=_parse_constant,
        )
    except StrictJSONError:
        raise
    except RecursionError:
        raise StrictJSONError("NESTING_TOO_DEEP", "nesting exceeds the parser limit") from None
    except json.JSONDecodeError as exc:
        raise StrictJSONError("NOT_STRICT_JSON", f"{exc.msg} at position {exc.pos}") from None
    except ValueError as exc:
        raise StrictJSONError("NOT_STRICT_JSON", type(exc).__name__) from None
    if max_depth(value) > MAX_DEPTH:  # before the type check: deep input is reported as deep
        raise StrictJSONError("NESTING_TOO_DEEP", f"nesting deeper than {MAX_DEPTH}")
    if type(value) is not dict:
        raise StrictJSONError("NOT_A_JSON_OBJECT", f"top level is {type(value).__name__}")
    return value
