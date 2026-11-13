"""Positive and negative tests for the run-manifest validator (contract run-manifest/v1).

Run from the repository root (stdlib only, no network):

    python3 -m unittest discover -s examples/run-manifest/tests -t examples/run-manifest -v
"""

import copy
import json
import os
import unittest

from tests import support as s
from tests.support import base, codes, pairs, set_path, del_path

v, c = s.v, s.c
LOCAL1 = s.LOCAL1


class ValidFixtures(unittest.TestCase):
    def test_every_valid_fixture_passes_and_has_no_warnings(self):
        files = sorted(f for f in os.listdir(s.VALID_DIR) if f.endswith(".json"))
        self.assertGreaterEqual(len(files), 10)
        for f in files:
            with self.subTest(f):
                res = v.validate_file(os.path.join(s.VALID_DIR, f))
                self.assertEqual([d.as_dict() for d in res.diagnostics], [])
                self.assertFalse(res.truncated)

    def test_data_hashes_verify_against_real_files(self):
        doc = base()
        self.assertEqual(v.verify_data_hashes(doc, s.ROOT).codes, [])

    def test_data_hash_mismatch_and_missing_and_unsafe(self):
        doc = base()
        doc["data_hashes"][0]["sha256"] = "ab" * 31 + "cd"
        self.assertEqual(v.verify_data_hashes(doc, s.ROOT).codes, ["HASH_MISMATCH"])
        doc = base()
        doc["data_hashes"][0]["path"] = "fixtures/nope.json"
        self.assertEqual(v.verify_data_hashes(doc, s.ROOT).codes, ["HASH_FILE_MISSING"])
        doc = base()
        doc["data_hashes"][0]["path"] = "../outside"
        self.assertEqual(v.verify_data_hashes(doc, s.ROOT).codes, ["PATH_UNSAFE"])
        self.assertEqual(v.verify_data_hashes([], s.ROOT).codes, ["MISSING_FIELD"])  # never raises on junk


class InvalidFixtures(unittest.TestCase):
    def test_each_invalid_fixture_gives_exactly_its_expected_diagnostics(self):
        expected = s.load(os.path.join(s.FIX, "manifests", "expected_diagnostics.json"))
        self.assertGreaterEqual(len(expected), 25)
        on_disk = sorted("invalid/" + f for f in os.listdir(s.INVALID_DIR))
        self.assertEqual(sorted(expected), on_disk)  # no fixture without an expectation and vice versa
        for name, exp in expected.items():
            with self.subTest(name):
                res = v.validate_file(os.path.join(s.FIX, "manifests", name))
                self.assertFalse(res.valid)
                self.assertEqual(sorted((d.code, d.path) for d in res.diagnostics), sorted((e["code"], e["path"]) for e in exp))
                for d in res.diagnostics:
                    self.assertTrue(d.hint and d.message)


class RequiredEvidence(unittest.TestCase):
    """The issue's definition of done: missing units, revisions, digests and clocks are caught."""

    def test_missing_unit_variants(self):
        d = base(); del_path(d, ("timing", "action_age_deadline", "unit"))
        self.assertEqual(pairs(d), [("UNIT_MISSING", "/timing/action_age_deadline/unit")])
        d = base(); set_path(d, ("timing", "action_age_deadline"), 200)  # bare number
        self.assertEqual(pairs(d), [("UNIT_MISSING", "/timing/action_age_deadline")])
        d = base(); set_path(d, ("run", "warmup"), 10.5)
        self.assertEqual(pairs(d), [("UNIT_MISSING", "/run/warmup")])
        d = base(); set_path(d, ("run", "warmup", "unit"), None)
        self.assertEqual(pairs(d), [("WRONG_TYPE", "/run/warmup/unit")])

    def test_unit_must_match_dimension(self):
        for path, bad in ((("run", "timeout", "unit"), "Mbit/s"), (("timing", "control_rate", "unit"), "s"), (("hardware", "memory", "unit"), "ms"), (("network", "loss", "unit"), "Hz")):
            with self.subTest(path):
                d = base(); set_path(d, path, bad)
                self.assertEqual(codes(d), ["UNIT_NOT_ALLOWED"])

    def test_unit_conversion_is_used_for_warmup_vs_timeout(self):
        d = base(); set_path(d, ("run", "warmup"), {"value": 60000, "unit": "ms"})  # == 60 s == timeout
        self.assertEqual(codes(d), ["WARMUP_EXCEEDS_TIMEOUT"])
        d = base(); set_path(d, ("run", "warmup"), {"value": 59999, "unit": "ms"})
        self.assertEqual(codes(d), [])

    def test_missing_revisions_and_digests(self):
        for path, code in ((("source", "git_sha"), "REVISION_MISSING"), (("model", "revision"), "REVISION_MISSING"),
                           (("images", 0, "oci_digest"), "DIGEST_MISSING"), (("data_hashes", 0, "sha256"), "DIGEST_MISSING")):
            with self.subTest(path):
                d = base(); del_path(d, path)
                self.assertEqual(codes(d), [code])
        d = base(); del_path(d, ("source",))
        self.assertEqual(codes(d), ["REVISION_MISSING"])
        d = base(); d["images"] = []
        self.assertEqual(pairs(d), [("DIGEST_MISSING", "/images")])
        d = base(); d["data_hashes"] = []
        self.assertEqual(pairs(d), [("DIGEST_MISSING", "/data_hashes")])
        d = base(); d["dataset"] = {"used": True, "name": "ds", "license": "CC0-1.0"}
        self.assertEqual(pairs(d), [("REVISION_MISSING", "/dataset/revision")])
        d = base(); d["simulator"] = {"used": True, "name": "sim"}
        self.assertEqual(pairs(d), [("REVISION_MISSING", "/simulator/version")])

    def test_missing_clock_information(self):
        d = base(); del_path(d, ("clocks",))
        self.assertEqual(pairs(d), [("CLOCK_MISSING", "/clocks")])
        for k in ("clock_basis", "clock_domain"):
            d = base(); del_path(d, ("clocks", k))
            self.assertEqual(pairs(d), [("CLOCK_MISSING", "/clocks/" + k)])
        d = base(); del_path(d, ("clocks", "sync_method"))
        self.assertEqual(codes(d), ["CLOCK_SYNC_INCOMPLETE"])
        d = base(LOCAL1)  # single_clock must not carry sync fields
        d["clocks"]["estimated_max_offset"] = {"value": 1, "unit": "ms"}
        self.assertEqual(codes(d), ["CLOCK_SYNC_INCOMPLETE"])
        d = base(); set_path(d, ("clocks", "clock_domain"), "same-ish")
        self.assertEqual(codes(d), ["ENUM_INVALID"])
        d = base(); set_path(d, ("clocks", "clock_basis"), "realtime")
        self.assertEqual(codes(d), ["ENUM_INVALID"])

    def test_licences(self):
        d = base(); del_path(d, ("model", "license"))
        self.assertEqual(codes(d), ["LICENSE_MISSING"])
        d = base(); del_path(d, ("code_license",))
        self.assertEqual(codes(d), ["LICENSE_MISSING"])
        d = base(); set_path(d, ("model", "license"), "")
        self.assertEqual(codes(d), ["LICENSE_MISSING"])
        d = base(); set_path(d, ("model", "license"), None)
        self.assertEqual(codes(d), ["LICENSE_MISSING"])
        for bad in ("unknown", "None", "NOASSERTION", "n/a", "TBD", " other "):
            d = base(); set_path(d, ("model", "license"), bad)
            with self.subTest(bad):
                self.assertIn(codes(d)[0], ("LICENSE_UNDECLARED", "LICENSE_MALFORMED"))
        d = base(); set_path(d, ("model", "license"), "MIT License")
        self.assertEqual(codes(d), ["LICENSE_MALFORMED"])
        d = base(); set_path(d, ("model", "license"), "Apache-2.0")  # a licence merely CONTAINING a bad word is fine
        self.assertEqual(codes(d), [])
        d = base(); set_path(d, ("model", "license"), "LicenseRef-none-of-the-above")
        self.assertEqual(codes(d), [])  # whole-value placeholder match, not substring


class Identifiers(unittest.TestCase):
    def test_hash_shapes(self):
        sha = base()["source"]["git_sha"]
        for bad in (sha[:39], sha + "a", sha.upper(), "g" * 40, "", " " + sha[1:], sha + "\n"):
            d = base(); set_path(d, ("source", "git_sha"), bad)
            with self.subTest(repr(bad)):
                self.assertEqual(codes(d), ["HASH_MALFORMED" if bad else "EMPTY_STRING"])
        dig = base()["images"][0]["oci_digest"]
        for bad in (dig.upper(), dig[7:], "sha512:" + dig[7:], dig + "\n", "sha256:abc"):
            d = base(); set_path(d, ("images", 0, "oci_digest"), bad)
            with self.subTest(bad[:20]):
                self.assertEqual(codes(d), ["HASH_MALFORMED"])

    def test_placeholder_hashes_are_rejected_by_whole_value(self):
        for ph in ("0" * 40, "a" * 40, "deadbeef" * 5, "1234" * 10, "ab" * 20):
            d = base(); set_path(d, ("source", "git_sha"), ph)
            with self.subTest(ph):
                self.assertEqual(codes(d), ["PLACEHOLDER_VALUE"])
        d = base(); set_path(d, ("source", "git_sha"), "0" * 39 + "1")  # one differing char is not a repeating pattern
        self.assertEqual(codes(d), [])

    def test_ids(self):
        for bad in ("run 1", "", "-run", "run/1", "x" * 65, "run\n", "r\u00fcn", "run\u0661"):
            d = base(); set_path(d, ("run", "independent_run_id"), bad)
            with self.subTest(repr(bad)):
                self.assertEqual(len(codes(d)), 1)
                self.assertIn(codes(d)[0], ("ID_MALFORMED", "EMPTY_STRING"))
        for ph in ("TODO", "tbd", "None", "CHANGEME", "Placeholder"):
            d = base(); set_path(d, ("run", "independent_run_id"), ph)
            with self.subTest(ph):
                self.assertEqual(codes(d), ["PLACEHOLDER_VALUE"])
        d = base(); set_path(d, ("run", "independent_run_id"), "todo-1")  # contains, does not equal
        self.assertEqual(codes(d), [])
        d = base(); set_path(d, ("run", "independent_run_id"), "x" * 64)
        self.assertEqual(codes(d), [])

    def test_versions(self):
        for bad in ("1 29", "", "-1", "1.29\n"):
            d = base(); set_path(d, ("software", "rmw_version"), bad)
            self.assertEqual(len(codes(d)), 1, bad)
        d = base(); set_path(d, ("software", "kubernetes_version"), "v1.29.3")
        self.assertEqual(codes(d), ["VERSION_MALFORMED"])
        d = base(); set_path(d, ("software", "kubernetes_version"), 1.29)  # a float is not a version
        self.assertEqual(codes(d), ["WRONG_TYPE"])
        d = base(); set_path(d, ("software", "kubernetes_version"), "1.29")
        self.assertEqual(codes(d), [])


class Timestamps(unittest.TestCase):
    def test_utc_strings(self):
        good = ("2026-01-01T00:00:00Z", "2026-12-31T23:59:59.999999Z", "2026-02-28T00:00:00.5Z")
        bad_malformed = ("2026-02-30T00:00:00Z", "2026-13-01T00:00:00Z", "2026-01-01T24:00:00Z", "2026-01-01T00:00:60Z",
                         "2026-01-01 00:00:00Z", "2026-01-01T00:00:00z", "2026-01-01T00:00:00Z\n", " 2026-01-01T00:00:00Z",
                         "2026-01-01T00:00:00.1234567Z", "1999-01-01T00:00:00Z", "2026-1-1T00:00:00Z", "", "yesterday",
                         "\u0662026-01-01T00:00:00Z", "2026-01-01T00:00:00Z\u0000")
        bad_zone = ("2026-01-01T00:00:00", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00-05:00", "2026-01-01T00:00:00+0100")
        # ended is later than every 'good' start, so only the string format is under test.
        for t in good:
            d = base(); set_path(d, ("run", "started_utc"), t); set_path(d, ("run", "ended_utc"), "2027-01-01T00:00:00Z")
            with self.subTest(t):
                self.assertEqual(codes(d), [])
        for t in bad_malformed:
            d = base(); set_path(d, ("run", "started_utc"), t)
            with self.subTest(repr(t)):
                self.assertEqual(len(codes(d)), 1)
                self.assertIn(codes(d)[0], ("TIMESTAMP_MALFORMED", "EMPTY_STRING"))
        for t in bad_zone:
            d = base(); set_path(d, ("run", "started_utc"), t)
            with self.subTest(t):
                self.assertEqual(codes(d), ["TIMESTAMP_NOT_UTC"])

    def test_equal_times_ok_reversed_not(self):
        d = base(); d["run"]["ended_utc"] = d["run"]["started_utc"]  # half-open window of zero length is allowed
        self.assertEqual(codes(d), [])
        d = base(); d["run"]["ended_utc"] = "2000-01-01T00:00:00Z"
        self.assertEqual(codes(d), ["TIME_ORDER"])

    def test_timestamp_wrong_types(self):
        for bad in (20260101, None, ["2026-01-01T00:00:00Z"], True):
            d = base(); set_path(d, ("run", "started_utc"), bad)
            self.assertEqual(codes(d), ["WRONG_TYPE"])


class NumbersAndTypes(unittest.TestCase):
    def test_bool_is_not_a_number(self):
        for path in (("run", "seed"), ("run", "run_index"), ("qos", 0, "depth"), ("hardware", "cpu", "logical_cores"),
                     ("action_space", "dimension"), ("outcome", "failure_count"), ("run", "timeout", "value"), ("timing", "control_rate", "value")):
            for b in (True, False):
                d = base(); set_path(d, path, b)
                with self.subTest((path, b)):
                    self.assertEqual(codes(d), ["BOOL_AS_NUMBER"])

    def test_non_bool_where_bool_required(self):
        for path in (("run", "fresh_deployment"), ("source", "git_dirty"), ("simulator", "used"), ("outcome", "censored")):
            for bad in (0, 1, "true", [True], None):
                d = base(); set_path(d, path, bad)
                with self.subTest((path, bad)):
                    self.assertEqual(codes(d), ["WRONG_TYPE"])

    def test_collections_where_scalars_belong(self):
        for path in (("run", "seed"), ("run", "run_index"), ("arm",), ("experiment_id",), ("run", "timeout", "value"), ("run", "timeout", "unit"),
                     ("software", "kubernetes_version"), ("model", "license"), ("clocks", "clock_domain"), ("qos", 0, "reliability"),
                     ("outcome", "status"), ("data_kind",), ("schema_version",)):
            for bad in ([], [1], [[]], {}, {"a": 1}, {"a": {}}):
                d = base(); set_path(d, path, bad)
                with self.subTest((path, bad)):
                    res = v.validate_document(d)
                    self.assertFalse(res.valid)
                    self.assertNotIn("INTERNAL_ERROR", res.codes)
                    self.assertIn("WRONG_TYPE", res.codes)  # typed as a type error, not a generic enum/format miss

    def test_scalars_where_collections_belong(self):
        for path in (("run",), ("placement",), ("qos",), ("hardware", "cpu"), ("network",), ("images",), ("action_space", "names"),
                     ("action_space", "units"), ("data_hashes",), ("fault_injection",), ("outcome", "task_success")):
            for bad in ("x", 1, True, None, 1.5):
                d = base(); set_path(d, path, bad)
                with self.subTest((path, bad)):
                    self.assertEqual(codes(d)[0], "WRONG_TYPE")

    def test_fractional_where_integer(self):
        for path in (("run", "seed"), ("qos", 0, "depth"), ("outcome", "failure_count")):
            d = base(); set_path(d, path, 2.5)
            self.assertEqual(codes(d), ["WRONG_TYPE"])
        d = base(); set_path(d, ("run", "seed"), 3.0)  # 3.0 is a float, not an integer literal
        self.assertEqual(codes(d), ["WRONG_TYPE"])

    def test_negative_zero_and_absurd_values(self):
        d = base(); set_path(d, ("run", "warmup", "value"), -0.001)
        self.assertEqual(codes(d), ["NEGATIVE_VALUE"])
        d = base(); set_path(d, ("run", "warmup", "value"), 0)  # warmup 0 is allowed
        self.assertEqual(codes(d), [])
        d = base(); set_path(d, ("run", "timeout", "value"), 0)
        self.assertEqual(codes(d), ["NON_POSITIVE_VALUE"])
        d = base(); set_path(d, ("timing", "control_rate", "value"), 0)
        self.assertEqual(codes(d), ["NON_POSITIVE_VALUE"])
        for big in (10**400, 1e13, 2**64):
            d = base(); set_path(d, ("network", "bandwidth", "value"), big)
            with self.subTest(str(big)[:12]):
                self.assertEqual(codes(d), ["VALUE_OUT_OF_RANGE"])
        d = base(); set_path(d, ("run", "seed"), 2**63)
        self.assertEqual(codes(d), ["VALUE_OUT_OF_RANGE"])
        d = base(); set_path(d, ("run", "seed"), 2**63 - 1)
        self.assertEqual(codes(d), [])
        d = base(); set_path(d, ("run", "seed"), -1)
        self.assertEqual(codes(d), ["NEGATIVE_VALUE"])

    def test_non_finite_is_reported_exactly_once_per_occurrence(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            d = base(); set_path(d, ("run", "timeout", "value"), bad)
            self.assertEqual(pairs(d), [("NON_FINITE_VALUE", "/run/timeout/value")])
            d = base(); set_path(d, ("run", "seed"), bad)
            self.assertEqual(pairs(d), [("NON_FINITE_VALUE", "/run/seed")])
        d = base(); set_path(d, ("network", "loss", "value"), float("nan")); set_path(d, ("network", "jitter", "value"), float("inf"))
        self.assertEqual(sorted(codes(d)), ["NON_FINITE_VALUE", "NON_FINITE_VALUE"])

    def test_loss_ratio_bounds(self):
        d = base(); set_path(d, ("network", "loss"), {"value": 100, "unit": "percent"})
        self.assertEqual(codes(d), [])
        d = base(); set_path(d, ("network", "loss"), {"value": 100.5, "unit": "percent"})
        self.assertEqual(codes(d), ["VALUE_OUT_OF_RANGE"])
        d = base(); set_path(d, ("network", "loss"), {"value": 1.5, "unit": "fraction"})
        self.assertEqual(codes(d), ["VALUE_OUT_OF_RANGE"])

    def test_strings(self):
        for bad, code in (("", "EMPTY_STRING"), ("x" * 129, "STRING_TOO_LONG"), ("tab\there", "STRING_INVALID"), ("caf\u00e9", "STRING_INVALID"), ("a\nb", "STRING_INVALID"), ("\ud800", "STRING_INVALID")):
            d = base(); set_path(d, ("hardware", "os"), bad)
            with self.subTest(repr(bad)):
                self.assertEqual(codes(d), [code])


class Structure(unittest.TestCase):
    def test_unknown_and_extension_fields(self):
        d = base(); d["sed"] = 1
        self.assertEqual(pairs(d), [("UNKNOWN_FIELD", "/sed")])
        d = base(); d["run"]["sead"] = 1
        self.assertEqual(pairs(d), [("UNKNOWN_FIELD", "/run/sead")])
        d = base(); d["x_lab_note"] = {"anything": [1, 2]}
        self.assertEqual(codes(d), [])

    def test_unknown_schema_version_stops_further_checks(self):
        d = base(); d["schema_version"] = "run-manifest/v2"; del d["run"]
        self.assertEqual(codes(d), ["SCHEMA_VERSION_UNKNOWN"])
        for bad in ("run-manifest/v1 ", "run-manifest/v1\n", "RUN-MANIFEST/V1", "run-manifest/v10", ""):
            d = base(); d["schema_version"] = bad
            self.assertEqual(codes(d), ["SCHEMA_VERSION_UNKNOWN"], repr(bad))
        d = base(); del d["schema_version"]
        self.assertEqual(codes(d), ["MISSING_FIELD"])
        d = base(); d["schema_version"] = 1
        self.assertEqual(codes(d), ["WRONG_TYPE"])

    def test_top_level_must_be_an_object(self):
        for bad in (None, [], [base()], "x", 1, True, 1.5):
            res = v.validate_document(bad)
            self.assertEqual(res.codes, ["MALFORMED_DOCUMENT"])

    def test_checking_continues_after_the_first_problem(self):
        d = base()
        set_path(d, ("run", "seed"), True)
        set_path(d, ("run", "started_utc"), "yesterday")
        del_path(d, ("model", "revision"))
        del_path(d, ("clocks", "clock_domain"))
        set_path(d, ("action_space", "actuation"), "enabled")
        got = set(codes(d))
        self.assertTrue({"BOOL_AS_NUMBER", "TIMESTAMP_MALFORMED", "REVISION_MISSING", "CLOCK_MISSING", "ACTUATION_NOT_NONE"} <= got, got)

    def test_diagnostic_cap_sets_truncated(self):
        d = base()
        d["qos"] = [{"topic": "bad"} for _ in range(c.MAX_LIST)]
        res = v.validate_document(d)
        self.assertEqual(len(res.diagnostics), c.MAX_DIAGNOSTICS)
        self.assertTrue(res.truncated)

    def test_arrays_must_agree_with_dimension(self):
        d = base(); d["action_space"]["names"].pop()
        self.assertEqual(pairs(d), [("DIM_MISMATCH", "/action_space/names")])
        d = base(); d["action_space"]["units"].append("rad")
        self.assertEqual(pairs(d), [("DIM_MISMATCH", "/action_space/units")])
        d = base(); d["action_space"]["names"][1] = d["action_space"]["names"][0]
        self.assertEqual(codes(d), ["DUPLICATE_ENTRY"])
        d = base(); d["action_space"]["units"][2] = ""
        self.assertEqual(codes(d), ["EMPTY_STRING"])
        d = base(); del d["action_space"]["units"]
        self.assertEqual(pairs(d), [("UNIT_MISSING", "/action_space/units")])

    def test_duplicates(self):
        d = base(); d["qos"].append(copy.deepcopy(d["qos"][0]))
        self.assertEqual(pairs(d), [("DUPLICATE_ENTRY", "/qos/2/topic")])
        d = base(); d["placement"].append(copy.deepcopy(d["placement"][0]))
        self.assertEqual(pairs(d), [("DUPLICATE_ENTRY", "/placement/4/component")])
        d = base(); d["images"].append(copy.deepcopy(d["images"][0]))
        self.assertEqual(pairs(d), [("DUPLICATE_ENTRY", "/images/2/name")])
        d = base(); d["data_hashes"].append(copy.deepcopy(d["data_hashes"][0]))
        self.assertEqual(pairs(d), [("DUPLICATE_ENTRY", "/data_hashes/1/path")])

    def test_conditional_sections(self):
        d = base(); d["hardware"]["gpu"] = {"present": True}
        self.assertEqual(sorted(p for _, p in pairs(d)), ["/hardware/gpu/count", "/hardware/gpu/driver_version", "/hardware/gpu/model"])
        d = base(); d["hardware"]["gpu"] = {"present": False, "model": "x"}
        self.assertEqual(codes(d), ["FIELD_NOT_ALLOWED"])
        d = base(); d["model"] = {"used": False, "name": "x"}
        self.assertEqual(codes(d), ["FIELD_NOT_ALLOWED"])
        d = base(); d["model"] = {"used": True}
        self.assertEqual(sorted(codes(d)), ["LICENSE_MISSING", "MISSING_FIELD", "REVISION_MISSING"])
        d = base(LOCAL1); d["network"]["bandwidth"] = {"value": 1, "unit": "Mbit/s"}
        self.assertEqual(codes(d), ["FIELD_NOT_ALLOWED"])
        d = base(); del d["network"]["shaping_tool"]
        self.assertEqual(codes(d), ["MISSING_FIELD"])
        d = base(); d["fault_injection"] = {"applied": True}
        self.assertEqual(codes(d), ["FAULT_INCONSISTENT", "FAULT_INCONSISTENT"])
        d = base(LOCAL1); d["fault_injection"] = {"applied": False, "kind": "x"}
        self.assertEqual(codes(d), ["FAULT_INCONSISTENT"])
        d = base(); d["qos"][0]["history"] = "keep_all"
        self.assertEqual(codes(d), ["FIELD_NOT_ALLOWED"])
        d = base(); del d["qos"][0]["depth"]
        self.assertEqual(codes(d), ["MISSING_FIELD"])
        d = base(); d["qos"][0]["reliability"] = "maybe"
        self.assertEqual(pairs(d), [("ENUM_INVALID", "/qos/0/reliability")])
        d = base(); d["qos"][0]["topic"] = "observation"
        self.assertEqual(pairs(d), [("ID_MALFORMED", "/qos/0/topic")])


class RunSemantics(unittest.TestCase):
    def test_censoring_rules(self):
        d = base(); d["outcome"].update(status="aborted", failure_count=1)  # aborted but censored=false
        self.assertEqual(codes(d), ["CENSORING_INCONSISTENT"])
        d = base(); d["outcome"].update(censored=True, censor_reason="stopped")  # completed but censored
        self.assertEqual(codes(d), ["CENSORING_INCONSISTENT"])
        d = base(); d["outcome"]["censor_reason"] = "why"  # reason without censoring
        self.assertEqual(codes(d), ["CENSORING_INCONSISTENT"])
        d = base(); d["outcome"].update(status="failed", censored=True, failure_count=1)
        self.assertEqual(codes(d), ["CENSOR_REASON_MISSING"])
        d = base(); d["outcome"].update(status="failed", censored=True, failure_count=1, censor_reason="   ")
        self.assertEqual(codes(d), ["CENSOR_REASON_MISSING"])
        d = base(); d["outcome"].update(status="failed", censored=False, failure_count=1)  # failed need not be censored
        self.assertEqual(codes(d), [])
        d = base(); d["outcome"].update(status="failed", failure_count=0)
        self.assertEqual(codes(d), ["FAILURE_COUNT_INCONSISTENT"])

    def test_completed_run_cannot_exceed_timeout(self):
        d = base(); d["outcome"]["observed_duration"] = {"value": 60.001, "unit": "s"}
        self.assertEqual(codes(d), ["TIMEOUT_EXCEEDED"])
        d = base(); d["outcome"]["observed_duration"] = {"value": 60, "unit": "s"}  # equal is allowed
        self.assertEqual(codes(d), [])
        d = base(); d["outcome"].update(status="failed", failure_count=1, observed_duration={"value": 90, "unit": "s"})
        self.assertEqual(codes(d), [])  # a failed run may report a longer overrun

    def test_task_success_only_when_applicable(self):
        d = base(); d["outcome"]["task_success"] = {"applicable": False, "value": False}
        self.assertEqual(codes(d), ["TASK_SUCCESS_INCONSISTENT"])
        d = base(); d["outcome"]["task_success"] = {"applicable": True, "value": None}
        self.assertEqual(codes(d), ["TASK_SUCCESS_INCONSISTENT"])
        d = base(); d["outcome"]["task_success"] = {"applicable": True, "value": 1}
        self.assertEqual(codes(d), ["WRONG_TYPE"])
        d = base(); d["outcome"]["task_success"] = {"applicable": True, "value": False}
        self.assertEqual(codes(d), [])
        d = base(); del d["outcome"]["task_success"]["value"]
        self.assertEqual(codes(d), ["MISSING_FIELD"])

    def test_dirty_tree_is_a_warning_not_an_error(self):
        d = base(); d["source"]["git_dirty"] = True
        self.assertEqual(codes(d), [])  # synthetic: no warning
        d["data_kind"] = "measured"; d["measurement_source"]["kind"] = "ros2_tracing"
        res = v.validate_document(d)
        self.assertTrue(res.valid)
        self.assertEqual([(x.code, x.severity) for x in res.diagnostics], [("SOURCE_DIRTY", "warning")])

    def test_data_kind_consistency(self):
        d = base(); d["measurement_source"]["kind"] = "application_log"
        self.assertEqual(codes(d), ["MEASUREMENT_KIND_MISMATCH"])
        d = base(); d["data_kind"] = "measured"
        self.assertEqual(codes(d), ["MEASUREMENT_KIND_MISMATCH"])
        d = base(); d["data_kind"] = "simulated"; d["measurement_source"]["kind"] = "simulator_log"
        self.assertEqual(codes(d), ["SIMULATOR_REQUIRED"])
        d = base(); d["data_kind"] = "simulated"; d["measurement_source"]["kind"] = "simulator_log"; d["simulator"] = {"used": True, "name": "sim", "version": "1.0"}
        self.assertEqual(codes(d), [])


class SafetyHeuristics(unittest.TestCase):
    def test_actuation_must_be_none(self):
        for bad in ("enabled", "None", "none ", "NONE", ""):
            d = base(); set_path(d, ("action_space", "actuation"), bad)
            self.assertEqual(codes(d), ["ACTUATION_NOT_NONE"], repr(bad))
        for bad in (False, None, ["none"], 0):
            d = base(); set_path(d, ("action_space", "actuation"), bad)
            self.assertEqual(codes(d), ["WRONG_TYPE"])
        d = base(); del d["action_space"]["actuation"]
        self.assertEqual(codes(d), ["MISSING_FIELD"])

    def test_ipv4_addresses_rejected_but_versions_allowed(self):
        for text in ("192.0.2.10", "host 10.0.0.1 here", "edge@172.16.0.5:8080", "0.0.0.0"):
            d = base(); set_path(d, ("description",), text)
            with self.subTest(text):
                self.assertEqual(pairs(d), [("ADDRESS_IN_VALUE", "/description")])
        for ok in ("1.2.3", "300.1.1.1", "1.2.3.4.5", "v1.2.3", "ratio 1.5"):
            d = base(); set_path(d, ("description",), ok)
            with self.subTest(ok):
                self.assertEqual(codes(d), [])
        d = base(); set_path(d, ("software", "rmw_version"), "1.2.3.4")  # a 4-part version is legitimate
        self.assertEqual(codes(d), [])

    def test_secret_like_values_and_keys(self):
        fake = {
            "github": "ghp_" + "Ab3" * 10,
            "hf": "hf_" + "Zy9" * 10,
            "aws": "AKIA" + "ABCDEFGH12345678",
            "pem": "-----BEGIN " + "RSA PRIVATE KEY-----",
            "jwt": "eyJ" + "a" * 12 + "." + "b" * 12 + "." + "c" * 5,
            "slack": "xox" + "b-" + "1234567890-abcdef",
            "k8s_join": "abcdef." + "0123456789abcdef",
        }
        for name, val in fake.items():
            d = base(); set_path(d, ("description",), val)
            with self.subTest(name):
                self.assertEqual(pairs(d), [("SECRET_LIKE_VALUE", "/description")])
        d = base(); set_path(d, ("description",), "prefix ghp_" + "Ab3" * 10 + " suffix")  # substring is detected
        self.assertEqual(codes(d), ["SECRET_LIKE_VALUE"])
        d = base(); set_path(d, ("description",), "abcdef.0123456789abcdef.extra")  # join-token shape is whole-value only
        self.assertEqual(codes(d), [])
        for key in ("x_password", "x_API_KEY", "x_kubeconfig", "x_my_Token", "x_private_key"):
            d = base(); d[key] = "v"
            with self.subTest(key):
                self.assertEqual(codes(d), ["SECRET_LIKE_KEY"])

    def test_paths(self):
        for bad in ("/etc/passwd", "../x", "a/../b", "a//b", "a b", "./a", "a\\b", "", "a/", "C:\\x"):
            d = base(); set_path(d, ("data_hashes", 0, "path"), bad)
            with self.subTest(bad):
                self.assertIn(codes(d)[0], ("PATH_UNSAFE", "EMPTY_STRING"))


class ParsingAndFiles(unittest.TestCase):
    def test_duplicate_keys_at_any_depth(self):
        text = json.dumps(base(), indent=2)
        top = text.replace('"arm":', '"arm": "x",\n  "arm":', 1)
        self.assertEqual([(d.code, d.path) for d in v.validate_text(top).diagnostics], [("DUPLICATE_KEY", "/arm")])
        nested = text.replace('"depth": 5,', '"depth": 5, "depth": 6,', 1)
        self.assertEqual([(d.code, d.path) for d in v.validate_text(nested).diagnostics], [("DUPLICATE_KEY", "/qos/0/depth")])
        three = '{"a": 1, "a": 2, "a": 3}'  # one diagnostic per repeated occurrence
        self.assertEqual(v.validate_text(three).codes.count("DUPLICATE_KEY"), 2)
        in_list = '{"x_l": [{"k": 1, "k": 2}]}'
        self.assertIn(("DUPLICATE_KEY", "/x_l/0/k"), [(d.code, d.path) for d in v.validate_text(in_list).diagnostics])

    def test_nan_and_infinity_literals_in_text(self):
        text = json.dumps(base(), indent=2).replace('"seed": 1001', '"seed": NaN')
        self.assertEqual([(d.code, d.path) for d in v.validate_text(text).diagnostics], [("NON_FINITE_VALUE", "/run/seed")])
        text = json.dumps(base()).replace('"value": 5,', '"value": -Infinity,', 1)
        self.assertIn("NON_FINITE_VALUE", v.validate_text(text).codes)
        text = json.dumps(base()).replace('"value": 5,', '"value": 1e999,', 1)  # overflows to inf
        self.assertIn("NON_FINITE_VALUE", v.validate_text(text).codes)

    def test_malformed_and_hostile_text(self):
        for text, code in (("", "MALFORMED_JSON"), ("{", "MALFORMED_JSON"), ("{'a': 1}", "MALFORMED_JSON"), ("\ufeff{}", "MALFORMED_JSON"),
                           ("1" * 5000, "MALFORMED_JSON"), ('{"a": 1}{"b": 2}', "MALFORMED_JSON"),
                           ("x" * (c.MAX_FILE_BYTES + 1), "FILE_TOO_LARGE")):
            with self.subTest(text[:10]):
                self.assertEqual(v.validate_text(text).codes, [code])

    def test_very_deep_json_text_is_reported_not_crashed(self):
        for n in (5000, 400000):  # 400000 levels is about 800 KB: under the size cap, over any parser limit
            res = v.validate_text('{"x_d": ' + "[" * n + "]" * n + "}")
            self.assertIn("NESTING_TOO_DEEP", res.codes, n)
            self.assertFalse(res.valid)

    def test_deep_but_parseable_nesting_is_bounded(self):
        deep = {"a": None}
        cur = deep
        for _ in range(200):
            cur["a"] = {"a": None}
            cur = cur["a"]
        d = base(); d["x_deep"] = deep
        self.assertEqual(v.validate_document(d).codes, ["NESTING_TOO_DEEP"])

    def test_cyclic_in_memory_document_terminates(self):
        d = base(); d["x_loop"] = d
        res = v.validate_document(d)
        self.assertIn("NESTING_TOO_DEEP", res.codes)

    def test_non_string_keys_in_memory(self):
        d = base(); d[1] = "x"; d[None] = "y"
        res = v.validate_document(d)
        self.assertFalse(res.valid)
        self.assertNotIn("INTERNAL_ERROR", res.codes)

    def test_files(self):
        res = v.validate_file(os.path.join(s.ROOT, "fixtures", "does-not-exist.json"))
        self.assertEqual(res.codes, ["FILE_UNREADABLE"])
        res = v.validate_file(s.ROOT)  # a directory
        self.assertEqual(res.codes, ["FILE_UNREADABLE"])

    def test_validator_is_pure(self):
        d = base()
        before = json.dumps(d, sort_keys=True)
        v.validate_document(d)
        set_path(d, ("run", "seed"), True)
        before = json.dumps(d, sort_keys=True)
        v.validate_document(d)
        self.assertEqual(json.dumps(d, sort_keys=True), before)  # a rejection leaves its input unchanged

    def test_every_emitted_code_is_documented(self):
        import re
        emitted = set()
        for fname in ("rm_validate.py", "rm_runfile.py", "rm_diag.py", "rm_summary.py"):
            with open(os.path.join(s.ROOT, fname), encoding="utf-8") as fh:
                text = fh.read()
            emitted |= set(re.findall(r'(?:\.add|Diagnostic|mismatch)\(\s*"([A-Z_]+)"', text))
            emitted |= set(re.findall(r'(?:bad_code|code|unknown_code|empty_code)=?\s*"([A-Z_]+)"', text))
            emitted |= set(re.findall(r'MISSING(?:_CODES)?\s*=\s*\{([^}]*)\}', text) and re.findall(r'"([A-Z_]+_MISSING)"', text))
        self.assertGreater(len(emitted), 40)
        self.assertEqual(sorted(e for e in emitted if e not in c.CODES), [])
        self.assertEqual(sorted(k for k in c.CODES if k.upper() != k), [])


if __name__ == "__main__":
    unittest.main()
