"""Drift, schema agreement, registry completeness and CLI exit-code tests."""

import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout

from tests import support as s
from tests.support import base, load, schema_errors

import fixture_gen as g
import rm_metrics as m
import rm_runfile as rf
import rm_schema as sch
import rm_validate as v

c = s.c

MANIFEST_SCHEMA = os.path.join(s.ROOT, "schema", "run-manifest.v1.schema.json")
RUNFILE_SCHEMA = os.path.join(s.ROOT, "schema", "run-metrics.v1.schema.json")
STRUCTURAL_INVALID = (
    "unit_missing", "unit_wrong_dimension", "revision_missing_model", "source_sha_missing", "source_sha_malformed",
    "oci_digest_missing", "clocks_missing", "clock_sync_incomplete", "timestamp_not_utc",
    "timestamp_trailing_newline", "run_id_malformed", "schema_version_unknown", "seed_bool", "seed_list",
    "warmup_negative", "actuation_enabled",
)


class GeneratedFilesAreInSync(unittest.TestCase):
    def test_fixture_gen_check_reports_no_drift(self):
        self.assertEqual(g.drift(g.generate()), [])

    def test_fixture_gen_is_deterministic(self):
        self.assertEqual(g.generate(), g.generate())

    def test_fixture_gen_check_cli_exit_code(self):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        done = subprocess.run([sys.executable, os.path.join(s.ROOT, "fixture_gen.py"), "--check"], capture_output=True, text=True, env=env)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("OK", done.stdout)

    def test_registry_json_matches_python_registry(self):
        self.assertEqual(load(os.path.join(s.ROOT, "schema", "metrics-registry.v1.json")), m.registry())

    def test_metrics_markdown_is_generated_from_the_registry(self):
        with open(os.path.join(s.ROOT, "METRICS.md"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), m.render_markdown())

    def test_schema_files_match_builders(self):
        self.assertEqual(load(MANIFEST_SCHEMA), sch.manifest_schema())
        self.assertEqual(load(RUNFILE_SCHEMA), sch.runfile_schema())


class RegistryCompleteness(unittest.TestCase):
    REQUIRED = ("id", "title", "kind", "unit", "value_domain", "per_run_reduction", "start_event", "end_event",
                "clock_scope", "clock_requirement", "censorable", "censoring_rule", "failed_run_rule", "applicable_when", "does_not_show")

    def test_issue_metric_list_is_covered(self):
        want = {"sensor_to_policy_request_ms", "request_serve_response_ms", "validator_delay_ms", "action_age_ms",
                "buffer_starvation_fraction", "deadline_miss_fraction", "message_gap_max_ms", "recovery_time_ms",
                "failure_count", "task_success"}
        self.assertTrue(want <= set(m.METRIC_IDS))
        self.assertEqual(len(set(m.METRIC_IDS)), len(m.METRIC_IDS))

    def test_every_metric_states_unit_clock_limits_and_censoring(self):
        for met in m.METRICS:
            with self.subTest(met["id"]):
                for k in self.REQUIRED:
                    self.assertIn(k, met)
                    self.assertTrue(met[k] or met[k] is False, k)
                self.assertIn(met["clock_scope"], m.SCOPES)
                self.assertIsInstance(met["censorable"], bool)
                self.assertGreaterEqual(len(met["does_not_show"]), 1)
                self.assertTrue(all(isinstance(x, str) and x for x in met["does_not_show"]))
                self.assertIn(met["value_domain"], ("non_negative_real", "fraction_0_1", "non_negative_int", "boolean"))

    def test_clock_scope_is_consistent_with_events(self):
        cross = {mid for mid in m.METRIC_IDS if m.BY_ID[mid]["clock_scope"] == "same_clock_domain"}
        self.assertEqual(cross, {"sensor_to_policy_request_ms", "action_age_ms", "deadline_miss_fraction", "recovery_time_ms"})
        self.assertEqual({mid for mid in m.METRIC_IDS if m.BY_ID[mid]["censorable"]}, {"recovery_time_ms"})

    def test_markdown_mentions_every_metric_and_the_clock_and_censoring_rules(self):
        md = m.render_markdown()
        for met in m.METRICS:
            self.assertIn(f"## `{met['id']}`", md)
        for phrase in ("Does NOT show", "Censored runs", "Failed runs", "Clock requirement", "not_computable", "never a number"):
            self.assertIn(phrase, md)


class SchemaAgreesWithFixtures(unittest.TestCase):
    """The JSON Schema documents structure; the validator is the authority. They must not disagree on fixtures."""

    @classmethod
    def setUpClass(cls):
        cls.manifest = load(MANIFEST_SCHEMA)
        cls.runfile = load(RUNFILE_SCHEMA)

    def test_top_level_required_keys_match_the_validator(self):
        self.assertEqual(sorted(self.manifest["required"]), sorted(k for k in v.TOP_REQUIRED))
        self.assertEqual(sorted(self.manifest["properties"]), sorted(set(v.TOP_REQUIRED) | set(v.TOP_OPTIONAL)))
        self.assertEqual(self.manifest["properties"]["schema_version"], {"const": c.SCHEMA_VERSION})

    def test_valid_fixtures_satisfy_the_schemas(self):
        for f in sorted(os.listdir(s.VALID_DIR)):
            with self.subTest(f):
                self.assertEqual(schema_errors(load(os.path.join(s.VALID_DIR, f)), self.manifest), [])
        for f in sorted(os.listdir(s.RUN_DIR)):
            with self.subTest(f):
                self.assertEqual(schema_errors(load(os.path.join(s.RUN_DIR, f)), self.runfile), [])

    def test_structurally_invalid_fixtures_violate_the_schema(self):
        for name in STRUCTURAL_INVALID:
            with self.subTest(name):
                doc = load(os.path.join(s.INVALID_DIR, name + ".json"))
                self.assertNotEqual(schema_errors(doc, self.manifest), [])

    def test_schema_conditionals_agree_with_validator(self):
        cases = [
            (("hardware", "gpu"), {"present": True}),
            (("simulator",), {"used": True}),
            (("model",), {"used": True}),
            (("fault_injection",), {"applied": True}),
            (("network",), {"profile": "p", "shaped": True}),
            (("clocks", "estimated_max_offset"), None),
        ]
        for path, val in cases:
            d = base()
            if val is None:
                s.del_path(d, path)
            else:
                s.set_path(d, path, val)
            with self.subTest(path):
                self.assertNotEqual(schema_errors(d, self.manifest), [])
                self.assertFalse(v.validate_document(d).valid)

    def test_checker_itself_is_not_vacuous(self):
        d = base(); d["unexpected"] = 1
        self.assertNotEqual(schema_errors(d, self.manifest), [])
        d = base(); d["run"]["seed"] = True
        self.assertNotEqual(schema_errors(d, self.manifest), [])
        d = base(); d["x_ok"] = {"anything": 1}
        self.assertEqual(schema_errors(d, self.manifest), [])

    def test_run_file_schema_lists_every_registry_metric(self):
        self.assertEqual(sorted(self.runfile["properties"]["metrics"]["required"]), sorted(m.METRIC_IDS))


class ValidatorCli(unittest.TestCase):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = v.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def valid(self):
        return os.path.join(s.VALID_DIR, s.LOSSY1 + ".json")

    def test_exit_codes(self):
        self.assertEqual(self.run_cli(self.valid())[0], 0)
        self.assertEqual(self.run_cli(os.path.join(s.INVALID_DIR, "unit_missing.json"))[0], 1)
        self.assertEqual(self.run_cli(self.valid(), os.path.join(s.INVALID_DIR, "unit_missing.json"))[0], 1)
        self.assertEqual(self.run_cli(os.path.join(s.ROOT, "nope.json"))[0], 2)
        self.assertEqual(self.run_cli()[0], 2)
        self.assertEqual(self.run_cli("--bogus")[0], 2)

    def test_json_report_is_parseable_and_typed(self):
        code, out, _ = self.run_cli("--format", "json", os.path.join(s.INVALID_DIR, "unit_missing.json"))
        rep = json.loads(out)
        self.assertEqual(code, 1)
        self.assertFalse(rep["ok"])
        d = rep["files"][0]["diagnostics"][0]
        self.assertEqual((d["code"], d["path"], d["severity"]), ("UNIT_MISSING", "/timing/action_age_deadline/unit", "error"))
        self.assertTrue(d["hint"] and d["message"])

    def test_list_codes_prints_every_code(self):
        code, out, _ = self.run_cli("--list-codes")
        self.assertEqual(code, 0)
        for k in c.CODES:
            self.assertIn(k, out)
        self.assertIn("SOURCE_DIRTY (warning)", out)

    def test_verify_hashes_flag(self):
        self.assertEqual(self.run_cli(self.valid(), "--verify-hashes", s.ROOT)[0], 0)
        code, out, _ = self.run_cli(self.valid(), "--verify-hashes", os.path.join(s.ROOT, "fixtures"))  # wrong base dir
        self.assertEqual(code, 1)
        self.assertIn("HASH_FILE_MISSING", out)

    def test_hostile_file_names_and_content_do_not_crash_the_cli(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "weirdé\n.json")
            with open(p, "wb") as fh:
                fh.write(b'{"schema_version": "run-manifest/v1", "arm": "\xff"}')
            code, out, _ = self.run_cli(p)
            self.assertEqual(code, 1)
            p2 = os.path.join(tmp, "surrogate.json")
            with open(p2, "w", encoding="utf-8", errors="surrogatepass") as fh:
                fh.write('{"schema_version": "run-manifest/v1", "arm": "\\ud800"}')
            self.assertEqual(self.run_cli(p2)[0], 1)

    def test_runfile_cli(self):
        out, err = io.StringIO(), io.StringIO()
        files = [os.path.join(s.RUN_DIR, f) for f in sorted(os.listdir(s.RUN_DIR))]
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(rf.main(["--base-dir", s.FIX] + files), 0)
            self.assertEqual(rf.main(["--base-dir", os.path.join(s.FIX, "manifests")] + files[:1]), 2)  # manifest not found => I/O error
            self.assertEqual(rf.main(files[:1]), 0)


class DocsExistAndAreHonest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(s.ROOT, "README.md"), encoding="utf-8") as fh:
            cls.readme = fh.read()

    def test_readme_has_required_sections_and_exact_commands(self):
        for needle in (
            "python3 examples/run-manifest/rm_summary.py",
            "python3 examples/run-manifest/rm_summary.py --check",
            "python3 examples/run-manifest/fixture_gen.py --check",
            "python3 -m unittest discover -s examples/run-manifest/tests -t examples/run-manifest",
            "python3 examples/run-manifest/rm_validate.py",
            "What this does NOT claim",
            "n<3: no statistical claim",
            "#14",
            "#15",
            "#16",
            "SYNTHETIC",
        ):
            self.assertIn(needle, self.readme)

    def test_readme_overclaim_words_absent(self):
        low = self.readme.lower()
        for bad in ("validated on", "benchmark shows", "real-time guarantee", "production-ready", "safety-certified"):
            self.assertNotIn(bad, low.replace("not safety-certified", "").replace("no safety-certified", ""))

    def test_public_firewall_hazard_names_are_not_used(self):
        hazards = {"results", "runs", "scenarios", "eval", "evaluation", "notes", "plans", "memory", "drafts", "scratch", "research",
                   "papers", "design", "private", "internal", "archive", "measurements", "findings", "raw-data"}
        for root, dirs, files in os.walk(s.ROOT):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for d in dirs:
                self.assertNotIn(d, hazards, os.path.join(root, d))
        for root, dirs, files in os.walk(s.ROOT):
            for f in files:
                self.assertFalse(f.endswith((".tex", ".bib", ".mp4", ".mov")), f)


if __name__ == "__main__":
    unittest.main()
