"""Shared helpers for the hf-revision-pins tests (stdlib only, offline)."""

import copy
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import hf_cache  # noqa: E402,F401
import hf_pins  # noqa: E402,F401

FIX = os.path.join(ROOT, "fixtures")
PINS = os.path.join(FIX, "pins")
SOURCES = os.path.join(FIX, "synthetic-files")
VALID = os.path.join(PINS, "synthetic-valid.json")
NO_HASHES = os.path.join(PINS, "synthetic-no-hashes.json")
INVALID_DIR = os.path.join(PINS, "invalid")


def load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def valid():
    return copy.deepcopy(load(VALID))


def as_real(doc):
    doc = copy.deepcopy(doc)
    doc["data_kind"] = "real"
    return doc


def codes(doc, release=False):
    return hf_pins.validate_document(doc, release=release).codes


class TempCache:
    """A temporary directory holding a seeded SYNTHETIC hub cache."""

    def __init__(self, doc=None, copy_files=False, seed=True):
        self.doc = valid() if doc is None else doc
        self.tmp = tempfile.mkdtemp(prefix="hfpins-test-")
        self.cache = os.path.join(self.tmp, "hub")
        os.makedirs(self.cache)
        if seed:
            hf_cache.seed_synthetic_cache(self.doc, SOURCES, self.cache, copy=copy_files)

    def resolved(self, index=0):
        return hf_cache.resolve_pin(self.doc["pins"][index], self.cache)

    def check(self, doc=None, **kw):
        return hf_cache.check_document(self.doc if doc is None else doc, self.cache, **kw)

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


def symlinks_supported():
    tmp = tempfile.mkdtemp(prefix="hfpins-symlink-")
    try:
        os.symlink("target", os.path.join(tmp, "link"))
        return True
    except (OSError, NotImplementedError, AttributeError):
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
