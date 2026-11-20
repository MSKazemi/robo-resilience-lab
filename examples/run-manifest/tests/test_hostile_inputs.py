"""Property-style hostile-input tests: replace every node of a valid manifest with hostile values.

The validator must never raise, never fall back to INTERNAL_ERROR, and must reject
type confusion (list/dict/None/bool where a scalar belongs, and the reverse).
"""

import copy
import unittest

from tests import support as s
from tests.support import base, set_path, walk

v = s.v

HOSTILE_ANY = [None, True, 0, -1, 1.5, float("nan"), float("inf"), 10**400, "", "\n", "x" * 500, "\ud800",
               [], [[]], {}, {"a": {}}, {"value": "x", "unit": []}, "2026-01-01T00:00:00Z", "0" * 64, "192.0.2.1", "none"]
TYPE_CONFUSION = [None, [], [[]], [1], {}, {"a": {}}]  # never valid in place of an existing str/int/bool/float leaf
SCALARS_FOR_CONTAINERS = [None, "x", 1, 1.5, True, ""]


class HostileInputs(unittest.TestCase):
    def setUp(self):
        self.doc = base()
        self.nodes = list(walk(self.doc))
        self.assertGreater(len(self.nodes), 150)

    def test_no_replacement_ever_raises_or_hits_internal_error(self):
        n = 0
        for path, _ in self.nodes:
            for h in HOSTILE_ANY:
                d = copy.deepcopy(self.doc)
                set_path(d, path, copy.deepcopy(h))
                res = v.validate_document(d)  # must not raise
                self.assertNotIn("INTERNAL_ERROR", res.codes, (path, h))
                for diag in res.diagnostics:
                    self.assertTrue(diag.code in s.c.CODES and diag.message and diag.hint)
                    self.assertTrue(diag.path == "" or diag.path.startswith("/"))
                    diag.message.encode("ascii")  # messages are ASCII-safe even for lone surrogates
                n += 1
        self.assertGreater(n, 3000)

    def test_type_confusion_on_scalar_leaves_is_always_rejected(self):
        for path, node in self.nodes:
            if node is None or isinstance(node, (dict, list)):
                continue
            for h in TYPE_CONFUSION:
                d = copy.deepcopy(self.doc)
                set_path(d, path, copy.deepcopy(h))
                res = v.validate_document(d)
                self.assertFalse(res.valid, (path, h))

    def test_scalars_in_place_of_containers_are_rejected(self):
        for path, node in self.nodes:
            if not isinstance(node, (dict, list)):
                continue
            for h in SCALARS_FOR_CONTAINERS:
                d = copy.deepcopy(self.doc)
                set_path(d, path, h)
                res = v.validate_document(d)
                self.assertFalse(res.valid, (path, h))
                # a bare scalar where a {value, unit} quantity belongs is the typed UNIT_MISSING
                self.assertTrue({"WRONG_TYPE", "UNIT_MISSING"} & set(res.codes), (path, h))

    def test_bool_in_place_of_any_number_is_typed(self):
        hits = 0
        for path, node in self.nodes:
            if isinstance(node, bool) or not isinstance(node, (int, float)):
                continue
            for b in (True, False):
                d = copy.deepcopy(self.doc)
                set_path(d, path, b)
                self.assertIn("BOOL_AS_NUMBER", v.validate_document(d).codes, (path, b))
                hits += 1
        self.assertGreater(hits, 20)

    def test_nan_in_place_of_any_number_is_reported_exactly_once(self):
        for path, node in self.nodes:
            if isinstance(node, bool) or not isinstance(node, (int, float)):
                continue
            d = copy.deepcopy(self.doc)
            set_path(d, path, float("nan"))
            res = v.validate_document(d)
            self.assertEqual(res.codes.count("NON_FINITE_VALUE"), 1, path)

    def test_negative_or_absurd_numbers_are_rejected(self):
        for path, node in self.nodes:
            if isinstance(node, bool) or not isinstance(node, (int, float)):
                continue
            for bad in (-1, -0.5, 10**400):
                d = copy.deepcopy(self.doc)
                set_path(d, path, bad)
                self.assertFalse(v.validate_document(d).valid, (path, bad))

    def test_removing_any_required_key_is_rejected(self):
        optional = {"description", "container_runtime", "kernel_version", "deadline", "lifespan",
                    "one_way_delay", "jitter", "loss", "bandwidth", "censor_reason"}
        checked = 0
        for path, _ in self.nodes:
            if isinstance(path[-1], int) or path[-1] in optional:
                continue  # list elements and optional keys: dropping them is legitimate
            d = copy.deepcopy(self.doc)
            parent = d
            for k in path[:-1]:
                parent = parent[k]
            del parent[path[-1]]
            self.assertFalse(v.validate_document(d).valid, path)
            checked += 1
        self.assertGreater(checked, 80)

    def test_concatenated_or_repeated_documents_are_not_silently_merged(self):
        text = '{"schema_version": "run-manifest/v1"}'
        self.assertEqual(v.validate_text(text + text).codes, ["MALFORMED_JSON"])
        self.assertEqual(v.validate_text("[" + text + "," + text + "]").codes, ["MALFORMED_DOCUMENT"])


if __name__ == "__main__":
    unittest.main()
