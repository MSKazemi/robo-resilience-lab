"""Repository hygiene: simulation-only imports, mutation patterns in sync, docs cover every reason."""

import ast
import json
import unittest
from pathlib import Path

import mutation_check
from tests.support import ROOT, Reason, g

CORE = ("gate_core.py", "sim_sink.py", "harness.py", "scenarios.py")
ALLOWED_IMPORTS = {
    "__future__", "collections", "dataclasses", "enum", "importlib", "json", "math", "pathlib", "re",
    "types", "typing", "gate_core", "sim_sink", "harness",
}
FORBIDDEN_NAMES = {"socket", "subprocess", "ctypes", "rclpy", "serial", "requests", "urllib", "http", "asyncio",
                   "threading", "multiprocessing", "os", "sys", "time", "random", "datetime", "shutil", "pickle"}


class NoHardwareTests(unittest.TestCase):
    def test_core_modules_import_only_the_allowed_standard_library_subset(self):
        for name in CORE:
            tree = ast.parse((ROOT / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    mods = [node.module.split(".")[0]]
                for m in mods:
                    with self.subTest(file=name, module=m):
                        self.assertIn(m, ALLOWED_IMPORTS)
                        self.assertNotIn(m, FORBIDDEN_NAMES)

    def test_core_modules_do_not_open_files_run_commands_or_call_the_clock(self):
        banned = ("open(", "eval(", "exec(", "__import__", "os.system", "Popen", "time.time", "time.monotonic")
        for name in CORE:
            text = (ROOT / name).read_text(encoding="utf-8")
            for b in banned:
                if name == "gate_core.py" and b == "exec(":
                    continue  # spec.loader.exec_module(...) loads the sibling contract file
                with self.subTest(file=name, banned=b):
                    self.assertNotIn(b, text)

    def test_gate_reads_its_joint_layout_from_the_mock_policy_contract(self):
        schema = json.loads((ROOT.parent / "mock-policy" / "schema" / "action.v1.schema.json")
                            .read_text(encoding="utf-8"))
        props = schema["properties"]
        self.assertEqual(props["action_space"]["const"], g.ACTION_SPACE)
        self.assertEqual(props["units"]["const"], list(g.JOINT_UNITS))
        self.assertIn("set_joint_targets", props["command"]["enum"])
        self.assertIn("set_joint_targets", g.COMMANDS)
        limits = [(p["minimum"], p["maximum"]) for p in props["values"]["prefixItems"]]
        self.assertEqual(limits, [tuple(x) for x in g.JOINT_LIMITS])


class MutationPatternTests(unittest.TestCase):
    def test_every_mutation_pattern_matches_exactly_once_and_changes_something(self):
        ids = [m[0] for m in mutation_check.MUTANTS]
        self.assertEqual(len(ids), len(set(ids)))
        for mid, fname, old, new, what in mutation_check.MUTANTS:
            with self.subTest(mutant=mid):
                src = (ROOT / fname).read_text(encoding="utf-8")
                self.assertEqual(src.count(old), 1, f"{mid}: pattern must occur exactly once in {fname}")
                self.assertNotEqual(old, new)
                self.assertTrue(what)


class DocsTests(unittest.TestCase):
    def test_readme_and_threat_model_exist_and_cover_every_reason_code(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        threat = (ROOT / "THREAT-MODEL.md").read_text(encoding="utf-8")
        for reason in Reason:
            with self.subTest(reason=reason.value):
                self.assertIn(reason.value, readme + threat)

    def test_readme_states_what_is_not_claimed(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("What this does NOT claim", readme)
        for phrase in ("certif", "real-time", "hardware", "motor"):
            self.assertIn(phrase, readme)

    def test_readme_commands_name_existing_files(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for name in ("run_fault_suite.py", "mutation_check.py", "reports/fault-suite.simulation.json"):
            self.assertIn(name, readme)
            self.assertTrue((ROOT / name).exists(), name)


if __name__ == "__main__":
    unittest.main()
