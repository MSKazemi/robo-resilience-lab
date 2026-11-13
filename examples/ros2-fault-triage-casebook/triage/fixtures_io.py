"""Canonical JSON text for case files, the manifest, and the ``--check`` / ``--write`` logic.

The JSON case files are the source of truth. ``--write`` rewrites them into one canonical layout and
regenerates ``MANIFEST.json`` (sha256 per file); ``--check`` verifies, without writing, that every
file is already byte-identical to its canonical form and that the manifest matches. A rejected
``--write`` (any validation problem) leaves every file untouched.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from . import taxonomy as T
from . import validator as V


def _scalar(v):
    if v is None or type(v) is bool:
        return json.dumps(v)
    if type(v) is int:
        return str(v)
    if type(v) is str:
        return json.dumps(v, ensure_ascii=True)
    raise TypeError(f"canonical form does not support {type(v).__name__}")  # validated input never gets here


def _inline(v):
    """Inline rendering for scalars and for short lists of scalars / lists of scalar lists, else None."""
    if type(v) is list:
        parts = []
        for x in v:
            if type(x) is dict:
                return None
            r = _inline(x)
            if r is None:
                return None
            parts.append(r)
        return "[" + ", ".join(parts) + "]"
    if type(v) is dict:
        return None
    return _scalar(v)


def canonical_dumps(obj, level=0):
    """Deterministic text: 2-space indent, sorted keys, short scalar lists inline, final newline."""
    return _render(obj, level) + ("\n" if level == 0 else "")


def _render(v, level):
    pad = "  " * level
    if type(v) is dict:
        if not v:
            return "{}"
        items = [f'{pad}  {json.dumps(k, ensure_ascii=True)}: {_render(v[k], level + 1)}' for k in sorted(v)]
        return "{\n" + ",\n".join(items) + f"\n{pad}}}"
    if type(v) is list:
        if not v:
            return "[]"
        inline = _inline(v)
        if inline is not None and len(inline) <= 72:
            return inline
        items = [f"{pad}  {_render(x, level + 1)}" for x in v]
        return "[\n" + ",\n".join(items) + f"\n{pad}]"
    return _scalar(v)


def sha256_hex(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_manifest(files):
    """``files`` maps file name -> canonical text. Returns the manifest text."""
    manifest = {
        "schema": T.MANIFEST_SCHEMA,
        "synthetic": True,
        "label": "SYNTHETIC: sha256 of every case file; regenerate with `python3 -m triage fixtures --write`",
        "case_count": len(files),
        "files": {name: sha256_hex(text) for name, text in sorted(files.items())},
    }
    return canonical_dumps(manifest)


def _collect(directory):
    """Validate the directory; return (report, {name: canonical_text}) for the valid cases."""
    rep = V.validate_dir(directory)
    texts = {}
    if rep.ok:
        for case in rep.valid_cases:
            texts[f"case_{case['case_id'][5:]}.json"] = canonical_dumps(case)
    return rep, texts


def check(directory):
    """Return a list of problem strings (empty = every file canonical and manifest current)."""
    rep, texts = _collect(directory)
    problems = [f"{d.file}: {d.code} {d.path} {d.message}".replace("  ", " ") for d in rep.diags]
    if problems:
        return problems
    root = Path(directory)
    for name, want in sorted(texts.items()):
        got = (root / name).read_text(encoding="utf-8")
        if got != want:
            problems.append(f"{name}: NOT_CANONICAL file differs from its canonical form (run fixtures --write and review the diff)")
    mpath = root / "MANIFEST.json"
    if not mpath.is_file() or mpath.is_symlink():
        problems.append("MANIFEST.json: MISSING manifest file")
    elif mpath.read_text(encoding="utf-8") != build_manifest(texts):
        problems.append("MANIFEST.json: STALE manifest does not match the case files (run fixtures --write)")
    return problems


def _atomic_write(path, text):
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def write(directory):
    """Rewrite canonical files + manifest. Returns (changed_names, problems). On problems, writes nothing."""
    rep, texts = _collect(directory)
    if not rep.ok:
        return [], [f"{d.file}: {d.code} {d.path} {d.message}" for d in rep.diags]
    root = Path(directory)
    changed = []
    for name, text in sorted(texts.items()):
        if (root / name).read_text(encoding="utf-8") != text:
            _atomic_write(root / name, text)
            changed.append(name)
    mtext = build_manifest(texts)
    mpath = root / "MANIFEST.json"
    if not mpath.is_file() or mpath.read_text(encoding="utf-8") != mtext:
        _atomic_write(mpath, mtext)
        changed.append("MANIFEST.json")
    return changed, []
