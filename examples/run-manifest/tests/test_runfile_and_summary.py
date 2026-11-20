"""Tests for run files, the manifest cross-check, the statistics policy and the summary generator."""

import copy
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout

from tests import support as s
from tests.support import load, write_json, rehash_manifest

import rm_metrics as m
import rm_runfile as rf
import rm_stats as st
import rm_summary as sm

c = s.c


def read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


def run_doc(rid=s.LOSSY1):
    return copy.deepcopy(load(os.path.join(s.RUN_DIR, rid + ".json")))


def rcodes(doc):
    return rf.validate_runfile_document(doc).codes


def rpairs(doc):
    return [(d.code, d.path) for d in rf.validate_runfile_document(doc).diagnostics]


class RunFileValidation(unittest.TestCase):
    def test_all_fixture_run_files_are_valid_and_match_their_manifests(self):
        names = sorted(f for f in os.listdir(s.RUN_DIR))
        self.assertEqual(len(names), 9)
        for n in names:
            with self.subTest(n):
                doc = load(os.path.join(s.RUN_DIR, n))
                self.assertEqual(rcodes(doc), [])
                mpath = os.path.join(s.FIX, *doc["manifest_file"].split("/"))
                man = load(mpath)
                self.assertEqual(rf.check_against_manifest(doc, man, s.v.sha256_file(mpath)).codes, [])

    def test_every_registry_metric_is_required(self):
        for mid in m.METRIC_IDS:
            d = run_doc(); del d["metrics"][mid]
            self.assertEqual(rpairs(d), [("METRIC_MISSING", "/metrics/" + mid)])

    def test_unknown_metric_and_version(self):
        d = run_doc(); d["metrics"]["p99_latency_ms"] = {}
        self.assertEqual(rpairs(d), [("UNKNOWN_METRIC", "/metrics/p99_latency_ms")])
        d = run_doc(); d["schema_version"] = "run-metrics/v2"
        self.assertEqual(rcodes(d), ["RUNFILE_VERSION_UNKNOWN"])
        for bad in (None, [], "x", 1):
            self.assertEqual(rcodes(bad), ["MALFORMED_DOCUMENT"])

    def test_units_and_statuses(self):
        d = run_doc(); d["metrics"]["action_age_ms"]["unit"] = "s"
        self.assertEqual(rcodes(d), ["METRIC_UNIT_MISMATCH"])
        d = run_doc(); del d["metrics"]["action_age_ms"]["unit"]
        self.assertEqual(rcodes(d), ["UNIT_MISSING"])
        d = run_doc(); d["metrics"]["action_age_ms"].update(status="censored", value=None, censored_lower_bound=1.0)
        self.assertIn("STATUS_INVALID", rcodes(d))
        d = run_doc(); d["metrics"]["action_age_ms"]["status"] = "great"
        self.assertEqual(rcodes(d)[0], "ENUM_INVALID")
        d = run_doc(); d["metrics"]["action_age_ms"]["value"] = None
        self.assertEqual(rcodes(d), ["VALUE_STATUS_MISMATCH"])
        d = run_doc(); d["metrics"]["recovery_time_ms"].update(status="not_applicable", value=3.0)
        self.assertIn("VALUE_STATUS_MISMATCH", rcodes(d))

    def test_values(self):
        for bad, code in ((True, "BOOL_AS_NUMBER"), (-1.0, "NEGATIVE_VALUE"), ("1", "WRONG_TYPE"), ([1], "WRONG_TYPE"), (float("nan"), "NON_FINITE_VALUE"), (10**400, "VALUE_OUT_OF_RANGE")):
            d = run_doc(); d["metrics"]["action_age_ms"]["value"] = bad
            with self.subTest(repr(bad)[:12]):
                self.assertEqual(rcodes(d), [code])
        d = run_doc(); d["metrics"]["failure_count"]["value"] = 1.5
        self.assertEqual(rcodes(d), ["WRONG_TYPE"])
        d = run_doc(); d["metrics"]["failure_count"]["value"] = True
        self.assertEqual(rcodes(d), ["BOOL_AS_NUMBER"])
        d = run_doc(); d["metrics"]["task_success"]["value"] = 1
        self.assertEqual(rcodes(d), ["WRONG_TYPE"])
        d = run_doc(); d["metrics"]["buffer_starvation_fraction"]["value"] = 1.5
        self.assertIn("VALUE_OUT_OF_RANGE", rcodes(d))

    def test_ratio_fields(self):
        d = run_doc(); d["metrics"]["buffer_starvation_fraction"]["starved_ticks"] += 5
        self.assertEqual(rcodes(d), ["RATIO_INCONSISTENT"])
        d = run_doc(); e = d["metrics"]["deadline_miss_fraction"]; e["missed_actions"] = e["released_actions"] + 1
        self.assertIn("RATIO_INCONSISTENT", rcodes(d))
        d = run_doc(); d["metrics"]["buffer_starvation_fraction"]["control_ticks"] = 0
        self.assertEqual(rcodes(d), ["VALUE_OUT_OF_RANGE"])  # 0/0 is never a value
        d = run_doc(); del d["metrics"]["buffer_starvation_fraction"]["control_ticks"]
        self.assertEqual(rcodes(d), ["MISSING_FIELD"])

    def test_status_specific_fields(self):
        d = run_doc(); d["metrics"]["recovery_time_ms"]["censored_lower_bound"] = 5.0  # ok run must not carry it
        self.assertEqual(rcodes(d), ["FIELD_NOT_ALLOWED"])
        d = run_doc("syn-edge-cloud-lossy-r04"); del d["metrics"]["recovery_time_ms"]["censored_lower_bound"]
        self.assertEqual(rcodes(d), ["MISSING_FIELD"])
        d = run_doc("syn-edge-cloud-single-run-r01"); d["metrics"]["action_age_ms"]["reason"] = "because"
        self.assertEqual(rcodes(d), ["ENUM_INVALID"])
        d = run_doc("syn-edge-cloud-single-run-r01"); del d["metrics"]["action_age_ms"]["reason"]
        self.assertEqual(rcodes(d), ["MISSING_FIELD"])
        d = run_doc(); d["metrics"]["action_age_ms"]["n_samples"] = 0
        self.assertEqual(rcodes(d), ["VALUE_OUT_OF_RANGE"])

    def test_hostile_run_file_never_raises(self):
        doc = run_doc()
        hostile = [None, True, 0, -1, 1.5, float("nan"), "", "x", "\ud800", [], [[]], {}, {"a": {}}]
        for path, _ in s.walk(doc):
            for h in hostile:
                d = copy.deepcopy(doc)
                s.set_path(d, path, h)
                res = rf.validate_runfile_document(d)
                self.assertNotIn("INTERNAL_ERROR", res.codes, (path, h))

    def test_duplicate_keys_in_run_file_text(self):
        doc, diags = s.v.d.parse_json_text(json.dumps(run_doc()).replace('"arm":', '"arm": "a", "arm":', 1))
        self.assertEqual(diags, [])
        self.assertEqual([(d.code, d.path) for d in rf.validate_runfile_document(doc).diagnostics], [("DUPLICATE_KEY", "/arm")])


class CrossCheck(unittest.TestCase):
    def _pair(self, rid=s.LOSSY1):
        doc = run_doc(rid)
        mpath = os.path.join(s.FIX, *doc["manifest_file"].split("/"))
        return doc, copy.deepcopy(load(mpath)), s.v.sha256_file(mpath)

    def test_mismatches(self):
        doc, man, sha = self._pair()
        self.assertEqual(rf.check_against_manifest(doc, man, "0" * 64).codes, ["RUNFILE_MANIFEST_MISMATCH"])
        for key, new in (("independent_run_id", "other-id"), ("arm", "other-arm"), ("data_kind", "measured")):
            d = copy.deepcopy(doc); d[key] = new
            with self.subTest(key):
                self.assertEqual(rf.check_against_manifest(d, man, sha).codes, ["RUNFILE_MANIFEST_MISMATCH"])

    def test_unsynchronized_clocks_forbid_cross_host_values(self):
        doc, man, sha = self._pair()
        man["clocks"] = {"clock_basis": "wall", "clock_domain": "unsynchronized", "simulation_time": False}
        res = rf.check_against_manifest(doc, man, sha)
        self.assertEqual(sorted(set(res.codes)), ["CLOCK_REQUIREMENT_VIOLATED"])
        offending = sorted(d.path for d in res.diagnostics)
        self.assertEqual(offending, sorted("/metrics/%s/status" % mid for mid in m.METRIC_IDS if m.BY_ID[mid]["clock_scope"] == "same_clock_domain"))
        # the single-run fixture is unsynchronized and does it right
        d2, m2, s2 = self._pair("syn-edge-cloud-single-run-r01")
        self.assertEqual(m2["clocks"]["clock_domain"], "unsynchronized")
        self.assertEqual(rf.check_against_manifest(d2, m2, s2).codes, [])
        # reason clock_unsynchronized on a synchronized manifest is a lie
        d3, m3, s3 = self._pair("syn-edge-cloud-single-run-r01")
        m3["clocks"] = {"clock_basis": "monotonic", "clock_domain": "single_clock", "simulation_time": False}
        self.assertIn("RUNFILE_MANIFEST_MISMATCH", rf.check_against_manifest(d3, m3, s3).codes)

    def test_applicability_follows_the_manifest(self):
        doc, man, sha = self._pair()
        man["fault_injection"] = {"applied": False}
        self.assertEqual(rf.check_against_manifest(doc, man, sha).codes, ["RUNFILE_MANIFEST_MISMATCH"])
        doc, man, sha = self._pair(s.LOCAL1)
        man["fault_injection"] = {"applied": True, "kind": "k", "duration": {"value": 1, "unit": "s"}}
        self.assertEqual(rf.check_against_manifest(doc, man, sha).codes, ["RUNFILE_MANIFEST_MISMATCH"])
        doc, man, sha = self._pair()
        man["outcome"]["task_success"] = {"applicable": False, "value": None}
        self.assertEqual(rf.check_against_manifest(doc, man, sha).codes, ["RUNFILE_MANIFEST_MISMATCH"])
        doc, man, sha = self._pair(s.LOCAL1)
        man["outcome"]["task_success"] = {"applicable": True, "value": True}
        self.assertEqual(rf.check_against_manifest(doc, man, sha).codes, ["RUNFILE_MANIFEST_MISMATCH"])

    def test_failure_count_must_equal_manifest_and_never_be_omitted(self):
        doc, man, sha = self._pair()
        doc["metrics"]["failure_count"]["value"] += 1
        self.assertEqual(rf.check_against_manifest(doc, man, sha).codes, ["RUNFILE_MANIFEST_MISMATCH"])
        doc, man, sha = self._pair()
        doc["metrics"]["failure_count"] = {"status": "not_computable", "value": None, "unit": "count", "reason": "no_samples"}
        self.assertEqual(rf.check_against_manifest(doc, man, sha).codes, ["RUNFILE_MANIFEST_MISMATCH"])


class LoadSet(unittest.TestCase):
    def setUp(self):
        self.tmp, self.fix, self.runs = s.copy_fixture_tree()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def load(self):
        return rf.load_runfile_set(self.runs, self.fix)

    def test_fixture_set_loads_cleanly(self):
        runs, problems = self.load()
        self.assertEqual(problems, [])
        self.assertEqual(len(runs), 9)

    def test_duplicate_run_id_is_rejected_loudly(self):
        src = os.path.join(self.runs, "syn-local-baseline-r01.json")
        shutil.copy(src, os.path.join(self.runs, "syn-local-baseline-r01-copy.json"))
        _, problems = self.load()
        self.assertEqual([p[1].code for p in problems], ["DUPLICATE_RUN_ID"])

    def test_concatenated_run_file_is_rejected(self):
        p = os.path.join(self.runs, "syn-local-baseline-r01.json")
        with open(p, encoding="utf-8") as fh:
            text = fh.read()
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text + text)
        _, problems = self.load()
        self.assertEqual([x[1].code for x in problems], ["MALFORMED_JSON"])

    def test_non_synthetic_and_missing_manifest_and_stray_files(self):
        p = os.path.join(self.runs, "syn-local-baseline-r01.json")
        d = load(p); d["data_kind"] = "measured"; write_json(p, d)
        _, problems = self.load()
        self.assertEqual([x[1].code for x in problems], ["NOT_SYNTHETIC"])
        d["data_kind"] = "synthetic"; write_json(p, d)
        os.remove(os.path.join(self.fix, "manifests", "valid", "syn-local-baseline-r01.json"))
        _, problems = self.load()
        self.assertEqual([x[1].code for x in problems], ["FILE_UNREADABLE"])
        with open(os.path.join(self.runs, "notes.txt"), "w") as fh:
            fh.write("x")
        _, problems = self.load()
        self.assertIn("UNKNOWN_FIELD", [x[1].code for x in problems])  # silent skips are bugs

    def test_tampered_manifest_is_detected_by_hash(self):
        mp = os.path.join(self.fix, "manifests", "valid", "syn-local-baseline-r02.json")
        man = load(mp); man["description"] = "edited after the fact"; write_json(mp, man)
        _, problems = self.load()
        self.assertEqual([x[1].code for x in problems], ["RUNFILE_MANIFEST_MISMATCH"])
        rehash_manifest(self.fix, "syn-local-baseline-r02")  # re-sealing it (legitimately regenerated) is accepted
        _, problems = self.load()
        self.assertEqual(problems, [])

    def test_invalid_manifest_blocks_summary(self):
        mp = os.path.join(self.fix, "manifests", "valid", "syn-local-baseline-r02.json")
        man = load(mp); del man["clocks"]; write_json(mp, man)
        rehash_manifest(self.fix, "syn-local-baseline-r02")
        _, problems = self.load()
        self.assertEqual([x[1].code for x in problems], ["CLOCK_MISSING"])

    def test_empty_and_missing_directory(self):
        empty = os.path.join(self.tmp, "empty"); os.mkdir(empty)
        self.assertNotEqual(rf.load_runfile_set(empty, self.fix)[1], [])
        self.assertEqual(rf.load_runfile_set(os.path.join(self.tmp, "nope"), self.fix)[1][0][1].code, "FILE_UNREADABLE")

    def test_unsafe_manifest_path_is_not_followed(self):
        p = os.path.join(self.runs, "syn-local-baseline-r01.json")
        d = load(p); d["manifest_file"] = "../../../etc/passwd"; write_json(p, d)
        _, problems = self.load()
        self.assertEqual([x[1].code for x in problems], ["PATH_UNSAFE"])


class StatisticsPolicy(unittest.TestCase):
    def test_no_interval_below_three_runs(self):
        self.assertIsNone(st.ci_mean([], 1))
        self.assertIsNone(st.ci_mean([1.0], 1))
        self.assertIsNone(st.ci_mean([1.0, 2.0], 1))
        self.assertIsNotNone(st.ci_mean([1.0, 2.0, 3.0], 1))
        self.assertIsNone(st.ci_diff_mean([1.0, 2.0], [1.0, 2.0, 3.0], 1))
        self.assertIsNone(st.ci_diff_mean([1.0, 2.0, 3.0], [5.0], 1))
        self.assertEqual(st.NO_CLAIM, "n<3: no statistical claim")

    def test_bootstrap_is_seeded_and_bounded_by_the_data(self):
        a = st.ci_mean([1.0, 2.0, 9.0, 4.0], 7)
        self.assertEqual(a, st.ci_mean([1.0, 2.0, 9.0, 4.0], 7))
        self.assertTrue(1.0 <= a[0] <= a[1] <= 9.0)
        self.assertEqual(st.ci_mean([5.0, 5.0, 5.0], 3), (5.0, 5.0))
        self.assertNotEqual(st.derived_seed("a", "b"), st.derived_seed("b", "a"))
        self.assertEqual(st.derived_seed("a", "b"), st.derived_seed("a", "b"))

    def test_mean_median_fmt(self):
        self.assertEqual(st.median([3, 1, 2]), 2)
        self.assertEqual(st.median([4, 1, 2, 3]), 2.5)
        self.assertEqual(st.fmt(-0.0001), "0")
        self.assertEqual(st.fmt(12.5), "12.5")
        self.assertEqual(st.fmt(1234.0), "1234")


class SummaryContent(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runs, problems = rf.load_runfile_set(s.RUN_DIR, s.FIX)
        assert not problems
        cls.files = sm.build_outputs(cls.runs)
        cls.agg = sm.aggregate(cls.runs)

    def test_every_output_is_labelled_synthetic(self):
        self.assertEqual(sorted(self.files), ["SUMMARY.md", "action_age_ms.svg", "deadline_miss_fraction.svg", "recovery_time_ms.svg"])
        for name, text in self.files.items():
            with self.subTest(name):
                self.assertIn("SYNTHETIC", text)
                if name.endswith(".svg"):
                    self.assertIn("NOT A MEASUREMENT", text)
                    self.assertIn("<title", text)
        self.assertTrue(self.files["SUMMARY.md"].startswith("# SYNTHETIC"))

    def test_svgs_are_well_formed_and_self_contained(self):
        import xml.etree.ElementTree as ET
        for name, text in self.files.items():
            if name.endswith(".svg"):
                root = ET.fromstring(text)
                self.assertTrue(root.tag.endswith("svg"))
                for bad in ("http://", "https://", "<script", "href=", "<image", "<foreignObject"):
                    if bad == "http://":
                        self.assertEqual(text.count("http://"), 1, name)  # only the xmlns declaration
                    else:
                        self.assertNotIn(bad, text, name)

    def test_n_below_three_prints_refusal_and_no_interval(self):
        single = self.agg["edge-cloud-single-run"]["metrics"]["request_serve_response_ms"]
        self.assertEqual(single["n_ok"], 1)
        self.assertIsNone(single["ci"])
        self.assertEqual(single["note"], st.NO_CLAIM)
        per_metric = self.files["SUMMARY.md"].split("## Per-metric summary")[1].split("## Arm comparisons")[0]
        rows = [l for l in per_metric.splitlines() if "`edge-cloud-single-run`" in l and l.startswith("| `request_serve")]
        self.assertEqual(len(rows), 1)
        self.assertIn("n<3: no statistical claim", rows[0])
        self.assertNotIn("[", rows[0])  # no interval brackets

    def test_no_significance_language_or_p_values(self):
        rows = [l.lower() for l in self.files["SUMMARY.md"].splitlines() if l.startswith("| `")]
        self.assertGreater(len(rows), 40)
        for line in rows:  # table cells only; the prose says "no p-values" on purpose
            for word in ("p-value", "p <", "p=", "significant", "significance", "confirmed", "proves"):
                self.assertNotIn(word, line)

    def test_censored_cell_reports_no_mean_median_or_interval(self):
        cell = self.agg["edge-cloud-lossy"]["metrics"]["recovery_time_ms"]
        self.assertEqual((cell["counts"]["ok"], cell["counts"]["censored"]), (3, 1))
        self.assertIsNone(cell["mean"]); self.assertIsNone(cell["median"]); self.assertIsNone(cell["ci"])
        self.assertIn("censored", cell["note"])
        self.assertEqual(cell["bounds"], [35000.0])

    def test_not_independent_runs_are_excluded_and_counted(self):
        runs = list(self.runs)
        for i, r in enumerate(runs):
            if r.run_id == "syn-local-baseline-r04":
                runs[i] = rf.Run(r.file, r.run_id, r.arm, False, r.metrics, r.manifest, r.manifest_sha256, r.file_sha256)
        agg = sm.aggregate(runs)
        a = agg["local-baseline"]
        self.assertEqual((a["runs"], a["independent"], a["excluded"]), (4, 3, 1))
        self.assertEqual(a["metrics"]["action_age_ms"]["n_ok"], 3)
        self.assertIsNotNone(a["metrics"]["action_age_ms"]["ci"])  # 3 independent runs is the minimum
        runs2 = [rf.Run(r.file, r.run_id, r.arm, False if r.run_id.endswith(("r03", "r04")) and r.arm == "local-baseline" else r.fresh_deployment, r.metrics, r.manifest, r.manifest_sha256, r.file_sha256) for r in self.runs]
        two = sm.aggregate(runs2)["local-baseline"]["metrics"]["action_age_ms"]
        self.assertEqual(two["n_ok"], 2)
        self.assertIsNone(two["ci"])
        self.assertEqual(two["note"], st.NO_CLAIM)

    def test_two_runs_give_no_interval_in_any_table(self):
        runs = [r for r in self.runs if r.arm == "local-baseline" and r.run_id[-3:] in ("r01", "r02")]
        files = sm.build_outputs(runs)
        md = files["SUMMARY.md"]
        table = md.split("## Per-metric summary")[1].split("## Arm comparisons")[0]
        for line in table.splitlines():
            if line.startswith("| `") and "local-baseline" in line and "not applicable" not in line and "not_applicable" not in line:
                self.assertTrue("n<3: no statistical claim" in line or "no values" in line, line)
                self.assertNotRegex(line, r"\[\s*-?[0-9]")

    def test_no_comparison_interval_unless_both_arms_have_three(self):
        md = self.files["SUMMARY.md"]
        comp = md.split("## Arm comparisons")[1].split("## Plots")[0]
        for line in comp.splitlines():
            if line.startswith("| `"):
                cells = [x.strip() for x in line.strip("|").split("|")]
                involves_single = "`edge-cloud-single-run`" in cells[1:3]
                has_interval = cells[4].startswith("[")
                if involves_single:
                    self.assertFalse(has_interval, line)
                    self.assertEqual(cells[4], "n<3: no statistical claim")

    def test_regeneration_matches_committed_reports_byte_for_byte(self):
        self.assertEqual(sm.check_outputs(self.files, sm.DEFAULT_OUT_DIR), [])
        again = sm.build_outputs(rf.load_runfile_set(s.RUN_DIR, s.FIX)[0])
        self.assertEqual(again, self.files)


class SummaryCli(unittest.TestCase):
    def setUp(self):
        self.tmp, self.fix, self.runs = s.copy_fixture_tree()
        self.out = os.path.join(self.tmp, "out")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = sm.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def base_args(self):
        return ["--run-dir", self.runs, "--base-dir", self.fix, "--out-dir", self.out]

    def test_write_then_check_then_tamper(self):
        code, _, _ = self.run_cli(*self.base_args())
        self.assertEqual(code, 0)
        self.assertEqual(self.run_cli(*self.base_args(), "--check")[0], 0)
        committed = {n: read_bytes(os.path.join(sm.DEFAULT_OUT_DIR, n)) for n in os.listdir(sm.DEFAULT_OUT_DIR)}
        regenerated = {n: read_bytes(os.path.join(self.out, n)) for n in os.listdir(self.out)}
        self.assertEqual(committed, regenerated)  # the one offline command recreates the committed plots byte-identically
        with open(os.path.join(self.out, "action_age_ms.svg"), "a") as fh:
            fh.write("<!-- tampered -->")
        code, _, err = self.run_cli(*self.base_args(), "--check")
        self.assertEqual(code, 1)
        self.assertIn("DRIFT action_age_ms.svg", err)
        with open(os.path.join(self.out, "stray.txt"), "w") as fh:
            fh.write("x")
        self.assertIn("stray", self.run_cli(*self.base_args(), "--check")[2])

    def test_refuses_and_writes_nothing_on_bad_input(self):
        os.remove(os.path.join(self.runs, "syn-local-baseline-r01.json"))
        shutil.copy(os.path.join(self.runs, "syn-local-baseline-r02.json"), os.path.join(self.runs, "dup.json"))
        code, _, err = self.run_cli(*self.base_args())
        self.assertEqual(code, 1)
        self.assertIn("DUPLICATE_RUN_ID", err)
        self.assertIn("nothing written", err)
        self.assertFalse(os.path.exists(self.out))

    def test_cli_refuses_a_consistent_non_synthetic_pair(self):
        # A measured manifest + measured run file are internally consistent, so only the NOT_SYNTHETIC gate can stop them.
        mp = os.path.join(self.fix, "manifests", "valid", "syn-local-baseline-r01.json")
        man = load(mp); man["data_kind"] = "measured"; man["measurement_source"]["kind"] = "ros2_tracing"; write_json(mp, man)
        rp = os.path.join(self.runs, "syn-local-baseline-r01.json")
        run = load(rp); run["data_kind"] = "measured"; write_json(rp, run)
        rehash_manifest(self.fix, "syn-local-baseline-r01")
        self.assertEqual(s.v.validate_file(mp).codes, [])
        code, _, err = self.run_cli(*self.base_args())
        self.assertEqual(code, 1)
        self.assertIn("NOT_SYNTHETIC", err)
        self.assertFalse(os.path.exists(self.out))

    def test_usage_error_exit_code(self):
        self.assertEqual(self.run_cli("--bogus")[0], 2)

    def test_output_is_independent_of_hash_randomisation(self):
        outs = []
        for seed in ("0", "12345"):
            env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONDONTWRITEBYTECODE="1")
            out_dir = os.path.join(self.tmp, "h" + seed)
            subprocess.run([sys.executable, os.path.join(s.ROOT, "rm_summary.py"), "--run-dir", self.runs, "--base-dir", self.fix, "--out-dir", out_dir],
                           check=True, env=env, capture_output=True)
            outs.append({n: hashlib.sha256(read_bytes(os.path.join(out_dir, n))).hexdigest() for n in sorted(os.listdir(out_dir))})
        self.assertEqual(outs[0], outs[1])


class AxisTicks(unittest.TestCase):
    def test_nice_ticks_cover_range_and_terminate(self):
        for vmax in (0, 1e-9, 0.3, 1, 7, 99.9, 1234.5, 35000.0, 1e11):
            t = sm.nice_ticks(vmax)
            self.assertGreaterEqual(t[-1], vmax)
            self.assertLessEqual(len(t), 12)
            self.assertEqual(t, sorted(t))


if __name__ == "__main__":
    unittest.main()
