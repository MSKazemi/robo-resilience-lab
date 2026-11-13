"""Shared helpers for the tests (stdlib unittest only)."""

import copy
import json
import unittest

from skill_review import reviewer
from skill_review.reviewer import ACCEPT, NEEDS_HUMAN, REJECT, review

V = "skill-proposal/v1"

IDLE = {"robot_id": "sim_robot_1", "robot_state": "idle", "state_age_ms": 120,
        "active_task_id": None, "estop_sim": False}
MOVING = dict(IDLE, robot_state="moving", active_task_id="task-42")


def ctx(**over):
    out = copy.deepcopy(IDLE)
    out.update(over)
    return out


def env(**over):
    base = {"proposal_version": V, "skill": "get_status", "robot_id": "sim_robot_1", "args": {}}
    base.update(over)
    return base


def J(obj):
    return json.dumps(obj, separators=(",", ":"))


def nav(**args):
    return env(skill="navigate_to_named_waypoint_sim", args=args)


def bench(**args):
    return env(skill="start_approved_benchmark", args=args)


_DEFAULT = object()


class ReviewCase(unittest.TestCase):
    def assertReject(self, data, code, context=_DEFAULT, msg=None):
        res = review(data, ctx() if context is _DEFAULT else context)
        self.assertEqual(res.verdict, REJECT, msg or res.as_dict())
        self.assertEqual(res.primary_reason, code, msg or res.as_dict())
        self.assertIsNone(res.call)
        self.assertIsNone(res.pending_call)
        return res

    def assertAccept(self, data, context=_DEFAULT):
        res = review(data, ctx() if context is _DEFAULT else context)
        self.assertEqual(res.verdict, ACCEPT, res.as_dict())
        self.assertEqual(res.reasons, ())
        self.assertIsNotNone(res.call)
        return res

    def assertHuman(self, data, code, context=_DEFAULT):
        res = review(data, ctx() if context is _DEFAULT else context)
        self.assertEqual(res.verdict, NEEDS_HUMAN, res.as_dict())
        self.assertEqual(res.primary_reason, code, res.as_dict())
        self.assertIsNone(res.call)
        return res
