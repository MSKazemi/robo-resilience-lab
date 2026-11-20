"""Shared helpers for the casebook tests (standard library only)."""

import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from triage import validator as V

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"
REPO_ROOT = ROOT.parent.parent


def load_case(n):
    return json.loads((FIXTURES / f"case_{n:03d}.json").read_text(encoding="utf-8"))


def all_cases():
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(FIXTURES.glob("case_*.json"))]


def case_by_label(label, role=None):
    return [c for c in all_cases() if c["expected"]["label"] == label and (role is None or c["case_role"] == role)]


def codes(diags):
    return sorted({d.code for d in diags})


def walk(obj, path=()):
    """Yield (path, value) for every node, parents before children."""
    yield path, obj
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from walk(v, path + (k,))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk(v, path + (i,))


def set_at(obj, path, value):
    """Return a deep copy of obj with the node at path replaced."""
    out = copy.deepcopy(obj)
    cur = out
    for k in path[:-1]:
        cur = cur[k]
    cur[path[-1]] = value
    return out


class TmpDirCase(unittest.TestCase):
    """Gives each test a scratch copy of the fixtures directory."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.dir = Path(self._td.name) / "fixtures"
        shutil.copytree(FIXTURES, self.dir)

    def snapshot(self):
        return {p.name: p.read_bytes() for p in sorted(self.dir.iterdir())}

    def write_case(self, name, obj):
        (self.dir / name).write_text(json.dumps(obj), encoding="utf-8")


def valid_case():
    return load_case(1)


class DiagCase(unittest.TestCase):
    def assertHasCode(self, diags, code):
        self.assertIn(code, codes(diags), msg=f"wanted {code}, got {diags}")

    def assertValid(self, diags):
        self.assertEqual([], list(diags))
