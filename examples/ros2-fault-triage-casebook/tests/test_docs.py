"""Documentation says only what the code does: pinned outputs, citations that resolve, hygiene."""

import re
import unittest

from triage import evaluate as E
from triage import taxonomy as T

from .support import REPO_ROOT, ROOT, all_cases

README = (ROOT / "README.md").read_text(encoding="utf-8")


def block(name):
    m = re.search(rf"<!-- BEGIN {name} -->\n```text\n(.*?)\n```\n<!-- END {name} -->", README, re.S)
    assert m, f"README is missing the {name} block"
    return m.group(1) + "\n"


def slug(heading):
    h = heading.strip().lower()
    h = re.sub(r"[^a-z0-9 _-]", "", h)
    return h.replace(" ", "-")


class ReadmePinTests(unittest.TestCase):
    def test_evaluate_block_equals_current_output(self):
        self.assertEqual(E.render_text(E.evaluate(all_cases())), block("evaluate-output"))

    def test_coverage_block_equals_current_output(self):
        self.assertEqual(E.render_coverage(E.coverage_table(all_cases())), block("coverage-output"))

    def test_readme_banner_and_ci_line(self):
        self.assertIn(T.BANNER, README)
        self.assertIn("python3 -m unittest discover -s examples/ros2-fault-triage-casebook/tests -t examples/ros2-fault-triage-casebook", README)

    def test_readme_case_and_family_counts_match_the_fixtures(self):
        cases = all_cases()
        families = {c["incident_family_id"] for c in cases}
        self.assertIn(f"{len(cases)} hand-authored SYNTHETIC incident cases in {len(families)} incident families", README)

    def test_readme_taxonomy_table_lists_every_label_and_its_step(self):
        for label in T.LABELS:
            self.assertIn(f"| `{label}` |", README, label)
            self.assertIn(f"`{T.LABEL_NEXT_STEP[label]}`", README, label)
        self.assertIn(f"(closed; {len(T.LABELS)} labels)", README)

    def test_readme_states_the_limits(self):
        for phrase in ("What this does NOT claim", "circular", "NOT RUN", "cross-worker", "Foxy", "Jazzy"):
            self.assertIn(phrase, README)


class CitationTests(unittest.TestCase):
    def test_every_repository_citation_exists_and_anchors_resolve(self):
        if not (REPO_ROOT / "docs" / "ROS2-KUBERNETES-DDS-GUIDE.md").is_file():
            self.skipTest("repository docs not found next to the casebook (copied out of the repo?)")
        for cid, (kind, target) in T.CITATIONS.items():
            if kind == "external":
                self.assertTrue(target.startswith("https://"), cid)
                continue
            path, _, anchor = target.partition("#")
            f = REPO_ROOT / path
            self.assertTrue(f.is_file(), f"{cid}: {path} does not exist")
            if anchor:
                heads = [slug(m.group(1)) for m in re.finditer(r"^#+\s+(.*)$", f.read_text(encoding="utf-8"), re.M)]
                self.assertIn(anchor, heads, f"{cid}: anchor #{anchor} not found in {path}")

    def test_every_case_cites_only_registered_sources(self):
        for c in all_cases():
            for cid in c["cites"]:
                self.assertIn(cid, T.CITATIONS)

    def test_next_step_registry_is_closed_and_read_only_in_wording(self):
        self.assertEqual(set(T.LABEL_NEXT_STEP.values()), set(T.NEXT_STEPS))
        for step, text in T.NEXT_STEPS.items():
            for bad in ("delete", "restart the", "apply", "chmod", "sudo", "scale"):
                self.assertNotIn(bad, text.lower(), step)


class HygieneTests(unittest.TestCase):
    HAZARD_DIRS = {"results", "runs", "scenarios", "eval", "evaluation", "notes", "plans", "memory", "drafts", "scratch",
                   "research", "papers", "design", "private", "internal", "archive", "measurements", "findings", "raw-data"}
    BAD_SUFFIXES = {".tex", ".bib", ".mp4", ".mov"}

    def test_no_public_firewall_hazard_names_or_suffixes(self):
        for p in ROOT.rglob("*"):
            if "__pycache__" in p.parts:
                continue
            rel = p.relative_to(ROOT)
            self.assertFalse(set(rel.parts) & self.HAZARD_DIRS, str(rel))
            self.assertNotIn(p.suffix, self.BAD_SUFFIXES, str(rel))

    def test_no_ai_attribution_text_in_the_tree(self):
        pat = re.compile("|".join(["Co-Authored" + "-By", "Generated " + "with", "noreply@" + "anthropic"]), re.I)
        for p in ROOT.rglob("*"):
            if p.is_file() and "__pycache__" not in p.parts and p.suffix in {".py", ".md", ".json"}:
                self.assertIsNone(pat.search(p.read_text(encoding="utf-8")), str(p))

    def test_no_non_ascii_in_fixtures_and_sources(self):
        for p in list(ROOT.joinpath("fixtures").glob("*.json")) + list(ROOT.joinpath("triage").glob("*.py")):
            p.read_text(encoding="utf-8").encode("ascii")

    def test_no_network_or_process_imports_in_the_package(self):
        banned = re.compile(r"^\s*(import|from)\s+(socket|subprocess|urllib|http|requests|ssl|ftplib|smtplib)\b", re.M)
        for p in ROOT.joinpath("triage").glob("*.py"):
            self.assertIsNone(banned.search(p.read_text(encoding="utf-8")), str(p))


if __name__ == "__main__":
    unittest.main()
