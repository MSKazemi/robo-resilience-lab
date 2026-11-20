"""Subscriber adapter shutdown paths, exercised against a FAKE rclpy (no ROS installed here).

Regression for the 2026-11-20 Jazzy container run (RESULTS-2026-11-20-jazzy-container.md): a
SIGINT or SIGTERM to the subscriber let rclpy's own signal handler shut the context down, so
``spin_once`` raised ``ExternalShutdownException``, no ``stop`` event was written, and the final
``rclpy.shutdown()`` raised ``RCLError``. The fake below reproduces those two behaviours of the
real rclpy; it does not test DDS or rclpy itself.
"""
import contextlib
import io
import os
import signal
import sys
import tempfile
import types
import unittest

from seqprobe.events import read_events
from seqprobe.payload import build_payload

from tests.test_adapters_static import load


class _ExternalShutdownException(Exception):
    pass


def _fake_rclpy(script):
    """Return {module name: module} for a fake rclpy whose spin_once follows ``script``.

    ``script`` is a list of steps: ("msg", payload) delivers one message to the subscription
    callback, ("external_shutdown",) shuts the context down and raises like the real rclpy
    signal handler path, ("signal", signum) calls the Python handler installed for ``signum``.
    After the script is exhausted spin_once does nothing.
    """
    state = {"shutdown": False, "callbacks": []}
    steps = list(script)

    class _Now:
        nanoseconds = 1_700_000_000_000_000_000

    class _Clock:
        def now(self):
            return _Now()

    class _Node:
        def get_clock(self):
            return _Clock()

        def create_subscription(self, _cls, _topic, cb, _qos):
            state["callbacks"].append(cb)

        def destroy_node(self):
            pass

    class _Msg:
        def __init__(self, data):
            self.data = data

    def spin_once(_node, timeout_sec=None):
        if not steps:
            return
        step = steps.pop(0)
        if step[0] == "msg":
            for cb in state["callbacks"]:
                cb(_Msg(step[1]))
        elif step[0] == "external_shutdown":
            state["shutdown"] = True
            raise _ExternalShutdownException()
        elif step[0] == "signal":
            signal.getsignal(step[1])(step[1], None)

    def shutdown():
        if state["shutdown"]:
            raise RuntimeError("failed to shutdown: rcl_shutdown already called")
        state["shutdown"] = True

    def try_shutdown():
        state["shutdown"] = True

    rclpy = types.ModuleType("rclpy")
    rclpy.init = lambda **_kw: None
    rclpy.create_node = lambda _name: _Node()
    rclpy.spin_once = spin_once
    rclpy.shutdown = shutdown
    rclpy.try_shutdown = try_shutdown

    executors = types.ModuleType("rclpy.executors")
    executors.ExternalShutdownException = _ExternalShutdownException

    qos = types.ModuleType("rclpy.qos")
    qos.QoSProfile = lambda **kw: kw
    for name in ("QoSReliabilityPolicy", "QoSDurabilityPolicy", "QoSHistoryPolicy"):
        setattr(qos, name, types.SimpleNamespace(RELIABLE=1, BEST_EFFORT=2, VOLATILE=1,
                                                 TRANSIENT_LOCAL=2, KEEP_LAST=1))
    rclpy.executors, rclpy.qos = executors, qos

    std_msgs = types.ModuleType("std_msgs")
    msg = types.ModuleType("std_msgs.msg")
    msg.String = _Msg
    std_msgs.msg = msg
    return {"rclpy": rclpy, "rclpy.executors": executors, "rclpy.qos": qos,
            "std_msgs": std_msgs, "std_msgs.msg": msg}


class SubscriberShutdownTests(unittest.TestCase):
    def setUp(self):
        self._saved_handlers = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
        self._saved_modules = {k: sys.modules.get(k) for k in
                               ("rclpy", "rclpy.executors", "rclpy.qos", "std_msgs", "std_msgs.msg")}
        self.tmp = tempfile.TemporaryDirectory()
        self.log_path = os.path.join(self.tmp.name, "sub.jsonl")

    def tearDown(self):
        for s, h in self._saved_handlers.items():
            signal.signal(s, h)
        for k, v in self._saved_modules.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        self.tmp.cleanup()

    def _run(self, interrupt_step, delivered=3, expected=10):
        script = [("msg", build_payload("r1", seq, 1, "wall", 64, 0)) for seq in range(delivered)]
        script.append(interrupt_step)
        sys.modules.update(_fake_rclpy(script))
        sub = load("subscriber_node")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = sub.main(["--run-id", "r1", "--expected-count", str(expected),
                           "--log-file", self.log_path])
        with open(self.log_path, encoding="utf-8") as fh:
            events = read_events(fh)
        return rc, events, err.getvalue()

    def _assert_incomplete_stop(self, rc, events, delivered=3):
        self.assertEqual(rc, 1)
        self.assertEqual([e.event for e in events], ["start"] + ["recv"] * delivered + ["stop"])
        verdict = events[-1].detail["verdict"]
        self.assertEqual(verdict["completion"], "incomplete")
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["unique"], delivered)

    def test_external_shutdown_still_writes_stop_and_exits_1(self):
        rc, events, err = self._run(("external_shutdown",))
        self._assert_incomplete_stop(rc, events)
        self.assertNotIn("Traceback", err)

    def test_sigterm_handler_stops_cleanly(self):
        rc, events, err = self._run(("signal", signal.SIGTERM))
        self._assert_incomplete_stop(rc, events)
        self.assertIn("interrupted", err)

    def test_sigint_handler_stops_cleanly(self):
        rc, events, _ = self._run(("signal", signal.SIGINT))
        self._assert_incomplete_stop(rc, events)

    def test_complete_run_exits_0(self):
        script = [("msg", build_payload("r1", seq, 1, "wall", 64, 0)) for seq in range(4)]
        sys.modules.update(_fake_rclpy(script))
        sub = load("subscriber_node")
        with contextlib.redirect_stderr(io.StringIO()):
            rc = sub.main(["--run-id", "r1", "--expected-count", "4", "--log-file", self.log_path])
        with open(self.log_path, encoding="utf-8") as fh:
            events = read_events(fh)
        self.assertEqual(rc, 0)
        self.assertTrue(events[-1].detail["verdict"]["ok"])


if __name__ == "__main__":
    unittest.main()
