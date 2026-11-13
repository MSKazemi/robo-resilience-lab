import dataclasses
import unittest

from skill_review import catalogue as cat
from skill_review import reviewer


class CatalogueTests(unittest.TestCase):
    def test_closed_expected_skill_set(self):
        self.assertEqual(
            sorted(cat.CATALOGUE),
            ["cancel_simulated_task", "get_status", "hold_position_sim", "inspect_scene",
             "navigate_to_named_waypoint_sim", "start_approved_benchmark"],
        )

    def test_no_forbidden_names_or_free_form_kinds(self):
        self.assertNotIn("string", cat.ARG_KINDS)
        for spec in cat.CATALOGUE.values():
            for part in cat.FORBIDDEN_NAME_PARTS:
                self.assertNotIn(part, spec.name)
                for a in spec.args:
                    self.assertNotIn(part, a.name)
                    self.assertIn(a.kind, cat.ARG_KINDS)

    def test_catalogue_is_immutable(self):
        with self.assertRaises(TypeError):
            cat.CATALOGUE["run_shell"] = None  # type: ignore[index]

    def test_every_precondition_has_an_evaluator(self):
        for spec in cat.CATALOGUE.values():
            for p in spec.preconditions:
                self.assertIn(p, reviewer._PRECONDITIONS)
        self.assertEqual(set(cat.PRECONDITION_IDS), set(reviewer._PRECONDITIONS))

    def test_consequential_skills_require_state_checks(self):
        for spec in cat.CATALOGUE.values():
            if spec.safety_level == cat.SAFETY_CONSEQUENTIAL:
                self.assertTrue({"state_fresh", "robot_idle", "no_estop"} <= set(spec.preconditions), spec.name)

    def test_numeric_args_have_units_and_bounds(self):
        for spec in cat.CATALOGUE.values():
            for a in spec.args:
                if a.kind in ("number", "integer"):
                    self.assertTrue(a.unit)
                    self.assertLess(a.minimum, a.maximum)

    def test_validate_catalogue_rejects_bad_specs(self):
        good = cat.CATALOGUE["get_status"]
        bad_specs = [
            dataclasses.replace(good, name="run_shell"),
            dataclasses.replace(good, name="Get_Status"),
            dataclasses.replace(good, safety_level="harmless"),
            dataclasses.replace(good, preconditions=("trust_me",)),
            dataclasses.replace(good, args=(cat.ArgSpec("raw_text", "enum", allowed=("a",)),)),
            dataclasses.replace(good, args=(cat.ArgSpec("note", "string"),)),
            dataclasses.replace(good, args=(cat.ArgSpec("speed", "number", minimum=0, maximum=1),)),  # no unit
            dataclasses.replace(good, args=(cat.ArgSpec("v", "enum", allowed=("Bad Value",)),)),
        ]
        for spec in bad_specs:
            with self.assertRaises(ValueError, msg=spec):
                cat.validate_catalogue({spec.name: spec})

    def test_describe_is_deterministic(self):
        self.assertEqual(cat.describe(), cat.describe())
        self.assertIn("navigate_to_named_waypoint_sim", cat.describe())


if __name__ == "__main__":
    unittest.main()
