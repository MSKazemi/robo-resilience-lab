"""Offline tests for recovery_metrics.py, RH.py and rehealing.py.

No cluster, no kubectl process, no network, no real sleeping. Kubernetes data are
SYNTHETIC hand-written fixtures (DataAnalysis/fixtures_recovery/) or in-test dicts in
the shape of kubectl output. Runs under both:

    cd DataAnalysis && python3 -m unittest tests.test_recovery_metrics
    python3 -m pytest DataAnalysis/tests/test_recovery_metrics.py
"""
import ast
import contextlib
import copy
import csv
import io
import json
import math
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

ANALYSIS_DIR = Path(__file__).resolve().parents[1]
if str(ANALYSIS_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYSIS_DIR))

import RH  # noqa: E402
import recovery_metrics as rm  # noqa: E402
import rehealing  # noqa: E402

FIX = ANALYSIS_DIR / "fixtures_recovery"
NS, SEL = "ros-sim", "app=talker-sim"
RS = "synthetic-replicaset-uid-1"


def fixture(name):
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))


def rows_by_name(rows):
    return {r["measurement"]: r for r in rows}


def analyze(obj, ref="test.json#sha256=0"):
    return rows_by_name(rm.analyze_observation(obj, evidence_ref=ref))


def pod(name, uid, created="2026-01-01T12:00:01Z", ns=NS, labels=None, ready=None,
        transition=None, deleting=None, rs=RS, extra=None):
    meta = {"name": name, "namespace": ns, "uid": uid,
            "labels": {"app": "talker-sim"} if labels is None else labels,
            "creationTimestamp": created}
    if rs:
        meta["ownerReferences"] = [{"uid": rs, "controller": True}]
    if deleting:
        meta["deletionTimestamp"] = deleting
    status = {"phase": "Running"}
    if ready is not None:
        cond = {"type": "Ready", "status": ready}
        if transition:
            cond["lastTransitionTime"] = transition
        status["conditions"] = [cond]
    p = {"metadata": meta, "status": status}
    p.update(extra or {})
    return p


def plist(*pods):
    return {"items": list(pods)}


class TimestampTests(unittest.TestCase):
    def test_z_and_offsets_denote_the_same_instant(self):
        z = rm.parse_k8s_timestamp("2026-01-01T12:00:00Z")
        plus = rm.parse_k8s_timestamp("2026-01-01T14:00:00+02:00")
        minus = rm.parse_k8s_timestamp("2026-01-01T06:30:00-05:30")
        self.assertEqual(z, plus)
        self.assertEqual(z, minus)
        self.assertEqual(plus.utcoffset(), timedelta(hours=2))      # offset preserved, not replaced
        self.assertEqual(minus.utcoffset(), -timedelta(hours=5, minutes=30))

    def test_interval_across_offsets_is_correct(self):
        a = rm.parse_k8s_timestamp("2026-01-01T12:00:00Z")
        b = rm.parse_k8s_timestamp("2026-01-01T14:00:03+02:00")     # 12:00:03Z
        self.assertEqual((b - a).total_seconds(), 3.0)
        # the old bug (replace(tzinfo=utc) on the wall clock digits) would give 7203 s
        self.assertNotEqual((b.replace(tzinfo=timezone.utc) - a).total_seconds(), 3.0)

    def test_fraction(self):
        t = rm.parse_k8s_timestamp("2026-01-01T12:00:00.5Z")
        self.assertEqual(t.microsecond, 500000)
        t = rm.parse_k8s_timestamp("2026-01-01T12:00:00.123456789Z")
        self.assertEqual(t.microsecond, 123456)                    # truncated, documented

    def test_rejected_strings(self):
        bad = [
            "2026-01-01T12:00:00",            # naive
            "2026-01-01 12:00:00Z",           # space separator
            "2026-01-01T12:00:00z",           # lowercase z
            "2026-01-01T12:00:00Z\n",         # trailing newline (fullmatch)
            " 2026-01-01T12:00:00Z",
            "2026-01-01T12:00:00+0200",       # no colon
            "2026-01-01T12:00:00+24:00",
            "2026-01-01T12:00:00+02:60",
            "2026-01-01T12:00:00-00:00",
            "2026-02-30T12:00:00Z",           # impossible date
            "2026-01-01T12:00:60Z",           # leap second not representable
            "2026-01-01T12:00:00.Z",
            "2026-01-01T12:00:00.1234567890Z",
            "٢٠٢٦-01-01T12:00:00Z",   # non-ASCII digits
            "",
        ]
        for text in bad:
            with self.subTest(text=text), self.assertRaises(rm.TimestampError):
                rm.parse_k8s_timestamp(text)

    def test_hostile_types(self):
        for value in (None, 5, 1.5, True, False, float("nan"), [], ["2026-01-01T12:00:00Z"], {}, b"2026"):
            with self.subTest(value=repr(value)), self.assertRaises(rm.TimestampError):
                rm.parse_k8s_timestamp(value)


class ScalarAndJsonTests(unittest.TestCase):
    def test_require_seconds(self):
        self.assertEqual(rm.require_seconds(3, "x"), 3.0)
        self.assertEqual(rm.require_seconds(0, "x"), 0.0)
        for value in (True, False, float("nan"), float("inf"), -1, -0.001, 1e12, "1", None, [1], {"a": 1}):
            with self.subTest(value=repr(value)), self.assertRaises(rm.MalformedInputError):
                rm.require_seconds(value, "x")

    def test_strict_json(self):
        self.assertEqual(rm.load_json_strict('{"a": [1, 2.5]}'), {"a": [1, 2.5]})
        for text in ('{"a": 1, "a": 2}', '{"a": NaN}', '[Infinity]', '[-Infinity]', '[1e999]', "{", "", "not json"):
            with self.subTest(text=text), self.assertRaises(rm.MalformedInputError):
                rm.load_json_strict(text)
        with self.assertRaises(rm.MalformedInputError):
            rm.load_json_strict(b"\xff\xfe")
        for value in (None, 5, ["{}"]):
            with self.assertRaises(rm.MalformedInputError):
                rm.load_json_strict(value)

    def test_selector(self):
        self.assertEqual(rm.parse_selector("app=talker,tier=a.b-c"), {"app": "talker", "tier": "a.b-c"})
        self.assertEqual(rm.parse_selector("example.com/role=x"), {"example.com/role": "x"})
        for bad in ("", "app", "app=", "=x", "app!=x", "app in (x)", "app=x,app=y", "app=x\n", " app=x",
                    "app=x,", "a=b=c", "ros-", "app=x y", None, ["app=x"], {"app": "x"}, 5, True):
            with self.subTest(bad=repr(bad)), self.assertRaises(rm.MalformedInputError):
                rm.parse_selector(bad)

    def test_identifier_validators_use_fullmatch(self):
        for fn in (rm.validate_namespace, rm.validate_sample_id, rm.validate_pod_name, rm.validate_context):
            for bad in ("ok\n", "", None, 5, ["ok"], "a b", "-x", "١"):
                with self.subTest(fn=fn.__name__, bad=repr(bad)), self.assertRaises(rm.MalformedInputError):
                    fn(bad)
        self.assertEqual(rm.validate_namespace("ros-sim"), "ros-sim")
        self.assertEqual(rm.validate_context("arn:aws:eks:r:1:cluster/x@y"), "arn:aws:eks:r:1:cluster/x@y")
        with self.assertRaises(rm.MalformedInputError):
            rm.validate_namespace("Ros")
        with self.assertRaises(rm.MalformedInputError):
            rm.validate_namespace("a" * 64)


class PodParsingTests(unittest.TestCase):
    def good(self):
        return pod("p-a", "uid-a", ready="True", transition="2026-01-01T12:00:00Z")

    def test_parse_good(self):
        v = rm.parse_pod(self.good())
        self.assertEqual((v.name, v.uid, v.controller_uid), ("p-a", "uid-a", RS))
        self.assertTrue(v.is_ready)
        self.assertFalse(v.terminating)

    def test_hostile_shapes_give_typed_error(self):
        def mut(fn):
            p = copy.deepcopy(self.good())
            fn(p)
            return p

        cases = {
            "item list": [],
            "metadata list": mut(lambda p: p.update(metadata=[])),
            "name list": mut(lambda p: p["metadata"].update(name=["a"])),
            "name bool": mut(lambda p: p["metadata"].update(name=True)),
            "name newline": mut(lambda p: p["metadata"].update(name="a\n")),
            "uid dict": mut(lambda p: p["metadata"].update(uid={"a": 1})),
            "uid newline": mut(lambda p: p["metadata"].update(uid="uid\n")),
            "namespace none": mut(lambda p: p["metadata"].update(namespace=None)),
            "labels list": mut(lambda p: p["metadata"].update(labels=["a"])),
            "label int": mut(lambda p: p["metadata"].update(labels={"a": 1})),
            "label bool": mut(lambda p: p["metadata"].update(labels={"a": True})),
            "owners dict": mut(lambda p: p["metadata"].update(ownerReferences={"uid": "x"})),
            "owner item list": mut(lambda p: p["metadata"].update(ownerReferences=[[]])),
            "controller str": mut(lambda p: p["metadata"].update(ownerReferences=[{"uid": "u", "controller": "yes"}])),
            "controller uid list": mut(lambda p: p["metadata"].update(ownerReferences=[{"uid": ["u"], "controller": True}])),
            "two controllers": mut(lambda p: p["metadata"].update(
                ownerReferences=[{"uid": "u1", "controller": True}, {"uid": "u2", "controller": True}])),
            "creation int": mut(lambda p: p["metadata"].update(creationTimestamp=5)),
            "deletion list": mut(lambda p: p["metadata"].update(deletionTimestamp=[])),
            "status list": mut(lambda p: p.update(status=[])),
            "phase dict": mut(lambda p: p["status"].update(phase={})),
            "conditions dict": mut(lambda p: p["status"].update(conditions={})),
            "condition list": mut(lambda p: p["status"].update(conditions=[[]])),
            "condition type list": mut(lambda p: p["status"].update(conditions=[{"type": ["Ready"], "status": "True"}])),
            "condition status bool": mut(lambda p: p["status"].update(conditions=[{"type": "Ready", "status": True}])),
            "ready status odd": mut(lambda p: p["status"].update(conditions=[{"type": "Ready", "status": "maybe"}])),
            "transition int": mut(lambda p: p["status"].update(
                conditions=[{"type": "Ready", "status": "True", "lastTransitionTime": 3}])),
            "duplicate Ready": mut(lambda p: p["status"].update(
                conditions=[{"type": "Ready", "status": "True"}, {"type": "Ready", "status": "False"}])),
        }
        for label, item in cases.items():
            with self.subTest(case=label), self.assertRaises(rm.MalformedInputError):
                rm.parse_pod(item)

    def test_pod_list_shapes_and_duplicates(self):
        for bad in ([], None, "x", {"items": {}}, {"items": None}, {"items": "x"}, {}):
            with self.subTest(bad=repr(bad)), self.assertRaises(rm.MalformedInputError):
                rm.parse_pod_list(bad)
        a = pod("p-a", "uid-a")
        with self.assertRaises(rm.DuplicateRecordError):
            rm.parse_pod_list(plist(a, a))
        with self.assertRaises(rm.DuplicateRecordError):
            rm.parse_pod_list(plist(pod("p-a", "uid-a"), pod("p-a", "uid-b")))      # same name, other uid
        with self.assertRaises(rm.DuplicateRecordError):
            rm.parse_pod_list(plist(pod("p-a", "uid-a"), pod("p-b", "uid-a")))      # same uid, other name

    def test_sanitize_drops_spec_env_annotations_and_is_reparsable(self):
        raw = pod("p-a", "uid-a", ready="True", transition="2026-01-01T12:00:00Z", extra={
            "spec": {"containers": [{"env": [{"name": "TOKEN", "value": "SECRET-VALUE"}]}]}})
        raw["metadata"]["annotations"] = {"note": "SECRET-ANNOTATION"}
        clean = rm.sanitize_pod_list(plist(raw))
        text = json.dumps(clean)
        self.assertNotIn("SECRET", text)
        self.assertNotIn("spec", clean["items"][0])
        self.assertEqual(rm.parse_pod_list(clean)[0].uid, "uid-a")

    def test_select_by_namespace_selector_owner(self):
        views = rm.parse_pod_list(plist(
            pod("a", "u1"),
            pod("b", "u2", ns="ros-other"),
            pod("c", "u3", labels={"app": "other"}),
            pod("d", "u4", rs="synthetic-replicaset-uid-2"),
        ))
        sel = rm.parse_selector(SEL)
        self.assertEqual([v.name for v in rm.select_pods(views, NS, sel)], ["a", "d"])
        self.assertEqual([v.name for v in rm.select_pods(views, NS, sel, owner_uid=RS)], ["a"])
        self.assertEqual([v.name for v in rm.select_pods(views, "ros-other", sel)], ["b"])


class EventParsingTests(unittest.TestCase):
    def test_hostile_shapes(self):
        ev = {"involvedObject": {"kind": "Pod", "uid": "u"}, "reason": "Scheduled", "eventTime": "2026-01-01T12:00:00Z"}
        rm.parse_event_list({"items": [ev]})
        for label, bad in {
            "list": [],
            "items dict": {"items": {}},
            "item list": {"items": [[]]},
            "involved list": {"items": [dict(ev, involvedObject=[])]},
            "reason list": {"items": [dict(ev, reason=["x"])]},
            "uid dict": {"items": [dict(ev, involvedObject={"uid": {}})]},
            "time int": {"items": [dict(ev, eventTime=5)]},
        }.items():
            with self.subTest(case=label), self.assertRaises(rm.MalformedInputError):
                rm.parse_event_list(bad)

    def test_time_field_preference(self):
        ev = {"involvedObject": {"uid": "u"}, "reason": "Scheduled", "firstTimestamp": "2026-01-01T12:00:01Z",
              "lastTimestamp": "2026-01-01T12:00:09Z"}
        self.assertEqual(rm.parse_event_list({"items": [ev]})[0].time_raw, "2026-01-01T12:00:01Z")


class FindReplacementTests(unittest.TestCase):
    def setUp(self):
        self.a = pod("p-a", "uid-a", ready="True")
        self.baseline = rm.parse_pod_list(plist(self.a))
        self.target = self.baseline[0]

    def polls(self, *lists):
        return [rm.parse_pod_list(plist(*pods)) for pods in lists]

    def test_found_by_uid_excluding_terminating_old_pod(self):
        old = pod("p-a", "uid-a", deleting="2026-01-01T12:00:30Z")
        new = pod("p-b", "uid-b")
        r = rm.find_replacement(self.baseline, self.polls([old], [old, new]), self.target)
        self.assertEqual((r.uid, r.first_poll_index), ("uid-b", 1))

    def test_same_name_new_uid_is_a_replacement(self):          # StatefulSet style
        r = rm.find_replacement(self.baseline, self.polls([pod("p-a", "uid-new")]), self.target)
        self.assertEqual(r.uid, "uid-new")

    def test_same_uid_is_never_a_replacement(self):
        with self.assertRaises(rm.NoReplacementPodError):
            rm.find_replacement(self.baseline, self.polls([self.a], [self.a]), self.target)

    def test_no_polls_or_empty_polls(self):
        for polls in ([], [[]], [[], []]):
            with self.subTest(polls=polls), self.assertRaises(rm.NoReplacementPodError):
                rm.find_replacement(self.baseline, self.polls(*polls), self.target)

    def test_two_new_pods_is_ambiguous_even_across_polls(self):
        with self.assertRaises(rm.AmbiguousMatchError):
            rm.find_replacement(self.baseline, self.polls([pod("p-b", "uid-b")], [pod("p-c", "uid-c")]), self.target)

    def test_other_owner_is_excluded(self):
        stranger = pod("p-x", "uid-x", rs="synthetic-replicaset-uid-9")
        with self.assertRaises(rm.NoReplacementPodError):
            rm.find_replacement(self.baseline, self.polls([stranger]), self.target)

    def test_other_baseline_pods_are_not_replacements(self):
        baseline = rm.parse_pod_list(plist(self.a, pod("p-c", "uid-c")))
        new = pod("p-b", "uid-b")
        r = rm.find_replacement(baseline, self.polls([self.a, pod("p-c", "uid-c"), new]), baseline[0])
        self.assertEqual(r.uid, "uid-b")


class FixtureAnalysisTests(unittest.TestCase):
    """Expected numbers were computed by hand from the fixture timestamps (see fixtures_recovery/)."""

    def test_success_basic(self):
        r = analyze(fixture("success_basic"))
        self.assertEqual(r["deletion_request_to_replacement_pod_observed"]["value"], 2.0)     # 1002.0 - 1000.0
        self.assertEqual(r["replacement_pod_observed_to_ready_observed"]["value"], 3.0)       # 1005.0 - 1002.0
        # +02:00 creationTimestamp 14:00:01+02:00 == 12:00:01Z; wall request 12:00:00Z
        self.assertEqual(r["aux_wallclock_request_to_replacement_creation_timestamp"]["value"], 1.0)
        self.assertEqual(r["aux_replacement_creation_to_ready_transition"]["value"], 3.0)     # 12:00:04Z - 12:00:01Z
        self.assertEqual(r["aux_replacement_creation_to_scheduled_event"]["value"], 0.5)
        for name, row in r.items():
            if name.startswith("aux_"):
                self.assertEqual(row["role"], "auxiliary")
        self.assertEqual(r["deletion_request_to_replacement_pod_observed"]["role"], "primary")
        self.assertEqual(r["deletion_request_to_replacement_pod_observed"]["clock"], "monotonic_local")
        row = r["replacement_pod_observed_to_ready_observed"]
        self.assertEqual((row["replacement_pod"], row["replacement_uid"]), ("talker-sim-7d9c5b8f4-bbbbb", "synthetic-pod-b-uid"))
        self.assertEqual(row["target_pod"], "talker-sim-7d9c5b8f4-aaaaa")
        self.assertTrue(row["synthetic"])

    def test_ros_message_recovery_is_not_measured_without_probe(self):
        row = analyze(fixture("success_basic"))["disruption_to_first_restored_ros_message"]
        self.assertEqual((row["status"], row["value"], row["error_code"]), ("not_measured", None, "no_message_probe"))
        self.assertEqual(row["role"], "application")

    def test_started_event_is_never_used(self):
        obs = fixture("success_basic")
        obs["events"] = plist()
        obs["events"]["items"] = [{"involvedObject": {"kind": "Pod", "uid": "synthetic-pod-b-uid"},
                                   "reason": "Started", "eventTime": "2026-01-01T12:00:03Z"}]
        row = analyze(obs)["aux_replacement_creation_to_scheduled_event"]
        self.assertEqual((row["status"], row["value"], row["error_code"]), ("failed", None, "scheduled_event_missing"))

    def test_multi_pod_picks_replacement_by_uid_and_namespace(self):
        r = analyze(fixture("success_multi_pod"))
        row = r["deletion_request_to_replacement_pod_observed"]
        self.assertEqual(row["value"], 1.5)
        self.assertEqual(row["replacement_uid"], "synthetic-pod-e-uid")
        self.assertEqual(r["replacement_pod_observed_to_ready_observed"]["value"], 1.75)
        self.assertEqual(r["aux_wallclock_request_to_replacement_creation_timestamp"]["value"], 1.0)   # +05:30 wall time
        self.assertEqual(r["aux_replacement_creation_to_scheduled_event"]["error_code"], "events_not_collected")

    def test_probe_supplied_is_reported_with_its_own_clock_label(self):
        row = analyze(fixture("success_multi_pod"))["disruption_to_first_restored_ros_message"]
        self.assertEqual((row["status"], row["value"], row["clock"]), ("measured", 2.5, "probe_supplied"))

    def test_probe_variants(self):
        base = fixture("success_multi_pod")
        for label, mutate, status, code in (
            ("restored null", lambda p: p.update(first_restored_message_s=None), "not_measured", "no_message_observed"),
            ("restored before start", lambda p: p.update(first_restored_message_s=1.0), "failed", "malformed_input"),
            ("bool", lambda p: p.update(first_restored_message_s=True), "failed", "malformed_input"),
            ("nan", lambda p: p.update(first_restored_message_s="nan"), "failed", "malformed_input"),
            ("list", lambda p: p.update(disruption_started_s=[1]), "failed", "malformed_input"),
            ("wrong schema", lambda p: p.update(schema="x"), "failed", "malformed_input"),
            ("empty source", lambda p: p.update(source=""), "failed", "malformed_input"),
            ("extra key", lambda p: p.update(extra=1), "failed", "malformed_input"),
        ):
            with self.subTest(label):
                obs = copy.deepcopy(base)
                mutate(obs["probe"])
                row = analyze(obs)["disruption_to_first_restored_ros_message"]
                self.assertEqual((row["status"], row["error_code"], row["value"]), (status, code, None))
        obs = copy.deepcopy(base)
        obs["probe"] = ["not", "a", "dict"]                       # wrong container type: corrupt observation
        with self.assertRaises(rm.MalformedInputError):
            analyze(obs)

    def test_no_replacement_never_yields_a_duration(self):
        rows = rm.analyze_observation(fixture("fail_no_replacement"), evidence_ref="f.json#sha256=0")
        r = rows_by_name(rows)
        for name in [m[0] for m in rm.MEASUREMENTS if m[2] != "probe_supplied"]:
            self.assertEqual((r[name]["status"], r[name]["value"], r[name]["error_code"]),
                             ("failed", None, "no_replacement_pod"), name)
        self.assertEqual(r["deletion_request_to_replacement_pod_observed"]["exit_status"], 4)
        self.assertEqual(r["deletion_request_to_replacement_pod_observed"]["run_error_code"], "poll_timeout")
        self.assertFalse(rm.primaries_ok(rows))

    def test_never_ready_measures_creation_but_not_ready(self):
        r = analyze(fixture("fail_never_ready"))
        self.assertEqual(r["deletion_request_to_replacement_pod_observed"]["value"], 1.0)
        for name in ("replacement_pod_observed_to_ready_observed", "aux_replacement_creation_to_ready_transition"):
            self.assertEqual((r[name]["status"], r[name]["value"], r[name]["error_code"]),
                             ("failed", None, "ready_not_observed"))

    def test_ambiguous_replacement_fails_all_pod_measurements(self):
        r = analyze(fixture("fail_ambiguous_replacement"))
        self.assertEqual(r["deletion_request_to_replacement_pod_observed"]["error_code"], "ambiguous_match")
        self.assertIsNone(r["deletion_request_to_replacement_pod_observed"]["value"])
        self.assertIsNone(r["deletion_request_to_replacement_pod_observed"]["replacement_uid"])

    def test_missing_events_only_fails_the_scheduled_event_row(self):
        r = analyze(fixture("missing_events"))
        self.assertEqual(r["deletion_request_to_replacement_pod_observed"]["value"], 2.0)
        self.assertEqual(r["replacement_pod_observed_to_ready_observed"]["value"], 3.0)
        row = r["aux_replacement_creation_to_scheduled_event"]
        self.assertEqual((row["status"], row["value"], row["error_code"]), ("failed", None, "scheduled_event_missing"))

    def test_events_not_collected_is_not_measured(self):
        obs = fixture("success_basic")
        obs["events"] = None
        row = analyze(obs)["aux_replacement_creation_to_scheduled_event"]
        self.assertEqual((row["status"], row["error_code"]), ("not_measured", "events_not_collected"))

    def test_duplicate_scheduled_events_fail(self):
        obs = fixture("success_basic")
        obs["events"]["items"].append(copy.deepcopy(obs["events"]["items"][0]))
        row = analyze(obs)["aux_replacement_creation_to_scheduled_event"]
        self.assertEqual((row["status"], row["value"], row["error_code"]), ("failed", None, "ambiguous_match"))

    def test_scheduled_event_of_other_pod_is_ignored(self):
        obs = fixture("success_basic")
        obs["events"]["items"][0]["involvedObject"]["uid"] = "synthetic-pod-zzz-uid"
        self.assertEqual(analyze(obs)["aux_replacement_creation_to_scheduled_event"]["error_code"], "scheduled_event_missing")

    def test_missing_wall_clock_and_missing_timestamps_degrade_gracefully(self):
        obs = fixture("success_basic")
        obs["delete_request_wall"] = None
        row = analyze(obs)["aux_wallclock_request_to_replacement_creation_timestamp"]
        self.assertEqual((row["status"], row["value"], row["error_code"]), ("not_measured", None, "no_wall_clock_request_time"))
        obs = fixture("success_basic")
        for poll in obs["polls"]:
            for item in poll["pods"]["items"]:
                item["metadata"].pop("creationTimestamp", None)
        r = analyze(obs)
        self.assertEqual(r["aux_wallclock_request_to_replacement_creation_timestamp"]["error_code"], "missing_timestamp")
        self.assertEqual(r["deletion_request_to_replacement_pod_observed"]["value"], 2.0)    # primary unaffected

    def test_negative_auxiliary_interval_becomes_null_with_warning(self):
        obs = fixture("success_basic")
        obs["delete_request_wall"] = "2026-01-01T12:00:09Z"      # skewed: after the creation timestamp
        row = analyze(obs)["aux_wallclock_request_to_replacement_creation_timestamp"]
        self.assertIsNone(row["value"])
        self.assertEqual(row["status"], "not_measured")
        self.assertIn("k8s_creation_negative_interval", row["warnings"])

    def test_bad_timestamp_in_pod_data_fails_only_that_auxiliary_row(self):
        obs = fixture("success_basic")
        for poll in obs["polls"]:
            for item in poll["pods"]["items"]:
                if item["metadata"]["uid"] == "synthetic-pod-b-uid":
                    item["metadata"]["creationTimestamp"] = "2026-01-01T12:00:01"      # naive
        r = analyze(obs)
        self.assertEqual(r["aux_wallclock_request_to_replacement_creation_timestamp"]["error_code"], "bad_timestamp")
        self.assertEqual(r["replacement_pod_observed_to_ready_observed"]["value"], 3.0)

    def test_ready_at_first_observation_is_zero_with_warning(self):
        obs = fixture("success_basic")
        ready = obs["polls"][3]["pods"]["items"][0]
        obs["polls"] = [{"mono_s": 1002.0, "pods": {"items": [ready]}}]
        row = analyze(obs)["replacement_pod_observed_to_ready_observed"]
        self.assertEqual(row["value"], 0.0)
        self.assertIn("ready_at_first_observation", row["warnings"])

    def test_replacement_disappearing_before_ready_fails(self):
        obs = fixture("success_basic")
        obs["polls"] = obs["polls"][:3] + [{"mono_s": 1004.0, "pods": {"items": []}}]
        row = analyze(obs)["replacement_pod_observed_to_ready_observed"]
        self.assertEqual((row["status"], row["error_code"]), ("failed", "replacement_pod_disappeared"))

    def test_no_ready_condition_is_distinct_from_not_ready(self):
        obs = fixture("success_basic")
        for poll in obs["polls"]:
            for item in poll["pods"]["items"]:
                if item["metadata"]["uid"] == "synthetic-pod-b-uid":
                    item["status"].pop("conditions", None)
        row = analyze(obs)["replacement_pod_observed_to_ready_observed"]
        self.assertEqual((row["status"], row["error_code"]), ("failed", "no_ready_condition"))

    def test_target_missing_from_baseline(self):
        obs = fixture("success_basic")
        obs["target_uid"] = "synthetic-pod-unknown-uid"
        r = analyze(obs)
        self.assertEqual(r["deletion_request_to_replacement_pod_observed"]["error_code"], "target_pod_not_found")
        self.assertIsNone(r["deletion_request_to_replacement_pod_observed"]["value"])


class ObservationValidationTests(unittest.TestCase):
    def test_corrupt_observations_raise_typed_error(self):
        def mut(fn):
            obs = fixture("success_basic")
            fn(obs)
            return obs

        cases = {
            "not an object": [],
            "unknown key": mut(lambda o: o.update(surprise=1)),
            "wrong schema": mut(lambda o: o.update(schema="x")),
            "sample id newline": mut(lambda o: o.update(sample_id="a\n")),
            "sample id list": mut(lambda o: o.update(sample_id=["a"])),
            "synthetic str": mut(lambda o: o.update(synthetic="yes")),
            "namespace list": mut(lambda o: o.update(namespace=["a"])),
            "selector list": mut(lambda o: o.update(selector=["app=x"])),
            "selector bad": mut(lambda o: o.update(selector="ros-")),
            "target uid dict": mut(lambda o: o.update(target_uid={})),
            "baseline list": mut(lambda o: o.update(baseline_pods=[])),
            "mono missing": mut(lambda o: o.pop("delete_requested_mono_s")),
            "mono bool": mut(lambda o: o.update(delete_requested_mono_s=True)),
            "mono str": mut(lambda o: o.update(delete_requested_mono_s="1000")),
            "mono negative": mut(lambda o: o.update(delete_requested_mono_s=-1)),
            "mono huge": mut(lambda o: o.update(delete_requested_mono_s=1e300)),
            "wall naive": mut(lambda o: o.update(delete_request_wall="2026-01-01T12:00:00")),
            "wall int": mut(lambda o: o.update(delete_request_wall=5)),
            "polls dict": mut(lambda o: o.update(polls={})),
            "poll list": mut(lambda o: o.update(polls=[[]])),
            "poll extra key": mut(lambda o: o["polls"][0].update(extra=1)),
            "poll missing pods": mut(lambda o: o["polls"][0].pop("pods")),
            "poll time bool": mut(lambda o: o["polls"][0].update(mono_s=False)),
            "poll before delete": mut(lambda o: o["polls"][0].update(mono_s=999.0)),
            "poll backwards": mut(lambda o: o["polls"][2].update(mono_s=1001.0)),
            "events dict": mut(lambda o: o.update(events={})),
            "exit bool": mut(lambda o: o.update(exit_status=True)),
            "exit big": mut(lambda o: o.update(exit_status=256)),
            "exit str": mut(lambda o: o.update(exit_status="0")),
            "exit float": mut(lambda o: o.update(exit_status=0.0)),
            "run_error list": mut(lambda o: o.update(run_error=[])),
            "run_error code bad": mut(lambda o: o.update(run_error={"code": "Bad Code\n"})),
            "context bad": mut(lambda o: o.update(kubectl_context="a b")),
        }
        for label, obs in cases.items():
            with self.subTest(case=label), self.assertRaises(rm.MalformedInputError):
                rm.analyze_observation(obs, evidence_ref="x.json#sha256=0")

    def test_evidence_ref_must_match_pattern(self):
        for bad in ("", "=cmd", "a\nb", None, ["a"], "a b"):
            with self.subTest(bad=repr(bad)), self.assertRaises(rm.MalformedInputError):
                rm.analyze_observation(fixture("success_basic"), evidence_ref=bad)

    def test_duplicate_pods_in_a_poll_are_rejected(self):
        obs = fixture("success_basic")
        item = obs["polls"][1]["pods"]["items"][1]
        obs["polls"][1]["pods"]["items"].append(copy.deepcopy(item))
        with self.assertRaises(rm.DuplicateRecordError):
            rm.analyze_observation(obs, evidence_ref="x.json#sha256=0")


class SerializationTests(unittest.TestCase):
    def rows(self, name="success_basic"):
        return rm.analyze_observation(fixture(name), evidence_ref=f"{name}.json#sha256=abc")

    def test_csv_schema_and_determinism(self):
        a, b = rm.rows_to_csv(self.rows()), rm.rows_to_csv(self.rows())
        self.assertEqual(a, b)
        parsed = list(csv.reader(io.StringIO(a)))
        self.assertEqual(tuple(parsed[0]), rm.COLUMNS)
        self.assertEqual(len(parsed) - 1, len(rm.MEASUREMENTS))
        for must in ("schema_version", "sample_id", "measurement", "definition", "unit", "exit_status",
                     "evidence_ref", "namespace", "selector", "target_uid", "replacement_uid"):
            self.assertIn(must, rm.COLUMNS)
        first = dict(zip(parsed[0], parsed[1]))
        self.assertEqual((first["schema_version"], first["unit"], first["synthetic"]), (rm.SCHEMA_VERSION, "s", "true"))
        self.assertEqual(first["value"], "2.000000")

    def test_json_schema_and_roundtrip(self):
        doc = json.loads(rm.rows_to_json(self.rows()))
        self.assertEqual(doc["schema_version"], rm.SCHEMA_VERSION)
        self.assertEqual(doc["columns"], list(rm.COLUMNS))
        self.assertEqual(len(doc["rows"]), len(rm.MEASUREMENTS))
        self.assertIsNone(doc["rows"][-1]["value"])           # ROS message row: null, not 0
        self.assertEqual(rm.rows_to_json(self.rows()), rm.rows_to_json(self.rows()))

    def test_failed_rows_have_empty_value_in_csv(self):
        parsed = list(csv.DictReader(io.StringIO(rm.rows_to_csv(self.rows("fail_no_replacement")))))
        self.assertTrue(all(r["value"] == "" for r in parsed))

    def test_committed_expected_outputs_are_byte_identical(self):
        for name in ("success_basic", "fail_no_replacement"):
            expected = (FIX / "expected" / f"{name}.csv").read_text(encoding="utf-8")
            data = (FIX / f"{name}.json").read_bytes()
            rows = rm.analyze_observation(rm.load_json_strict(data), evidence_ref=rm.evidence_ref_for(f"{name}.json", data))
            self.assertEqual(rm.rows_to_csv(rows), expected, name)

    def test_csv_formula_injection_is_neutralised(self):
        for text in ("=1+1", "+1", "-1", "@x", "\tx"):
            self.assertEqual(rm._csv_cell(text), "'" + text)
        self.assertEqual(rm._csv_cell("plain"), "plain")
        self.assertEqual(rm._csv_cell(None), "")
        self.assertEqual(rm._csv_cell(True), "true")
        self.assertEqual(rm._csv_cell(1.5), "1.500000")


class AnalyzeCliTests(unittest.TestCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        rc = rm.main(list(argv), stdout=out, stderr=err)
        return rc, out.getvalue(), err.getvalue()

    def test_exit_codes(self):
        self.assertEqual(self.run_cli("analyze", "--observation", str(FIX / "success_basic.json"))[0], 0)
        self.assertEqual(self.run_cli("analyze", "--observation", str(FIX / "fail_no_replacement.json"))[0], 1)
        self.assertEqual(self.run_cli("analyze", "--observation", str(FIX / "fail_never_ready.json"))[0], 1)
        self.assertEqual(self.run_cli("analyze", "--observation", str(FIX / "missing_events.json"))[0], 0)
        self.assertEqual(self.run_cli("analyze", "--observation", str(FIX / "does_not_exist.json"))[0], 2)
        self.assertEqual(self.run_cli("analyze")[0], 2)
        self.assertEqual(self.run_cli()[0], 2)

    def test_failure_is_reported_on_stderr(self):
        rc, out, err = self.run_cli("analyze", "--observation", str(FIX / "fail_no_replacement.json"))
        self.assertEqual(rc, 1)
        self.assertIn("no_replacement_pod", err)
        self.assertIn("deletion_request_to_replacement_pod_observed", out)

    def test_malformed_and_duplicate_key_files(self):
        with tempfile.TemporaryDirectory() as d:
            for name, text in {"dup.json": '{"a": 1, "a": 2}', "nan.json": '{"a": NaN}', "list.json": "[]",
                               "bad.json": "{"}.items():
                path = Path(d) / name
                path.write_text(text, encoding="utf-8")
                with self.subTest(name=name):
                    rc, out, err = self.run_cli("analyze", "--observation", str(path))
                    self.assertEqual(rc, 2)
                    self.assertEqual(out, "")
                    self.assertIn("error [", err)

    def test_output_file_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "out.csv"
            args = ("analyze", "--observation", str(FIX / "success_basic.json"), "--output", str(target))
            self.assertEqual(self.run_cli(*args)[0], 0)
            first = target.read_text(encoding="utf-8")
            self.assertEqual(self.run_cli(*args)[0], 2)
            self.assertEqual(target.read_text(encoding="utf-8"), first)

    def test_json_format_and_evidence_ref_override(self):
        rc, out, _ = self.run_cli("analyze", "--observation", str(FIX / "success_basic.json"),
                                  "--format", "json", "--evidence-ref", "my-ref")
        self.assertEqual(rc, 0)
        self.assertEqual({r["evidence_ref"] for r in json.loads(out)["rows"]}, {"my-ref"})
        self.assertEqual(self.run_cli("analyze", "--observation", str(FIX / "success_basic.json"),
                                      "--evidence-ref", "=bad")[0], 2)

    def test_default_evidence_ref_is_file_name_plus_sha256(self):
        _, out, _ = self.run_cli("analyze", "--observation", str(FIX / "success_basic.json"))
        ref = next(csv.DictReader(io.StringIO(out)))["evidence_ref"]
        self.assertRegex(ref, r"success_basic\.json#sha256=[0-9a-f]{64}")


# ----------------------------------------------------------------------------- RH.py
def kpod(name, uid, created, *, ready=None, transition=None, deleting=None):
    """kubectl-shaped pod that also carries data the tool must NOT store."""
    p = pod(name, uid, created, ready=ready, transition=transition, deleting=deleting)
    p["metadata"]["annotations"] = {"secret-note": "SECRET-ANNOTATION"}
    p["spec"] = {"containers": [{"name": "c", "env": [{"name": "TOKEN", "value": "SECRET-ENV-VALUE"}]}]}
    return p


A_UID, B_UID = "synthetic-pod-a-uid", "synthetic-pod-b-uid"
OLD = kpod("talker-sim-aaaaa", A_UID, "2025-12-31T23:00:00Z", ready="True", transition="2025-12-31T23:00:05Z")
OLD_DEL = kpod("talker-sim-aaaaa", A_UID, "2025-12-31T23:00:00Z", ready="True",
               transition="2025-12-31T23:00:05Z", deleting="2026-01-01T12:00:30Z")
NEW_PENDING = kpod("talker-sim-bbbbb", B_UID, "2026-01-01T12:00:01Z")
NEW_NOTREADY = kpod("talker-sim-bbbbb", B_UID, "2026-01-01T12:00:01Z", ready="False", transition="2026-01-01T12:00:02Z")
NEW_READY = kpod("talker-sim-bbbbb", B_UID, "2026-01-01T12:00:01Z", ready="True", transition="2026-01-01T12:00:04Z")


class FakeClock:
    def __init__(self):
        self.t = 1000.0
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


class FakeKubectl:
    """Replays recorded `get pods` answers (last one repeats) and records every call."""

    def __init__(self, pod_lists, *, context="synthetic-ctx", events=None, fail_on=None, allow_delete=True,
                 on_get_hook=None):
        self.pod_lists = list(pod_lists)
        self.context_name = context
        self.events = events
        self.fail_on = fail_on or {}
        self.calls = []
        self.allow_delete = allow_delete
        self.gets = 0
        self.on_get_hook = on_get_hook

    def run(self, args):
        args = list(args)
        self.calls.append(args)
        verb = args[0]
        if verb in self.fail_on:
            raise rm.KubectlError(f"fake failure for {verb}")
        if verb == "config":
            return self.context_name + "\n"
        if verb == "delete":
            if not self.allow_delete:
                raise rm.GuardError("refusing to run 'kubectl delete' without --yes")
            return f'pod "{args[2]}" deleted\n'
        if verb == "get" and args[1] == "pods":
            idx = min(self.gets, len(self.pod_lists) - 1)
            self.gets += 1
            if self.on_get_hook:
                self.on_get_hook(self.gets)
            return json.dumps(self.pod_lists[idx])
        if verb == "get" and args[1] == "events":
            return json.dumps(self.events if self.events is not None else {"items": []})
        raise AssertionError(f"unexpected kubectl call {args}")

    @property
    def verbs(self):
        return [c[0] for c in self.calls]


class RHBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.out_dir = Path(self._tmp.name) / "out"
        self.clock = FakeClock()
        self.kubectl = None

    def go(self, fake, *argv, base=True, sample="t1"):
        self.kubectl = fake
        args = list(argv)
        if base:
            args = ["--namespace", NS, "--selector", SEL, "--out-dir", str(self.out_dir)] + args
        if "--sample-id" not in args and "--yes" in args:
            args += ["--sample-id", sample]
        out, err = io.StringIO(), io.StringIO()
        factory_calls = []

        def factory(**kw):
            factory_calls.append(kw)
            fake.allow_delete = kw.get("allow_delete", False)
            return fake

        rc = RH.run(args, kubectl_factory=factory, clock=self.clock.now, sleep=self.clock.sleep,
                    wall=lambda: datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc), out=out, err=err)
        self.factory_calls = factory_calls
        return rc, out.getvalue(), err.getvalue()

    def csv_rows(self, sample="t1"):
        with open(self.out_dir / f"{sample}_recovery.csv", encoding="utf-8", newline="") as fh:
            return rows_by_name(csv.DictReader(fh))


class DryRunTests(RHBase):
    def test_default_is_dry_run_and_deletes_nothing(self):
        fake = FakeKubectl([plist(OLD)])
        rc, out, _ = self.go(fake)
        self.assertEqual(rc, 0)
        self.assertNotIn("delete", fake.verbs)
        self.assertEqual(fake.verbs, ["config", "get"])                      # read-only calls only
        self.assertIn("DRY RUN", out)
        self.assertIn("talker-sim-aaaaa", out)
        self.assertIn("synthetic-ctx", out)                                   # current context is printed
        self.assertFalse(self.out_dir.exists())                               # no files written
        self.assertEqual(self.factory_calls, [{"context": None, "allow_delete": False}])

    def test_dry_run_with_explicit_context_does_not_query_current_context(self):
        fake = FakeKubectl([plist(OLD)])
        rc, out, _ = self.go(fake, "--context", "lab-kind")
        self.assertEqual(rc, 0)
        self.assertEqual(fake.verbs, ["get"])
        self.assertIn("lab-kind", out)

    def test_dry_run_with_ambiguous_selector_refuses(self):
        fake = FakeKubectl([plist(OLD, kpod("talker-sim-ccccc", "synthetic-pod-c-uid", "2025-12-31T23:00:00Z"))])
        rc, _, err = self.go(fake)
        self.assertEqual(rc, 1)
        self.assertIn("ambiguous_match", err)
        self.assertNotIn("delete", fake.verbs)

    def test_dry_run_with_target_pod_among_several_names_it(self):
        fake = FakeKubectl([plist(OLD, kpod("talker-sim-ccccc", "synthetic-pod-c-uid", "2025-12-31T23:00:00Z"))])
        rc, out, _ = self.go(fake, "--target-pod", "talker-sim-ccccc")
        self.assertEqual(rc, 0)
        self.assertIn("would delete pod talker-sim-ccccc", out)

    def test_dry_run_no_matching_pod_is_an_error(self):
        rc, _, err = self.go(FakeKubectl([plist()]))
        self.assertEqual(rc, 1)
        self.assertIn("target_pod_not_found", err)

    def test_dry_run_unknown_target_pod_and_terminating_pod(self):
        rc, _, err = self.go(FakeKubectl([plist(OLD)]), "--target-pod", "nope")
        self.assertEqual(rc, 1)
        rc, _, err = self.go(FakeKubectl([plist(OLD_DEL)]))
        self.assertEqual(rc, 1)
        self.assertIn("target_pod_not_found", err)

    def test_through_the_real_wrapper_with_an_injected_runner_no_delete_is_issued(self):
        seen = []

        def runner(cmd, **kw):
            seen.append(cmd)
            text = "synthetic-ctx\n" if "config" in cmd else json.dumps(plist(OLD))
            return subprocess.CompletedProcess(cmd, 0, stdout=text, stderr="")

        out = io.StringIO()
        rc = RH.run(["--namespace", NS, "--selector", SEL, "--out-dir", str(self.out_dir)],
                    kubectl_factory=lambda **kw: RH.Kubectl(runner=runner, **kw), out=out, err=io.StringIO())
        self.assertEqual(rc, 0)
        self.assertEqual(len(seen), 2)
        self.assertTrue(all(c[0] == "kubectl" and "delete" not in c for c in seen))
        self.assertIn(["kubectl", "get", "pods", "-n", NS, "-l", SEL, "-o", "json"], seen)


class GuardTests(RHBase):
    def assert_refused_before_any_kubectl(self, *argv, base=True, code=2):
        fake = FakeKubectl([plist(OLD)])
        rc, _, err = self.go(fake, *argv, base=base)
        self.assertEqual(rc, code, err)
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.factory_calls, [])
        self.assertFalse(self.out_dir.exists())

    def test_yes_requires_context_or_confirmation(self):
        self.assert_refused_before_any_kubectl("--yes")

    def test_yes_context_is_correct_alone_without_yes_changes_nothing(self):
        fake = FakeKubectl([plist(OLD)])
        rc, out, _ = self.go(fake, "--yes-context-is-correct")
        self.assertEqual(rc, 0)
        self.assertIn("DRY RUN", out)
        self.assertNotIn("delete", fake.verbs)

    def test_namespace_and_selector_are_required(self):
        for argv in ([], ["--namespace", NS], ["--selector", SEL], ["--yes", "--context", "c"]):
            with self.subTest(argv=argv):
                fake = FakeKubectl([plist(OLD)])
                rc, _, _ = self.go(fake, *argv, base=False)
                self.assertEqual(rc, 2)
                self.assertEqual(fake.calls, [])

    def test_invalid_values_are_refused(self):
        bad_args = [
            ["--namespace", "Bad_NS"], ["--namespace", "ros\n"], ["--namespace", ""],
            ["--selector", "ros-"], ["--selector", "app!=x"], ["--selector", "app in (x)"], ["--selector", ""],
            ["--target-pod", "UPPER"], ["--target-pod", "a\n"], ["--owner-uid", "u\n"],
            ["--context", "-x"], ["--context", "a b"],
            ["--timeout-replacement", "nan"], ["--timeout-replacement", "inf"], ["--timeout-replacement", "0"],
            ["--timeout-replacement", "-5"], ["--timeout-replacement", "1e9"], ["--timeout-replacement", "abc"],
            ["--timeout-ready", "0.5"], ["--poll-interval", "0"], ["--poll-interval", "0.01"], ["--poll-interval", "600"],
            ["--sample-id", "a b"], ["--sample-id", "../x"], ["--sample-id", ""],
        ]
        for extra in bad_args:
            with self.subTest(extra=extra):
                args = ["--namespace", NS, "--selector", SEL, "--out-dir", str(self.out_dir)]
                # later duplicates win in argparse, so appending overrides the valid default
                fake = FakeKubectl([plist(OLD)])
                rc, _, _ = self.go(fake, *args, *extra, base=False)
                self.assertEqual(rc, 2)
                self.assertEqual(fake.calls, [])

    def test_all_namespaces_or_prefix_style_flags_do_not_exist(self):
        self.assert_refused_before_any_kubectl("--all-namespaces")
        self.assert_refused_before_any_kubectl("--prefix", "ros-")

    def test_wrapper_refuses_delete_unless_allowed_and_never_starts_a_process(self):
        calls = []
        k = RH.Kubectl(runner=lambda *a, **kw: calls.append(a))
        with self.assertRaises(rm.GuardError):
            k.run(["delete", "pod", "x", "-n", NS])
        self.assertEqual(calls, [])

    def test_wrapper_verb_and_argument_allowlist(self):
        calls = []
        k = RH.Kubectl(allow_delete=True, runner=lambda *a, **kw: calls.append(a))
        for argv in (["apply", "-f", "x"], ["exec", "p"], ["config", "set-context", "x"], ["config", "use-context", "x"],
                     [], [""], ["get", None], ["get", 5], ["get", "pods\x00"]):
            with self.subTest(argv=argv), self.assertRaises(rm.GuardError):
                k.run(argv)
        self.assertEqual(calls, [])

    def test_wrapper_checks_return_code_and_failures(self):
        def runner_rc(rc, out="{}", err="boom"):
            return lambda cmd, **kw: subprocess.CompletedProcess(cmd, rc, stdout=out, stderr=err)

        for rc in (1, 2, 127, -9):
            with self.subTest(rc=rc), self.assertRaises(rm.KubectlError) as cm:
                RH.Kubectl(runner=runner_rc(rc)).run(["get", "pods"])
        self.assertIn("boom", str(cm.exception))
        self.assertEqual(RH.Kubectl(runner=runner_rc(0)).run(["get", "pods"]), "{}")

        class Odd:
            returncode, stdout, stderr = True, "x", ""
        with self.assertRaises(rm.KubectlError):
            RH.Kubectl(runner=lambda cmd, **kw: Odd()).run(["get", "pods"])

        class NoOut:
            returncode, stdout, stderr = 0, None, ""
        with self.assertRaises(rm.KubectlError):
            RH.Kubectl(runner=lambda cmd, **kw: NoOut()).run(["get", "pods"])

        def timeout(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 1)

        def missing(cmd, **kw):
            raise FileNotFoundError(2, "No such file or directory")

        for r in (timeout, missing):
            with self.assertRaises(rm.KubectlError):
                RH.Kubectl(runner=r).run(["get", "pods"])

    def test_wrapper_passes_context_timeout_and_closes_stdin(self):
        seen = {}

        def runner(cmd, **kw):
            seen["cmd"], seen["kw"] = cmd, kw
            return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

        RH.Kubectl(context="lab-kind", runner=runner).run(["get", "pods", "-n", NS])
        self.assertEqual(seen["cmd"][:3], ["kubectl", "--context", "lab-kind"])
        self.assertFalse(seen["kw"].get("shell", False))
        self.assertEqual(seen["kw"]["stdin"], subprocess.DEVNULL)
        self.assertGreater(seen["kw"]["timeout"], 0)
        RH.Kubectl(context="lab-kind", runner=runner).run(["config", "current-context"])
        self.assertEqual(seen["cmd"], ["kubectl", "config", "current-context"])


class LiveMeasurementTests(RHBase):
    YES = ["--yes", "--context", "synthetic-ctx", "--poll-interval", "1"]

    def test_successful_measurement_uses_monotonic_polls_and_writes_evidence(self):
        fake = FakeKubectl([plist(OLD), plist(OLD_DEL), plist(OLD_DEL, NEW_PENDING), plist(OLD_DEL, NEW_NOTREADY),
                            plist(NEW_READY)])
        rc, out, err = self.go(fake, *self.YES)
        self.assertEqual(rc, 0, err)
        deletes = [c for c in fake.calls if c[0] == "delete"]
        self.assertEqual(deletes, [["delete", "pod", "talker-sim-aaaaa", "-n", NS, "--wait=false"]])
        self.assertEqual(self.factory_calls, [{"context": "synthetic-ctx", "allow_delete": True}])
        rows = self.csv_rows()
        # polls at t=1000 (old only), 1001 (replacement appears), 1002 (not ready), 1003 (Ready)
        self.assertEqual(float(rows["deletion_request_to_replacement_pod_observed"]["value"]), 1.0)
        self.assertEqual(float(rows["replacement_pod_observed_to_ready_observed"]["value"]), 2.0)
        self.assertEqual(rows["deletion_request_to_replacement_pod_observed"]["exit_status"], "0")
        self.assertEqual(rows["disruption_to_first_restored_ros_message"]["status"], "not_measured")
        self.assertEqual(float(rows["aux_wallclock_request_to_replacement_creation_timestamp"]["value"]), 1.0)
        self.assertIn("NOT MEASURED", out)
        self.assertTrue((self.out_dir / "t1_recovery.json").exists())
        # sanitized evidence: no env values or annotations
        evidence = (self.out_dir / "t1_observation.json").read_text(encoding="utf-8")
        self.assertNotIn("SECRET", evidence)
        self.assertEqual(json.loads(evidence)["synthetic"], False)
        # the evidence file re-analyses to byte-identical CSV through the offline path
        rc2, out2, _ = (lambda o, e: (rm.main(["analyze", "--observation", str(self.out_dir / "t1_observation.json")],
                                              stdout=o, stderr=e), o.getvalue(), e.getvalue()))(io.StringIO(), io.StringIO())
        self.assertEqual(rc2, 0)
        self.assertEqual(out2, (self.out_dir / "t1_recovery.csv").read_text(encoding="utf-8"))

    def test_with_events_adds_the_scheduled_event_and_uses_uid_field_selector(self):
        events = {"items": [
            {"involvedObject": {"kind": "Pod", "uid": B_UID}, "reason": "Scheduled", "eventTime": "2026-01-01T12:00:01.500000Z",
             "message": "SECRET-MESSAGE"},
            {"involvedObject": {"kind": "Pod", "uid": B_UID}, "reason": "Started", "eventTime": "2026-01-01T12:00:03Z"}]}
        fake = FakeKubectl([plist(OLD), plist(NEW_READY)], events=events)
        rc, _, err = self.go(fake, *self.YES, "--with-events")
        self.assertEqual(rc, 0, err)
        ev_call = [c for c in fake.calls if c[:2] == ["get", "events"]][0]
        self.assertIn(f"involvedObject.uid={B_UID}", ev_call)
        self.assertEqual(float(self.csv_rows()["aux_replacement_creation_to_scheduled_event"]["value"]), 0.5)
        self.assertNotIn("SECRET", (self.out_dir / "t1_observation.json").read_text(encoding="utf-8"))

    def test_timeout_without_replacement_is_bounded_and_fails(self):
        fake = FakeKubectl([plist(OLD), plist(OLD_DEL)])                     # replacement never appears
        rc, _, err = self.go(fake, "--yes", "--context", "c", "--timeout-replacement", "10", "--poll-interval", "2")
        self.assertEqual(rc, 4)
        self.assertIn("poll_timeout", err)
        self.assertLessEqual(fake.gets, 1 + 10 // 2 + 2)                      # baseline + bounded polls
        self.assertGreaterEqual(self.clock.t, 1010.0)
        self.assertLessEqual(self.clock.t, 1010.0 + 1e-9)                     # never sleeps past the deadline
        self.assertTrue(all(s <= 2 for s in self.clock.sleeps))
        rows = self.csv_rows()
        row = rows["deletion_request_to_replacement_pod_observed"]
        self.assertEqual((row["status"], row["value"], row["error_code"], row["exit_status"], row["run_error_code"]),
                         ("failed", "", "no_replacement_pod", "4", "poll_timeout"))

    def test_timeout_waiting_for_ready_keeps_the_creation_measurement(self):
        fake = FakeKubectl([plist(OLD), plist(OLD_DEL, NEW_PENDING), plist(NEW_NOTREADY)])
        rc, _, err = self.go(fake, "--yes", "--context", "c", "--timeout-ready", "5", "--poll-interval", "1")
        self.assertEqual(rc, 4)
        rows = self.csv_rows()
        self.assertEqual(float(rows["deletion_request_to_replacement_pod_observed"]["value"]), 0.0)
        self.assertEqual(rows["replacement_pod_observed_to_ready_observed"]["error_code"], "ready_not_observed")
        self.assertLessEqual(self.clock.t, 1000.0 + 5.0 + 1e-9)

    def test_clock_that_never_advances_still_terminates(self):
        fake = FakeKubectl([plist(OLD), plist(OLD_DEL)])
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(RH, "MAX_LIVE_POLLS", 50):
            rc = RH.run(["--namespace", NS, "--selector", SEL, "--out-dir", str(self.out_dir), "--yes", "--context", "c",
                         "--sample-id", "frozen"],
                        kubectl_factory=lambda **kw: fake, clock=self.clock.now, sleep=lambda s: None, out=out, err=err)
        self.assertEqual(rc, 4)                                              # time stood still; the poll cap ended the loop
        self.assertEqual(fake.gets, 1 + 50)

    def test_ambiguous_replacement_at_runtime(self):
        other = kpod("talker-sim-ddddd", "synthetic-pod-d-uid", "2026-01-01T12:00:01Z")
        fake = FakeKubectl([plist(OLD), plist(OLD_DEL, NEW_PENDING, other)])
        rc, _, err = self.go(fake, *self.YES)
        self.assertEqual(rc, 1)
        self.assertIn("ambiguous_match", err)
        self.assertEqual(self.csv_rows()["deletion_request_to_replacement_pod_observed"]["error_code"], "ambiguous_match")

    def test_replacement_that_disappears_fails(self):
        fake = FakeKubectl([plist(OLD), plist(OLD_DEL, NEW_PENDING), plist(OLD_DEL)])
        rc, _, err = self.go(fake, *self.YES)
        self.assertEqual(rc, 1)
        self.assertIn("replacement_pod_disappeared", err)

    def test_kubectl_failure_during_polling_is_nonzero_and_evidence_is_kept(self):
        class Flaky(FakeKubectl):
            def run(self, args):
                if args[0] == "get" and args[1] == "pods" and self.gets >= 2:
                    self.calls.append(list(args))
                    raise rm.KubectlError("fake: connection refused")
                return super().run(args)

        fake = Flaky([plist(OLD), plist(OLD_DEL)])
        rc, _, err = self.go(fake, *self.YES)
        self.assertEqual(rc, 3)
        self.assertEqual(len([c for c in fake.calls if c[0] == "delete"]), 1)
        self.assertTrue((self.out_dir / "t1_observation.json").exists())      # the deletion happened: evidence is written
        self.assertEqual(json.loads((self.out_dir / "t1_observation.json").read_text())["run_error"], {"code": "kubectl_failed"})
        self.assertEqual(self.csv_rows()["deletion_request_to_replacement_pod_observed"]["exit_status"], "3")

    def test_delete_failure_is_nonzero(self):
        fake = FakeKubectl([plist(OLD)], fail_on={"delete": True})
        rc, _, err = self.go(fake, *self.YES)
        self.assertEqual(rc, 3)
        self.assertEqual(fake.gets, 1)                                         # baseline only, no polling after a failed delete

    def test_baseline_listing_failure_is_nonzero_and_deletes_nothing(self):
        fake = FakeKubectl([plist(OLD)], fail_on={"get": True})
        rc, _, _ = self.go(fake, *self.YES)
        self.assertEqual(rc, 3)
        self.assertNotIn("delete", fake.verbs)

    def test_malformed_kubectl_output_is_typed_and_deletes_nothing(self):
        for label, text in {"list": "[]", "dup": '{"items": [], "items": []}', "nan": '{"items": NaN}', "empty": ""}.items():
            with self.subTest(label):
                fake = FakeKubectl([plist(OLD)])
                fake.pod_lists = [None]
                orig = fake.run

                def run(args, orig=orig, text=text, fake=fake):
                    if args[0] == "get":
                        fake.calls.append(list(args))
                        return text
                    return orig(args)
                fake.run = run
                rc, _, err = self.go(fake, *self.YES)
                self.assertEqual(rc, 2)
                self.assertNotIn("delete", fake.verbs)

    def test_keyboard_interrupt_after_delete_still_writes_evidence(self):
        fake = FakeKubectl([plist(OLD), plist(OLD_DEL)])

        def boom(s):
            raise KeyboardInterrupt

        self.clock.sleep = boom
        out, err = io.StringIO(), io.StringIO()
        rc = RH.run(["--namespace", NS, "--selector", SEL, "--out-dir", str(self.out_dir), "--yes", "--context", "c",
                     "--sample-id", "intr"], kubectl_factory=lambda **kw: fake, clock=self.clock.now, sleep=boom,
                    out=out, err=err)
        self.assertEqual(rc, 130)
        obs = json.loads((self.out_dir / "intr_observation.json").read_text())
        self.assertEqual((obs["exit_status"], obs["run_error"]), (130, {"code": "interrupted"}))

    def test_internal_bug_does_not_lose_evidence(self):
        fake = FakeKubectl([plist(OLD), plist(OLD_DEL)], on_get_hook=lambda n: (_ for _ in ()).throw(ZeroDivisionError()) if n == 2 else None)
        rc, _, err = self.go(fake, *self.YES)
        self.assertEqual(rc, 1)
        self.assertIn("internal_error", err)
        self.assertTrue((self.out_dir / "t1_observation.json").exists())

    def test_existing_output_is_refused_before_deleting(self):
        self.out_dir.mkdir(parents=True)
        (self.out_dir / "t1_recovery.csv").write_text("keep me", encoding="utf-8")
        fake = FakeKubectl([plist(OLD)])
        rc, _, err = self.go(fake, *self.YES)
        self.assertEqual(rc, 2)
        self.assertNotIn("delete", fake.verbs)
        self.assertEqual((self.out_dir / "t1_recovery.csv").read_text(encoding="utf-8"), "keep me")

    def test_yes_context_is_correct_prints_and_uses_current_context(self):
        fake = FakeKubectl([plist(OLD), plist(NEW_READY)])
        rc, out, _ = self.go(fake, "--yes", "--yes-context-is-correct", "--poll-interval", "1")
        self.assertEqual(rc, 0)
        self.assertIn("current", out)
        self.assertEqual(self.factory_calls, [{"context": None, "allow_delete": True}])
        self.assertEqual(json.loads((self.out_dir / "t1_observation.json").read_text())["kubectl_context"], "synthetic-ctx")

    def test_owner_uid_restricts_candidates(self):
        stranger = kpod("talker-sim-xxxxx", "synthetic-pod-x-uid", "2026-01-01T12:00:01Z")
        stranger["metadata"]["ownerReferences"] = [{"uid": "synthetic-replicaset-uid-9", "controller": True}]
        fake = FakeKubectl([plist(OLD), plist(OLD_DEL, stranger, NEW_READY)])
        rc, _, err = self.go(fake, *self.YES, "--owner-uid", RS)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.csv_rows()["deletion_request_to_replacement_pod_observed"]["replacement_uid"], B_UID)


class StructureTests(unittest.TestCase):
    def test_rehealing_is_a_thin_alias_of_rh(self):
        self.assertIs(rehealing.main, RH.main)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(rehealing.main([]), 2)                           # missing required args, nothing run

    def test_old_unsafe_patterns_are_gone(self):
        for name in ("RH.py", "rehealing.py", "recovery_metrics.py"):
            src = (ANALYSIS_DIR / name).read_text(encoding="utf-8")
            self.assertNotIn("shell=True", src, name)
            self.assertNotIn('"ros-"', src, name)
            self.assertNotIn("lastTimestamp\"]", src, name)
            self.assertNotIn("while True", src, name)
            self.assertNotIn("replace(tzinfo", src, name)

    def test_only_one_place_starts_a_process(self):
        tree = ast.parse((ANALYSIS_DIR / "RH.py").read_text(encoding="utf-8"))
        uses = [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                and n.value.id == "subprocess" and n.attr in {"run", "Popen", "call", "check_call", "check_output"}]
        self.assertEqual(len(uses), 1)                                         # the default runner of Kubectl
        rm_src = (ANALYSIS_DIR / "recovery_metrics.py").read_text(encoding="utf-8")
        self.assertNotIn("subprocess", rm_src)
        self.assertNotIn("import os", rm_src)

    def test_fixtures_are_labelled_synthetic_and_contain_no_secrets(self):
        for path in sorted(FIX.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIs(data["synthetic"], True, path.name)
            self.assertIn("SYNTHETIC", data["label"], path.name)
            self.assertTrue(data["sample_id"].startswith("SYNTHETIC-"), path.name)
            text = path.read_text(encoding="utf-8").lower()
            for needle in ("token", "password", "secret", "kubeconfig", "192.168.", "10.0."):
                self.assertNotIn(needle, text, f"{path.name}: {needle}")


if __name__ == "__main__":
    unittest.main()
