"""Validator: positive on the shipped cases, negative on every defect class (typed diagnostics)."""

import copy
import json
import math
import os
import unittest

from triage import taxonomy as T
from triage import validator as V

from .support import DiagCase, FIXTURES, TmpDirCase, all_cases, case_by_label, load_case, set_at, valid_case, walk


class ShippedCases(DiagCase):
    def test_every_shipped_case_is_valid(self):
        for c in all_cases():
            with self.subTest(case=c["case_id"]):
                self.assertValid(V.validate_case(c))

    def test_directory_is_valid_and_nothing_is_skipped(self):
        rep = V.validate_dir(FIXTURES)
        self.assertEqual([], rep.diags)
        self.assertEqual(rep.case_files, len(rep.valid_cases))
        self.assertEqual(rep.entries_seen, rep.case_files + 1)  # + MANIFEST.json
        self.assertGreaterEqual(rep.case_files, 12)


class StructureTests(DiagCase):
    def test_not_an_object(self):
        for bad in (None, [], "x", 3, True, 1.5):
            self.assertHasCode(V.validate_case(bad), V.WRONG_TYPE)

    def test_unknown_and_missing_key(self):
        c = valid_case()
        c["extra"] = 1
        self.assertHasCode(V.validate_case(c), V.UNKNOWN_KEY)
        c = valid_case()
        del c["symptom"]
        self.assertHasCode(V.validate_case(c), V.MISSING_KEY)

    def test_unknown_key_in_evidence(self):
        c = valid_case()
        c["evidence"]["pods"]["publisher"]["host"] = "x"
        self.assertHasCode(V.validate_case(c), V.UNKNOWN_KEY)

    def test_label_outside_closed_taxonomy(self):
        c = valid_case()
        c["expected"]["label"] = "qos_incompatible"
        self.assertHasCode(V.validate_case(c), V.BAD_VALUE)

    def test_label_is_case_sensitive_and_exact(self):
        c = valid_case()
        c["expected"]["label"] = "No_Fault"
        self.assertHasCode(V.validate_case(c), V.BAD_VALUE)

    def test_synthetic_must_be_true_boolean(self):
        for bad in (False, 1, "true", None):
            c = valid_case()
            c["synthetic"] = bad
            self.assertHasCode(V.validate_case(c), V.NOT_SYNTHETIC)

    def test_source_must_say_synthetic_in_full(self):
        for bad in ("hand written", "SYNTHETIC:", "SYNTHETIC: ok\n", "synthetic: lower"):
            c = valid_case()
            c["source"] = bad
            self.assertHasCode(V.validate_case(c), V.NOT_SYNTHETIC)

    def test_schema_version_checked(self):
        c = valid_case()
        c["schema"] = "rrl.triage-case/2"
        self.assertHasCode(V.validate_case(c), V.BAD_VALUE)


class IdentifierTests(DiagCase):
    def test_trailing_newline_rejected_everywhere(self):
        for field in ("case_id", "incident_family_id"):
            c = valid_case()
            c[field] = c[field] + "\n"
            self.assertHasCode(V.validate_case(c), V.BAD_ID)

    def test_non_ascii_digits_and_lookalikes_rejected(self):
        for bad in ("case-١٢٣", "case-00１", "Case-001", "case-1", "case-0011", " case-001"):
            c = valid_case()
            c["case_id"] = bad
            self.assertHasCode(V.validate_case(c), V.BAD_ID)

    def test_family_id_patterns(self):
        for bad in ("fam-", "fam--x", "fam-X", "family-x", "fam-a_b"):
            c = valid_case()
            c["incident_family_id"] = bad
            self.assertHasCode(V.validate_case(c), V.BAD_ID)

    def test_text_rejects_newline_tab_non_ascii_and_overlong(self):
        for bad in ("line\nbreak", "tab\there", "café", "x" * (T.MAX_TEXT + 1), "", "trailing\n"):
            c = valid_case()
            c["symptom"] = bad
            self.assertTrue(V.validate_case(c), msg=repr(bad))


class TypeConfusionTests(DiagCase):
    def test_bool_is_not_an_integer(self):
        c = valid_case()
        c["evidence"]["probe_summary"]["received_total"] = False
        self.assertHasCode(V.validate_case(c), V.WRONG_TYPE)
        c = valid_case()
        c["evidence"]["ros"]["publisher"]["domain_id"] = True
        self.assertHasCode(V.validate_case(c), V.WRONG_TYPE)

    def test_int_is_not_a_boolean(self):
        c = valid_case()
        c["evidence"]["pods"]["publisher"]["ready"] = 1
        self.assertHasCode(V.validate_case(c), V.WRONG_TYPE)
        c = valid_case()
        c["evidence"]["probe_summary"]["ok"] = 0
        self.assertHasCode(V.validate_case(c), V.WRONG_TYPE)

    def test_float_and_numeric_string_are_not_integers(self):
        for bad in (1.0, 2.5, "200"):
            c = valid_case()
            c["evidence"]["probe_summary"]["expected_count"] = bad
            self.assertHasCode(V.validate_case(c), V.WRONG_TYPE)

    def test_nan_and_infinity_in_memory(self):
        for bad in (math.nan, math.inf, -math.inf):
            c = valid_case()
            c["evidence"]["probe_summary"]["unique"] = bad
            self.assertHasCode(V.validate_case(c), V.NON_FINITE)

    def test_nan_in_json_text_is_rejected_at_parse(self):
        for lit in ("NaN", "Infinity", "-Infinity"):
            obj, diags = V.parse_json_text('{"a": %s}' % lit)
            self.assertIsNone(obj)
            self.assertHasCode(diags, V.NON_FINITE)

    def test_out_of_range_integers(self):
        for path, bad in ((("ros", "publisher", "domain_id"), -1), (("ros", "publisher", "domain_id"), T.MAX_DOMAIN_ID + 1),
                          (("ros", "publisher", "qos", "depth"), 0), (("probe_summary", "expected_count"), 0),
                          (("probe_summary", "received_total"), T.MAX_COUNT + 1), (("probe_summary", "received_total"), -1),
                          (("probe_summary", "received_total"), 10**30)):
            c = valid_case()
            cur = c["evidence"]
            for k in path[:-1]:
                cur = cur[k]
            cur[path[-1]] = bad
            self.assertHasCode(V.validate_case(c), V.OUT_OF_RANGE)

    def test_list_and_dict_where_scalar_expected_never_raise(self):
        """Every scalar leaf, replaced by a list / dict / NaN, must give a diagnostic and never an exception."""
        for n in (1, 3, 15, 17, 22, 26):  # QoS, discovery, pods, partition, clock, injection
            base = load_case(n)
            for path, val in walk(base):
                if not path or isinstance(val, (dict, list)):
                    continue
                for bad in ([], {}, [1], {"a": 1}, math.nan):
                    with self.subTest(case=n, path=path, bad=repr(bad)):
                        diags = V.validate_case(set_at(base, path, bad))
                        self.assertTrue(diags)

    def test_container_replaced_by_scalar_never_raises(self):
        for n in (1, 15, 17, 22):
            base = load_case(n)
            for path, val in walk(base):
                if not path or not isinstance(val, (dict, list)):
                    continue
                for bad in (5, "x", None, True, math.nan):
                    with self.subTest(case=n, path=path, bad=repr(bad)):
                        self.assertTrue(V.validate_case(set_at(base, path, bad)))

    def test_unhashable_where_enum_expected(self):
        c = valid_case()
        c["expected"]["label"] = ["no_fault"]
        self.assertHasCode(V.validate_case(c), V.WRONG_TYPE)
        c = valid_case()
        c["cites"] = [["probe-schema"]]
        self.assertHasCode(V.validate_case(c), V.WRONG_TYPE)
        c = valid_case()
        c["cites"] = [{"a": 1}]
        self.assertHasCode(V.validate_case(c), V.WRONG_TYPE)

    def test_evidence_bundle_validator_is_total(self):
        for bad in (None, [], "x", 3, math.nan, {"pods": []}, {"pods": {"publisher": 3}}, {"ros": {"publisher": {"qos": 1}}},
                    {"probe_summary": {"missing_ranges": [[1]]}}, {"probe_summary": {"missing_ranges": [3]}},
                    {"fault_markers": [[]]}, {"fault_markers": "partition_begin"}, {"log_excerpt": [1]}):
            self.assertTrue(V.validate_evidence(bad), msg=repr(bad))


class ConsistencyTests(DiagCase):
    def test_role_label_rules(self):
        c = valid_case()
        c["case_role"] = "ambiguous"
        self.assertHasCode(V.validate_case(c), V.INCONSISTENT)
        amb = case_by_label("insufficient_evidence", "ambiguous")[0]
        amb["case_role"] = "positive"
        self.assertHasCode(V.validate_case(amb), V.INCONSISTENT)

    def test_hard_negative_needs_looks_like_and_others_must_not_have_it(self):
        hn = case_by_label("no_fault", "hard_negative")[0]
        bad = copy.deepcopy(hn)
        bad["looks_like"] = None
        self.assertHasCode(V.validate_case(bad), V.INCONSISTENT)
        bad = copy.deepcopy(hn)
        bad["looks_like"] = "no_fault"
        self.assertHasCode(V.validate_case(bad), V.INCONSISTENT)
        pos = valid_case()
        pos["looks_like"] = "rmw_mismatch"
        self.assertHasCode(V.validate_case(pos), V.INCONSISTENT)

    def test_safe_next_step_must_match_label_and_be_known(self):
        c = valid_case()
        c["expected"]["safe_next_step"] = "no_action"
        self.assertHasCode(V.validate_case(c), V.INCONSISTENT)
        c["expected"]["safe_next_step"] = "kubectl delete pod"
        self.assertHasCode(V.validate_case(c), V.BAD_VALUE)

    def test_secondary_labels(self):
        c = valid_case()
        lab = c["expected"]["label"]
        c["expected"]["secondary_labels"] = [lab]
        self.assertHasCode(V.validate_case(c), V.INCONSISTENT)
        c["expected"]["secondary_labels"] = ["rmw_mismatch", "rmw_mismatch"]
        self.assertHasCode(V.validate_case(c), V.INCONSISTENT)
        c["expected"]["secondary_labels"] = ["insufficient_evidence"]
        self.assertHasCode(V.validate_case(c), V.INCONSISTENT)
        c["expected"]["secondary_labels"] = ["nonsense"]
        self.assertHasCode(V.validate_case(c), V.BAD_VALUE)

    def test_unknown_or_duplicate_citation(self):
        c = valid_case()
        c["cites"] = ["no-such-doc"]
        self.assertHasCode(V.validate_case(c), V.UNKNOWN_CITATION)
        c["cites"] = ["probe-schema", "probe-schema"]
        self.assertHasCode(V.validate_case(c), V.INCONSISTENT)
        c["cites"] = []
        self.assertHasCode(V.validate_case(c), V.OUT_OF_RANGE)

    def test_discovery_mode_and_server_check_must_agree(self):
        c = valid_case()
        c["evidence"]["discovery"] = {"mode": "multicast_default", "server_check": "reachable"}
        self.assertHasCode(V.validate_case(c), V.INCONSISTENT)
        c["evidence"]["discovery"] = {"mode": "discovery_server", "server_check": None}
        self.assertHasCode(V.validate_case(c), V.INCONSISTENT)

    def test_probe_summary_arithmetic(self):
        for field, bad in (("unique", 201), ("missing", 5), ("completion", "complete"), ("ok", True), ("reordered", 999)):
            c = valid_case()  # zero-message case
            c["evidence"]["probe_summary"][field] = bad
            self.assertHasCode(V.validate_case(c), V.INCONSISTENT)

    def test_missing_ranges_must_be_sorted_and_sum_to_missing(self):
        c = load_case(17)  # partition case
        c["evidence"]["probe_summary"]["missing_ranges"] = [[60, 99], [10, 20]]
        self.assertHasCode(V.validate_case(c), V.INCONSISTENT)
        c["evidence"]["probe_summary"]["missing_ranges"] = [[60, 90]]
        self.assertHasCode(V.validate_case(c), V.INCONSISTENT)
        c["evidence"]["probe_summary"]["missing_ranges"] = [[99, 60]]
        self.assertHasCode(V.validate_case(c), V.INCONSISTENT)

    def test_latency_summary_consistency(self):
        c = load_case(22)
        c["evidence"]["latency_summary"]["min_ns"] = 5
        self.assertHasCode(V.validate_case(c), V.INCONSISTENT)

    def test_forbidden_content_in_free_text(self):
        for text in ("see 192.168.1.20 for the pod", "contact ops@example.org", "open https://example.invalid/x",
                     "key=" + "A1b2C3d4" * 5):
            for field in ("symptom",):
                c = valid_case()
                c[field] = text
                self.assertHasCode(V.validate_case(c), V.FORBIDDEN_CONTENT)
        c = valid_case()
        c["evidence"]["log_excerpt"] = ["token ghp_" + "a1B2" * 10]
        self.assertHasCode(V.validate_case(c), V.FORBIDDEN_CONTENT)

    def test_plain_snake_case_identifiers_are_not_tokens(self):
        c = valid_case()
        c["symptom"] = "check_discovery_server_reachability and clock_skew_misleading_latency"
        self.assertValid(V.validate_case(c))


class LoadingTests(DiagCase):
    def test_duplicate_json_key(self):
        obj, diags = V.parse_json_text('{"a": 1, "a": 2}')
        self.assertIsNone(obj)
        self.assertHasCode(diags, V.DUPLICATE_KEY)

    def test_duplicate_key_nested(self):
        _o, diags = V.parse_json_text('{"x": {"k": 1, "k": 1}}')
        self.assertHasCode(diags, V.DUPLICATE_KEY)

    def test_deep_nesting_is_a_typed_diagnostic(self):
        _o, diags = V.parse_json_text("[" * 100_000 + "]" * 100_000)
        self.assertHasCode(diags, V.UNREADABLE)

    def test_bom_and_garbage(self):
        for text in ("﻿{}", "{", "", "nul\x00l"):
            _o, diags = V.parse_json_text(text)
            self.assertTrue(diags, msg=repr(text))


class DirectoryTests(TmpDirCase, DiagCase):
    def codes_for(self):
        return sorted({d.code for d in V.validate_dir(self.dir).diags})

    def test_duplicate_case_id_across_files(self):
        c = load_case(2)
        c["case_id"] = "case-001"
        self.write_case("case_002.json", c)
        rep = V.validate_dir(self.dir)
        self.assertIn(V.DUPLICATE_ID, [d.code for d in rep.diags])
        self.assertEqual(len(all_cases()) - 1, len(rep.valid_cases))  # the duplicate is not accepted

    def test_file_name_must_match_case_id(self):
        c = load_case(2)
        c["case_id"] = "case-099"
        self.write_case("case_002.json", c)
        self.assertIn(V.BAD_FILENAME, self.codes_for())

    def test_unexpected_files_and_dirs_are_reported_not_skipped(self):
        (self.dir / "notes.txt").write_text("x")
        (self.dir / "sub").mkdir()
        (self.dir / "case_1.json").write_text("{}")
        rep = V.validate_dir(self.dir)
        names = sorted(d.file for d in rep.diags if d.code == V.UNEXPECTED_FILE)
        self.assertEqual(["case_1.json", "notes.txt", "sub"], names)

    def test_symlink_is_not_followed(self):
        target = self.dir / "case_001.json"
        os.symlink(target, self.dir / "case_900.json")
        self.assertIn(V.UNREADABLE, self.codes_for())

    def test_oversized_file(self):
        (self.dir / "case_002.json").write_text(" " * (T.MAX_FILE_BYTES + 1))
        self.assertIn(V.FILE_TOO_LARGE, self.codes_for())

    def test_non_utf8_file(self):
        (self.dir / "case_002.json").write_bytes(b"\xff\xfe{}")
        self.assertIn(V.UNREADABLE, self.codes_for())

    def test_top_level_array_file(self):
        (self.dir / "case_002.json").write_text("[]")
        self.assertIn(V.WRONG_TYPE, self.codes_for())

    def test_family_siblings_must_share_a_split(self):
        c = load_case(2)  # sibling of case 1 (same family)
        c["split"] = "holdout"
        self.write_case("case_002.json", c)
        self.assertIn(V.SPLIT_CONFLICT, self.codes_for())

    def test_missing_directory(self):
        rep = V.validate_dir(self.dir / "nope")
        self.assertFalse(rep.ok)
        self.assertEqual(V.UNREADABLE, rep.diags[0].code)

    def test_one_bad_file_does_not_hide_the_others(self):
        (self.dir / "case_002.json").write_text("{")
        rep = V.validate_dir(self.dir)
        self.assertEqual(len(all_cases()) - 1, len(rep.valid_cases))
        self.assertEqual(1, len(rep.diags))


if __name__ == "__main__":
    unittest.main()
