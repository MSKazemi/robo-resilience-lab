"""Pin-file contract and validator for immutable Hugging Face revisions (``hf-revision-pins/v1``).

Python standard library only. No network, no ``huggingface_hub`` import.

A pin file is a JSON object::

    {
      "schema_version": "hf-revision-pins/v1",
      "data_kind": "synthetic" | "real",
      "description": "optional free text",
      "pins": [
        {
          "id": "policy",                       # local alias, unique in the file
          "repo_id": "namespace/name",          # Hugging Face repo id
          "kind": "model" | "dataset",
          "revision": "<40 lowercase hex>",     # full commit hash, never a branch/tag
          "license": "apache-2.0",              # license identifier as declared by the repo
          "gated": false,                       # optional; true = needs accepted terms + token to prefetch
          "files": [                            # optional (required by release mode)
            {"path": "config.json", "sha256": "<64 lowercase hex>",
             "size": 123,                       # optional, bytes
             "role": "config"}                  # optional, see FILE_ROLES
          ]
        }
      ]
    }

The validator never raises on bad input: every problem becomes a ``Diagnostic`` with a
stable code. ``validate_document`` keeps going after the first problem.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field

SCHEMA_VERSION = "hf-revision-pins/v1"
DATA_KINDS = ("synthetic", "real")
KINDS = ("model", "dataset")
FILE_ROLES = ("weights", "config", "processor", "tokenizer", "data", "metadata", "other")
MAX_PIN_FILE_BYTES = 1024 * 1024
MAX_PINS = 256
MAX_FILES_PER_PIN = 4096
MAX_DIAGNOSTICS = 200
TOP_KEYS = {"schema_version", "data_kind", "description", "pins"}
PIN_KEYS = {"id", "repo_id", "kind", "revision", "license", "gated", "files"}
FILE_KEYS = {"path", "sha256", "size", "role"}

# huggingface_hub treats exactly 40 lowercase hex characters as a commit hash
# (REGEX_COMMIT_HASH in huggingface_hub/file_download.py). Anything else is a ref name
# that the Hub resolves at download time, i.e. it can move.
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
HEX_RE = re.compile(r"[0-9a-fA-F]+")
# Conservative copy of huggingface_hub.utils.validate_repo_id (ASCII only, optional
# namespace, 1-96 chars per part, no leading/trailing '-' or '.', no '--' or '..', no '.git').
_REPO_PART = r"[A-Za-z0-9_](?:[A-Za-z0-9_.\-]{0,94}[A-Za-z0-9_])?"
REPO_ID_RE = re.compile(r"(?:%s/)?%s" % (_REPO_PART, _REPO_PART))
PIN_ID_RE = re.compile(r"[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?")
LICENSE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+\-]{0,63}")
UNACCEPTABLE_LICENSES = {"unknown", "none", "n/a", "na", "tbd", "todo", "unlicensed"}
COMMON_BRANCHES = {"main", "master", "dev", "develop", "head", "latest", "stable", "release", "trunk"}

CODES = {
    "PARSE_ERROR": "the file is not strict JSON (also: duplicate keys, NaN/Infinity, too large, not UTF-8)",
    "TYPE_ERROR": "a field has the wrong JSON type",
    "MISSING_FIELD": "a required field is absent",
    "UNKNOWN_FIELD": "a field is not part of hf-revision-pins/v1 (typo guard)",
    "SCHEMA_VERSION": "schema_version is not hf-revision-pins/v1",
    "DATA_KIND": "data_kind is not 'synthetic' or 'real'",
    "NO_PINS": "pins is empty",
    "TOO_MANY": "more pins or files than the validator accepts",
    "PIN_ID": "pin id is not a lowercase slug",
    "DUPLICATE_PIN_ID": "two pins share an id",
    "DUPLICATE_REPO": "the same (kind, repo_id) is pinned twice",
    "REPO_ID": "repo_id is not a valid Hugging Face repo id",
    "KIND": "kind is not 'model' or 'dataset'",
    "REVISION_MISSING": "revision is absent or empty",
    "REVISION_BRANCH": "revision is a branch name (mutable)",
    "REVISION_PR_REF": "revision is a refs/pr/N pull-request ref (mutable)",
    "REVISION_SHORT_SHA": "revision is an abbreviated commit hash",
    "REVISION_UPPERCASE": "revision is 40 hex characters but not lowercase",
    "REVISION_NOT_COMMIT": "revision is a tag or other ref name (mutable), not a 40-hex commit hash",
    "LICENSE": "license is missing, malformed or a placeholder such as 'unknown'",
    "FILES_EMPTY": "files is present but empty",
    "FILE_PATH": "file path is not a safe relative POSIX path",
    "DUPLICATE_FILE": "the same file path is listed twice in one pin",
    "SHA256": "sha256 is not 64 lowercase hex characters",
    "SIZE": "size is not a non-negative integer",
    "ROLE": "role is not one of the documented roles",
    "RELEASE_NO_FILE_HASHES": "release mode: a pin lists no file sha256 values",
    "RELEASE_SYNTHETIC": "release mode: data_kind is 'synthetic'",
    "TRUNCATED": "too many diagnostics; the rest were not reported",
    "INTERNAL_ERROR": "the validator hit an unexpected error (treated as invalid)",
}


@dataclass(frozen=True)
class Diagnostic:
    code: str
    path: str
    message: str

    def render(self) -> str:
        return "[%s] %s: %s" % (self.code, self.path or "/", self.message)


@dataclass
class Result:
    diagnostics: list = field(default_factory=list)
    document: object = None

    @property
    def ok(self) -> bool:
        return not self.diagnostics

    @property
    def codes(self) -> list:
        return [d.code for d in self.diagnostics]


class _Sink:
    def __init__(self):
        self.items = []
        self.full = False

    def add(self, code, path, message):
        if self.full:
            return
        if len(self.items) >= MAX_DIAGNOSTICS:
            self.items.append(Diagnostic("TRUNCATED", "", "stopped after %d diagnostics" % MAX_DIAGNOSTICS))
            self.full = True
            return
        self.items.append(Diagnostic(code, path, message))


def _ptr(*parts) -> str:
    out = ""
    for p in parts:
        out += "/" + str(p).replace("~", "~0").replace("/", "~1")
    return out


def _is_str(v) -> bool:
    return isinstance(v, str)


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def classify_revision(rev):
    """Return ``None`` if ``rev`` is an immutable full commit hash, else a (code, message) pair."""
    if not _is_str(rev) or rev == "":
        return ("REVISION_MISSING", "revision must be a full 40-character lowercase commit hash")
    if COMMIT_RE.fullmatch(rev):
        return None
    low = rev.lower()
    if HEX_RE.fullmatch(rev) and len(rev) == 40:
        return ("REVISION_UPPERCASE", "commit hashes are lowercase on the Hub and in the cache; write %r" % low)
    if low.startswith("refs/pr/"):
        return ("REVISION_PR_REF", "%r is a pull-request ref and moves when the PR is updated" % rev)
    if low.startswith("refs/heads/") or low in COMMON_BRANCHES:
        return ("REVISION_BRANCH", "%r is a branch and moves on every push; pin the commit hash instead" % rev)
    if HEX_RE.fullmatch(rev) and len(rev) < 40:
        return ("REVISION_SHORT_SHA", "%r is an abbreviated hash (%d chars); write all 40 characters" % (rev, len(rev)))
    return ("REVISION_NOT_COMMIT", "%r is not a 40-hex commit hash; tags and branch names can be moved" % rev)


def _safe_rel_path(p) -> str | None:
    """Return an error message if ``p`` is not a safe relative POSIX path, else None."""
    if not _is_str(p) or p == "":
        return "path must be a non-empty string"
    if len(p) > 1024:
        return "path is longer than 1024 characters"
    if "\\" in p or "\x00" in p or any(ord(ch) < 32 for ch in p):
        return "path contains a backslash or control character"
    if p.startswith("/") or re.match(r"[A-Za-z]:", p):
        return "path must be relative"
    parts = p.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            return "path has an empty, '.' or '..' segment"
    return None


def _strict_pairs(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError("duplicate key %r" % k)
        out[k] = v
    return out


def _reject_constant(name):
    raise ValueError("non-finite number %s is not allowed" % name)


def parse_text(text):
    """Parse strict JSON: duplicate keys and NaN/Infinity are errors. Returns (doc, error)."""
    try:
        doc = json.loads(text, object_pairs_hook=_strict_pairs, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as exc:
        return None, str(exc)
    return doc, None


def _check_float_free(node, sink, path=""):
    if isinstance(node, float) and not math.isfinite(node):
        sink.add("TYPE_ERROR", path, "non-finite number")
    elif isinstance(node, dict):
        for k, v in node.items():
            _check_float_free(v, sink, path + _ptr(k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _check_float_free(v, sink, path + _ptr(i))


def _validate_file_entry(entry, base, sink, seen_paths):
    if not isinstance(entry, dict):
        sink.add("TYPE_ERROR", base, "a file entry must be an object")
        return
    for k in sorted(set(entry) - FILE_KEYS):
        sink.add("UNKNOWN_FIELD", base + _ptr(k), "unknown field %r" % k)
    if "path" not in entry:
        sink.add("MISSING_FIELD", base + _ptr("path"), "required field 'path' is absent")
    else:
        err = _safe_rel_path(entry["path"])
        if err:
            sink.add("FILE_PATH", base + _ptr("path"), err)
        elif entry["path"] in seen_paths:
            sink.add("DUPLICATE_FILE", base + _ptr("path"), "%r is listed twice" % entry["path"])
        else:
            seen_paths.add(entry["path"])
    if "sha256" not in entry:
        sink.add("MISSING_FIELD", base + _ptr("sha256"), "required field 'sha256' is absent")
    elif not (_is_str(entry["sha256"]) and SHA256_RE.fullmatch(entry["sha256"])):
        sink.add("SHA256", base + _ptr("sha256"), "expected 64 lowercase hex characters")
    if "size" in entry and not (_is_int(entry["size"]) and entry["size"] >= 0):
        sink.add("SIZE", base + _ptr("size"), "size must be a non-negative integer (bytes)")
    if "role" in entry and entry["role"] not in FILE_ROLES:
        sink.add("ROLE", base + _ptr("role"), "role must be one of %s" % ", ".join(FILE_ROLES))


def _validate_pin(pin, i, sink, seen_ids, seen_repos, release):
    base = _ptr("pins", i)
    if not isinstance(pin, dict):
        sink.add("TYPE_ERROR", base, "a pin must be an object")
        return
    for k in sorted(set(pin) - PIN_KEYS):
        sink.add("UNKNOWN_FIELD", base + _ptr(k), "unknown field %r" % k)
    for k in ("id", "repo_id", "kind", "revision", "license"):
        if k not in pin:
            code = "REVISION_MISSING" if k == "revision" else "MISSING_FIELD"
            sink.add(code, base + _ptr(k), "required field %r is absent" % k)

    pid = pin.get("id")
    if "id" in pin:
        if not (_is_str(pid) and PIN_ID_RE.fullmatch(pid)):
            sink.add("PIN_ID", base + _ptr("id"), "id must be a lowercase slug (a-z, 0-9, '-', '_'), max 64")
        elif pid in seen_ids:
            sink.add("DUPLICATE_PIN_ID", base + _ptr("id"), "id %r is used twice" % pid)
        else:
            seen_ids.add(pid)

    repo_ok = False
    if "repo_id" in pin:
        rid = pin["repo_id"]
        if not _is_str(rid):
            sink.add("TYPE_ERROR", base + _ptr("repo_id"), "repo_id must be a string")
        elif not REPO_ID_RE.fullmatch(rid) or "--" in rid or ".." in rid or rid.endswith(".git"):
            sink.add("REPO_ID", base + _ptr("repo_id"),
                     "%r is not 'name' or 'namespace/name' (ASCII letters, digits, '-', '_', '.'; "
                     "no '--', '..', '.git' suffix or leading/trailing '-'/'.')" % rid)
        else:
            repo_ok = True

    kind_ok = False
    if "kind" in pin:
        if pin["kind"] not in KINDS or not _is_str(pin["kind"]):
            sink.add("KIND", base + _ptr("kind"), "kind must be 'model' or 'dataset'")
        else:
            kind_ok = True
    if repo_ok and kind_ok:
        key = (pin["kind"], pin["repo_id"])
        if key in seen_repos:
            sink.add("DUPLICATE_REPO", base + _ptr("repo_id"),
                     "%s %r is already pinned by %r; one revision per repo" % (key[0], key[1], seen_repos[key]))
        else:
            seen_repos[key] = pid

    if "revision" in pin:
        bad = classify_revision(pin["revision"])
        if bad:
            sink.add(bad[0], base + _ptr("revision"), bad[1])

    if "license" in pin:
        lic = pin["license"]
        if not (_is_str(lic) and LICENSE_RE.fullmatch(lic)) or lic.lower() in UNACCEPTABLE_LICENSES:
            sink.add("LICENSE", base + _ptr("license"),
                     "write the license identifier the repo declares (e.g. 'apache-2.0', 'mit', 'cc-by-4.0'); "
                     "placeholders such as 'unknown' are rejected")

    if "gated" in pin and not isinstance(pin["gated"], bool):
        sink.add("TYPE_ERROR", base + _ptr("gated"), "gated must be true or false")

    files = pin.get("files")
    if "files" in pin:
        if not isinstance(files, list):
            sink.add("TYPE_ERROR", base + _ptr("files"), "files must be an array")
            files = None
        elif not files:
            sink.add("FILES_EMPTY", base + _ptr("files"), "omit 'files' or list at least one file")
        elif len(files) > MAX_FILES_PER_PIN:
            sink.add("TOO_MANY", base + _ptr("files"), "more than %d files" % MAX_FILES_PER_PIN)
            files = None
        else:
            seen_paths = set()
            for j, entry in enumerate(files):
                _validate_file_entry(entry, base + _ptr("files", j), sink, seen_paths)
    if release and not (isinstance(files, list) and files):
        sink.add("RELEASE_NO_FILE_HASHES", base + _ptr("files"),
                 "release mode needs at least one file with a sha256 per pin")


def _validate(doc, sink, release):
    if not isinstance(doc, dict):
        sink.add("TYPE_ERROR", "", "the document must be a JSON object")
        return
    _check_float_free(doc, sink)
    for k in sorted(set(doc) - TOP_KEYS):
        sink.add("UNKNOWN_FIELD", _ptr(k), "unknown field %r" % k)
    if "schema_version" not in doc:
        sink.add("MISSING_FIELD", "/schema_version", "required field 'schema_version' is absent")
    elif doc["schema_version"] != SCHEMA_VERSION:
        sink.add("SCHEMA_VERSION", "/schema_version", "expected %r" % SCHEMA_VERSION)
    if "data_kind" not in doc:
        sink.add("MISSING_FIELD", "/data_kind", "required field 'data_kind' is absent")
    elif doc["data_kind"] not in DATA_KINDS or not _is_str(doc["data_kind"]):
        sink.add("DATA_KIND", "/data_kind", "data_kind must be 'synthetic' or 'real'")
    elif release and doc["data_kind"] == "synthetic":
        sink.add("RELEASE_SYNTHETIC", "/data_kind", "synthetic fixtures cannot pass a release-grade check")
    if "description" in doc and not _is_str(doc["description"]):
        sink.add("TYPE_ERROR", "/description", "description must be a string")
    if "pins" not in doc:
        sink.add("MISSING_FIELD", "/pins", "required field 'pins' is absent")
        return
    pins = doc["pins"]
    if not isinstance(pins, list):
        sink.add("TYPE_ERROR", "/pins", "pins must be an array")
        return
    if not pins:
        sink.add("NO_PINS", "/pins", "list at least one pin")
        return
    if len(pins) > MAX_PINS:
        sink.add("TOO_MANY", "/pins", "more than %d pins" % MAX_PINS)
        return
    seen_ids, seen_repos = set(), {}
    for i, pin in enumerate(pins):
        _validate_pin(pin, i, sink, seen_ids, seen_repos, release)


def validate_document(doc, release=False) -> Result:
    """Validate an already-parsed document. Never raises."""
    sink = _Sink()
    try:
        _validate(doc, sink, release)
    except Exception as exc:  # noqa: BLE001 - fail closed on any validator bug
        sink.add("INTERNAL_ERROR", "", "%s: %s" % (type(exc).__name__, exc))
    return Result(diagnostics=sink.items, document=doc)


def validate_text(text, release=False) -> Result:
    if isinstance(text, bytes):
        if len(text) > MAX_PIN_FILE_BYTES:
            return Result([Diagnostic("PARSE_ERROR", "", "pin file is larger than %d bytes" % MAX_PIN_FILE_BYTES)])
        try:
            text = text.decode("utf-8")
        except UnicodeDecodeError as exc:
            return Result([Diagnostic("PARSE_ERROR", "", "not UTF-8: %s" % exc)])
    doc, err = parse_text(text)
    if err is not None:
        return Result([Diagnostic("PARSE_ERROR", "", err)])
    return validate_document(doc, release=release)


def validate_file(path, release=False) -> Result:
    """Read and validate a pin file. I/O problems are PARSE_ERROR diagnostics, never exceptions."""
    try:
        with open(path, "rb") as fh:
            data = fh.read(MAX_PIN_FILE_BYTES + 1)
    except OSError as exc:
        return Result([Diagnostic("PARSE_ERROR", "", "cannot read pin file: %s" % exc.strerror)])
    return validate_text(data, release=release)
