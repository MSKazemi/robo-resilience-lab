"""Offline resolver and checker for a Hugging Face Hub style cache.

Python standard library only. This module never opens a network connection and never
imports ``huggingface_hub``. It only *reads* the cache layout that ``huggingface_hub``
writes (see https://huggingface.co/docs/huggingface_hub/guides/manage-cache)::

    <cache_dir>/
      models--<namespace>--<name>/          # datasets--<namespace>--<name>/ for datasets
        blobs/<etag>                        # file contents
        refs/main                           # text file holding a commit hash (ignored here)
        snapshots/<40-hex commit>/<path>    # usually a symlink to ../../blobs/<etag>

Pins resolve to ``snapshots/<revision>/<path>`` only. ``refs/`` is deliberately ignored:
a branch ref is exactly the mutable pointer a pin exists to avoid.

``seed_synthetic_cache`` writes that layout for SYNTHETIC fixtures only, so tests and the
README demo have a cache to check without downloading anything.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass, field

import hf_pins

OFFLINE_TRUE_VALUES = {"1", "ON", "YES", "TRUE"}  # same set as huggingface_hub.constants._is_true
CHUNK = 1024 * 1024

CHECK_CODES = {
    "PIN_INVALID": "the pin file does not validate; the cache was not inspected",
    "CACHE_DIR_MISSING": "the cache directory does not exist or is not a directory",
    "CACHE_REPO_MISSING": "no cache folder for this repo (never prefetched, or wrong kind)",
    "CACHE_REVISION_MISSING": "the repo folder exists but has no snapshot for the pinned revision",
    "CACHE_REVISION_MISMATCH": "the cache holds other revisions of this repo, but not the pinned one",
    "CACHE_FILE_MISSING": "a pinned file is absent from the snapshot (or is a dangling symlink)",
    "CACHE_NOT_REGULAR_FILE": "a pinned path is a directory or special file, not a regular file",
    "CACHE_PATH_ESCAPE": "a pinned path resolves outside the repo's cache folder (symlink escape)",
    "CACHE_SIZE_MISMATCH": "the cached file size differs from the pinned size",
    "CACHE_SHA256_MISMATCH": "the cached file content does not match the pinned sha256 (tampered or wrong file)",
    "CACHE_READ_ERROR": "the cached file could not be read",
    "OFFLINE_ENV_NOT_SET": "HF_HUB_OFFLINE is not set to a true value (1/ON/YES/TRUE)",
    "UNVERIFIED_NO_FILES": "warning: the pin lists no files, only the snapshot folder was checked",
}


@dataclass(frozen=True)
class Finding:
    code: str
    pin_id: str
    path: str
    message: str
    warning: bool = False

    def render(self) -> str:
        level = "warning" if self.warning else "error"
        where = self.pin_id + (":" + self.path if self.path else "")
        return "[%s:%s] %s: %s" % (level, self.code, where or "-", self.message)


@dataclass
class CheckReport:
    findings: list = field(default_factory=list)
    verified_files: int = 0
    pins_checked: int = 0

    @property
    def errors(self):
        return [f for f in self.findings if not f.warning]

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def codes(self):
        return [f.code for f in self.findings]


def _expand(p):
    return os.path.expanduser(os.path.expandvars(p))


def resolve_cache_dir(cache_dir=None, env=None):
    """Return the hub cache directory with the same precedence as huggingface_hub.

    Explicit ``cache_dir`` > ``HF_HUB_CACHE`` > legacy ``HUGGINGFACE_HUB_CACHE`` >
    ``HF_HOME``/hub > ``XDG_CACHE_HOME``/huggingface/hub > ``~/.cache/huggingface/hub``.
    """
    if cache_dir:
        return os.path.abspath(_expand(str(cache_dir)))
    env = os.environ if env is None else env
    for var in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"):
        if env.get(var):
            return os.path.abspath(_expand(env[var]))
    if env.get("HF_HOME"):
        return os.path.abspath(os.path.join(_expand(env["HF_HOME"]), "hub"))
    xdg = env.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.abspath(os.path.join(_expand(xdg), "huggingface", "hub"))


def offline_env_enabled(env=None) -> bool:
    env = os.environ if env is None else env
    value = env.get("HF_HUB_OFFLINE") or env.get("TRANSFORMERS_OFFLINE")
    return value is not None and value.upper() in OFFLINE_TRUE_VALUES


def repo_folder_name(repo_id, kind):
    """``models--org--name`` / ``datasets--org--name`` (huggingface_hub.file_download.repo_folder_name)."""
    return "--".join(["%ss" % kind] + repo_id.split("/"))


@dataclass(frozen=True)
class Resolved:
    pin_id: str
    kind: str
    repo_id: str
    revision: str
    repo_dir: str
    snapshot_dir: str
    files: tuple  # ((relative path, absolute path), ...)


def resolve_pin(pin, cache_dir):
    """Map one *validated* pin to absolute cache paths. Does not touch the filesystem."""
    repo_dir = os.path.join(cache_dir, repo_folder_name(pin["repo_id"], pin["kind"]))
    snap = os.path.join(repo_dir, "snapshots", pin["revision"])
    files = tuple((f["path"], os.path.join(snap, *f["path"].split("/"))) for f in pin.get("files", []))
    return Resolved(pin["id"], pin["kind"], pin["repo_id"], pin["revision"], repo_dir, snap, files)


def resolve_document(doc, cache_dir):
    return [resolve_pin(p, cache_dir) for p in doc["pins"]]


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(CHUNK)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _inside(child, parent) -> bool:
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:  # different drives on Windows
        return False


def _check_pin(pin, cache_dir, report):
    r = resolve_pin(pin, cache_dir)
    pid = r.pin_id
    if not os.path.isdir(r.repo_dir):
        report.findings.append(_finding(
            "CACHE_REPO_MISSING", pid, "", "%s %r is not in the cache (expected %s)" % (r.kind, r.repo_id, r.repo_dir)))
        return
    if not os.path.isdir(r.snapshot_dir):
        snaps_dir = os.path.join(r.repo_dir, "snapshots")
        try:
            others = sorted(e for e in os.listdir(snaps_dir) if e != r.revision)
        except OSError:
            others = []
        if others:
            shown = ", ".join(o[:12] for o in others[:5]) + (" ..." if len(others) > 5 else "")
            report.findings.append(_finding(
                "CACHE_REVISION_MISMATCH", pid, "",
                "pinned %s is not cached; the cache holds %d other revision(s): %s" % (r.revision, len(others), shown)))
        else:
            report.findings.append(_finding(
                "CACHE_REVISION_MISSING", pid, "", "no snapshot for pinned revision %s" % r.revision))
        return
    if not r.files:
        report.findings.append(_finding(
            "UNVERIFIED_NO_FILES", pid, "", "snapshot exists; no file hashes are pinned, contents not verified",
            warning=True))
        return
    real_repo = os.path.realpath(r.repo_dir)
    entries = {f["path"]: f for f in pin["files"]}
    for rel, abspath in r.files:
        want = entries[rel]
        if not os.path.exists(abspath):  # False for a dangling symlink too
            report.findings.append(_finding("CACHE_FILE_MISSING", pid, rel, "not present in the snapshot"))
            continue
        real = os.path.realpath(abspath)
        if not _inside(real, real_repo):
            report.findings.append(_finding(
                "CACHE_PATH_ESCAPE", pid, rel, "resolves outside the repo cache folder; refusing to trust it"))
            continue
        if not os.path.isfile(real):
            report.findings.append(_finding("CACHE_NOT_REGULAR_FILE", pid, rel, "not a regular file"))
            continue
        try:
            size = os.path.getsize(real)
            if "size" in want and size != want["size"]:
                report.findings.append(_finding(
                    "CACHE_SIZE_MISMATCH", pid, rel, "size %d bytes, pinned %d" % (size, want["size"])))
                continue
            got = sha256_file(real)
        except OSError as exc:
            report.findings.append(_finding("CACHE_READ_ERROR", pid, rel, exc.strerror or str(exc)))
            continue
        if got != want["sha256"]:
            report.findings.append(_finding(
                "CACHE_SHA256_MISMATCH", pid, rel, "sha256 %s, pinned %s" % (got, want["sha256"])))
            continue
        report.verified_files += 1


def _finding(code, pin_id, path, message, warning=False):
    return Finding(code, pin_id, path, message, warning)


def check_document(doc, cache_dir, release=False, require_offline_env=False, env=None):
    """Validate ``doc`` and verify every pin against the cache. Never raises; fails closed.

    A pin file that does not validate is never checked against the cache (``PIN_INVALID``).
    In release mode, a warning such as ``UNVERIFIED_NO_FILES`` cannot occur because the
    validator already requires file hashes.
    """
    report = CheckReport()
    try:
        result = hf_pins.validate_document(doc, release=release)
        if not result.ok:
            for d in result.diagnostics:
                report.findings.append(Finding("PIN_INVALID", "-", d.path, d.render()))
            return report
        if require_offline_env and not offline_env_enabled(env):
            report.findings.append(Finding("OFFLINE_ENV_NOT_SET", "-", "",
                                           "set HF_HUB_OFFLINE=1 so huggingface_hub cannot fall back to the network"))
        if not os.path.isdir(cache_dir):
            report.findings.append(Finding("CACHE_DIR_MISSING", "-", "", "%s is not a directory" % cache_dir))
            return report
        for pin in doc["pins"]:
            report.pins_checked += 1
            _check_pin(pin, cache_dir, report)
    except Exception as exc:  # noqa: BLE001 - fail closed on any checker bug
        message = "internal error %s: %s" % (type(exc).__name__, exc)
        report.findings.append(Finding("CACHE_READ_ERROR", "-", "", message))
    return report


class SeedError(Exception):
    pass


def seed_synthetic_cache(doc, source_dir, cache_dir, copy=False):
    """Write a huggingface_hub-style cache for a SYNTHETIC pin file from local fixture files.

    ``source_dir/<pin id>/<path>`` must hold each pinned file and match its sha256.
    Blobs are named by sha256 (as huggingface_hub does for LFS files). Snapshot entries are
    relative symlinks into ``blobs/`` unless ``copy`` is true (or symlinks are unavailable).
    Refuses real pin files, invalid pin files, mismatching sources and existing repo folders.
    Returns the list of repo folders created.
    """
    result = hf_pins.validate_document(doc, release=False)
    if not result.ok:
        raise SeedError("pin file is invalid: " + "; ".join(d.render() for d in result.diagnostics[:5]))
    if doc["data_kind"] != "synthetic":
        raise SeedError("seeding is only for data_kind 'synthetic'; prefetch real assets with huggingface_hub")
    plans = []
    for pin in doc["pins"]:
        r = resolve_pin(pin, cache_dir)
        if os.path.lexists(r.repo_dir):
            raise SeedError("%s already exists; refusing to overwrite a cache folder" % r.repo_dir)
        for f in pin.get("files", []):
            src = os.path.join(source_dir, pin["id"], *f["path"].split("/"))
            if not os.path.isfile(src):
                raise SeedError("missing fixture source %s" % src)
            if sha256_file(src) != f["sha256"]:
                raise SeedError("fixture source %s does not match its pinned sha256" % src)
        plans.append((pin, r))
    created = []
    for pin, r in plans:
        os.makedirs(os.path.join(r.repo_dir, "blobs"))
        os.makedirs(os.path.join(r.repo_dir, "refs"))
        os.makedirs(r.snapshot_dir)
        # refs/main exists in a real cache; written here for layout fidelity. The checker ignores it.
        with open(os.path.join(r.repo_dir, "refs", "main"), "w", encoding="ascii", newline="") as fh:
            fh.write(pin["revision"])
        for f in pin.get("files", []):
            src = os.path.join(source_dir, pin["id"], *f["path"].split("/"))
            blob = os.path.join(r.repo_dir, "blobs", f["sha256"])
            if not os.path.exists(blob):
                shutil.copyfile(src, blob)
            dest = os.path.join(r.snapshot_dir, *f["path"].split("/"))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            if copy:
                shutil.copyfile(blob, dest)
            else:
                target = os.path.relpath(blob, os.path.dirname(dest))
                try:
                    os.symlink(target, dest)
                except (OSError, NotImplementedError):
                    shutil.copyfile(blob, dest)
        created.append(r.repo_dir)
    return created
