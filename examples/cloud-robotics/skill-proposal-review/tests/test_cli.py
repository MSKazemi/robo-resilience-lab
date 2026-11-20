import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = {"PYTHONDONTWRITEBYTECODE": "1", "PATH": ""}
GOOD = b'{"proposal_version":"skill-proposal/v1","skill":"get_status","robot_id":"sim_robot_1","args":{}}'


def run(script, *args, stdin=None):
    return subprocess.run([sys.executable, str(ROOT / script), *args], input=stdin, capture_output=True,
                          timeout=20, env=ENV)


def run_open_stdin(*args, timeout=5):
    """Run with stdin held open and never written: a CLI that blocks on stdin times out and fails."""
    proc = subprocess.Popen([sys.executable, str(ROOT / "review_cli.py"), *args], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=ENV)
    try:
        proc.wait(timeout=timeout)
        return proc.returncode, proc.stdout.read(), proc.stderr.read()
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise AssertionError(f"review_cli.py {args} blocked on stdin")
    finally:
        for f in (proc.stdin, proc.stdout, proc.stderr):
            f.close()


class CliTests(unittest.TestCase):
    def test_evaluate_exit_zero_banner_and_determinism(self):
        a, b = run("evaluate.py"), run("evaluate.py")
        self.assertEqual(a.returncode, 0, a.stderr)
        self.assertEqual(a.stdout, b.stdout)
        out = a.stdout.decode()
        self.assertIn("SYNTHETIC teaching corpus - not a security guarantee", out)
        self.assertIn("RESULT: PASS", out)
        self.assertIn("NOT RUN / not measured", out)

    def test_evaluate_json_parses(self):
        res = run("evaluate.py", "--json")
        self.assertEqual(res.returncode, 0)
        data = json.loads(res.stdout)
        self.assertEqual(data["summary"]["unauthorized_rejected"], data["summary"]["unauthorized_total"])

    def test_evaluate_case_trace_and_usage_errors(self):
        res = run("evaluate.py", "--case", "inj-01")
        self.assertEqual(res.returncode, 0)
        self.assertIn(b"UNKNOWN_FIELD", res.stdout)
        self.assertEqual(run("evaluate.py", "--case", "nope").returncode, 2)
        self.assertEqual(run("evaluate.py", "--case").returncode, 2)

    def test_review_cli_exit_codes(self):
        self.assertEqual(run("review_cli.py", stdin=GOOD).returncode, 0)
        self.assertEqual(run("review_cli.py", stdin=b"rm -rf /").returncode, 4)
        nav = GOOD.replace(b"get_status", b"navigate_to_named_waypoint_sim").replace(
            b'"args":{}', b'"args":{"waypoint":"dock_a"}')
        self.assertEqual(run("review_cli.py", stdin=nav).returncode, 3)
        self.assertEqual(run("review_cli.py", "--context", "bogus", stdin=GOOD).returncode, 2)

    def test_review_cli_help_does_not_read_stdin(self):
        code, out, _ = run_open_stdin("--help")
        self.assertEqual(code, 0)
        self.assertIn(b"usage:", out)
        self.assertIn(b"--context", out)
        self.assertIn(b"Exit codes", out)

    def test_review_cli_usage_errors_exit_2_without_reading_stdin(self):
        for args in (("--bogus",), ("--context",), ("--context", "bogus"), ("extra_positional",)):
            with self.subTest(args=args):
                code, out, err = run_open_stdin(*args)
                self.assertEqual(code, 2)
                self.assertTrue(err, "expected a message on stderr")

    def test_review_cli_oversized_input_is_rejected_not_truncated(self):
        from skill_review.strict_json import MAX_INPUT_BYTES
        # valid JSON padded with spaces past the cap, then garbage: truncating at the cap would look valid
        padded = GOOD + b" " * (MAX_INPUT_BYTES - len(GOOD) + 10) + b"garbage"
        for data in (padded, GOOD + b" " * MAX_INPUT_BYTES):  # second: valid JSON followed by cap spaces
            res = run("review_cli.py", stdin=data)
            self.assertEqual(res.returncode, 4)
            self.assertIn(b"INPUT_TOO_LARGE", res.stdout)

    def test_review_cli_input_at_exact_cap_is_accepted(self):
        from skill_review.strict_json import MAX_INPUT_BYTES
        data = GOOD + b" " * (MAX_INPUT_BYTES - len(GOOD))
        self.assertEqual(len(data), MAX_INPUT_BYTES)
        self.assertEqual(run("review_cli.py", stdin=data).returncode, 0)

    def test_review_cli_read_size_is_tied_to_the_reviewer_cap(self):
        sys.path.insert(0, str(ROOT))
        import review_cli
        from skill_review.strict_json import MAX_INPUT_BYTES
        self.assertGreater(review_cli.READ_BYTES, MAX_INPUT_BYTES)

    def test_review_cli_interactive_terminal_prints_hint_and_exits_2(self):
        try:
            import pty
        except ImportError:  # pragma: no cover - non-POSIX
            self.skipTest("pty not available")
        master, slave = pty.openpty()
        try:
            proc = subprocess.Popen([sys.executable, str(ROOT / "review_cli.py")], stdin=slave,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=ENV)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                self.fail("review_cli.py blocked on an interactive stdin")
            err = proc.stderr.read()
            proc.stdout.close()
            proc.stderr.close()
            self.assertEqual(proc.returncode, 2)
            self.assertIn(b"pipe", err)
        finally:
            os.close(master)
            os.close(slave)

    def test_build_corpus_check(self):
        res = run("build_corpus.py", "--check")
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_catalogue_listing_runs(self):
        res = subprocess.run([sys.executable, "-m", "skill_review.catalogue"], cwd=ROOT, capture_output=True,
                             env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": ""}, timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(b"get_status", res.stdout)


class NoDangerousApiTests(unittest.TestCase):
    """Static guard: the package must not grow eval/exec/subprocess/network/yaml code paths."""

    def test_source_has_no_dangerous_calls(self):
        banned = ("eval(", "exec(", "subprocess", "os.system", "import socket", "urllib", "requests", "yaml",
                  "pickle", "rclpy", "import http")
        for path in sorted((ROOT / "skill_review").glob("*.py")):
            text = path.read_text(encoding="utf-8")
            for token in banned:
                self.assertNotIn(token, text, f"{path.name} contains {token!r}")


if __name__ == "__main__":
    unittest.main()
