# Sequence probe in the Jazzy container: 2026-11-20 run (issue #139)

> **Measured once, on one host, in one container; not cross-worker.** The publisher and the
> subscriber ran as two processes inside a single `docker run --network none` container, so DDS
> traffic stayed on that container's loopback / shared memory. This result says nothing about
> cross-container, cross-node, kind or Kubernetes delivery, and the probe reports no latency,
> jitter or throughput. The fault cases are deliberate process stops, not network impairment.

## What was shown

| Acceptance item (#139) | Result in this run |
|---|---|
| Publisher and subscriber adapters run inside the Jazzy image from `ROS2/Dockerfile` on one host and write a schema-v1 log | Yes, for `std_msgs/String` and `std_msgs/ByteMultiArray`: 40 of 40 messages, `completion: complete`, `ok: true`, subscriber exit 0 |
| `python3 -m seqprobe analyze` on that log, exit code recorded | Exit **0** for both clean runs, on the host (Python 3.12.3) and inside the container |
| A deliberate fault yields a not-ok verdict | Stopping the subscriber (SIGTERM or SIGINT) or the publisher (SIGTERM) part-way: `ok: false`, subscriber exit 1, `analyze` exit **1** |
| Card C updated for the tested configuration only | **Not done here.** Card C lives in `docs/EXPERIMENT-STATUS.md`, outside this change's scope; left to the maintainer |

Before the fix described below, the first clean run (20 messages, `String`) also completed with
`ok: true`, but stopping the subscriber did not write a `stop` event. That bug is fixed in this
change, and the table above is from the run after the fix.

## Environment

| Item | Value |
|---|---|
| Date (UTC) | 2026-11-20, cases run 01:32:53Z to 01:33:38Z |
| Host | 12 CPUs, 30 GiB RAM, Ubuntu 24.04.5 LTS, kernel 7.0.0-43-generic, x86_64 |
| Container runtime | Docker 29.10.0 (client and server), overlayfs, cgroup v2 |
| Image | `rrl-jazzy:seqprobe-local`, id `sha256:c1a63f94c2fedaa7818567927f03575ee999fa19cd4342f65f3ca98a7f3289ed` (local build, not published) |
| Base image | `ros:jazzy-ros-base-noble@sha256:8eed0a4669bbf43ee331878a4c39bd8ebddea8e3312a58b4fae8edb14526b617` (from `ROS2/Dockerfile`, unchanged) |
| Inside the image | Ubuntu 24.04.5 LTS, Python 3.12.3, `ROS_DISTRO=jazzy`, RMW `rmw_fastrtps_cpp` (default; `RMW_IMPLEMENTATION` unset) |
| Package versions | `ros-jazzy-rclpy 7.1.12-1noble.20260912.162354`, `ros-jazzy-std-msgs 5.3.8-1noble.20260911.094424`, `ros-jazzy-rmw-fastrtps-cpp 8.4.4-1noble.20260911.100940`, `ros-jazzy-fastrtps 2.14.7-1noble.20260911.035053`, `ros-jazzy-demo-nodes-cpp 0.33.11-1noble.20261105.092654`, `ros-jazzy-ros2-pulse 0.7.0-1noble.20261105.090619`, `stress-ng 0.17.06-1build1`, `python3-venv 3.12.3-0ubuntu2.1` |

### Image build

The default apt pins in `ROS2/Dockerfile` (`ros-jazzy-demo-nodes-cpp=0.33.11-1noble.20260920.235420`,
`ros-jazzy-ros2-pulse=0.5.0-1noble.20260914.160740`) are no longer in the package index, so the
image was built **without editing the Dockerfile**, overriding those two pins with build arguments.
The other two pins (`stress-ng`, `python3-venv`) resolved as written. From the repository root:

```sh
docker build -f ROS2/Dockerfile \
  --build-arg ROS_DEMO_NODES_CPP_VERSION=0.33.11-1noble.20261105.092654 \
  --build-arg ROS2_PULSE_VERSION=0.7.0-1noble.20261105.090619 \
  -t rrl-jazzy:seqprobe-local ROS2
```

Exit 0. Updating the pins in the Dockerfile itself is a separate change.

## Commands

From the repository root, with the image above:

```sh
python3 -m unittest discover -s examples/ros2-sequence-probe/tests -t examples/ros2-sequence-probe
bash examples/ros2-sequence-probe/run-in-jazzy-container.sh rrl-jazzy:seqprobe-local OUT_DIR
```

[`run-in-jazzy-container.sh`](run-in-jazzy-container.sh) runs one container per case with
`--network none`, the probe mounted read-only at `/probe`, the host user's uid/gid, and a distinct
`ROS_DOMAIN_ID` per case (41-45). Every case uses 40 messages at 10 Hz, 64-byte payloads, QoS
reliable / volatile / keep-last depth 10, the `wall` clock, publisher `--start-delay-s 2` and
`--linger-s 2` (defaults), and subscriber `--idle-timeout-s 3`. Faults are sent 4 s after both
processes are launched (the publisher waits 2 s before its first message). It then runs `python3 -m seqprobe analyze` on
each subscriber log on the host. The run took about 45 s for all five cases.

## Observed output

The `run_id` of each case is its case name. The full JSON-lines logs (about 10 KB per log) are
not committed: this repository keeps `results/` directories out of the public tree, so they stay
with the person who ran them. The table and the excerpts below are copied from those logs and
from the script's printed exit codes.

| Case | Message type | Fault | Publisher log | Subscriber log | Verdict (`analyze`) | Exit codes pub / sub / analyze |
|---|---|---|---|---|---|---|
| `ok-string` | `String` | none | 40 `send`, stop `completed` | 40 `recv` | `complete`, ok, missing 0 | 0 / 0 / 0 |
| `ok-bytes` | `ByteMultiArray` | none | 40 `send`, stop `completed` | 40 `recv` | `complete`, ok, missing 0 | 0 / 0 / 0 |
| `sub-sigterm` | `String` | SIGTERM to subscriber | 40 `send`, stop `completed` | 17 `recv`, then `stop` | `incomplete`, not ok, missing 23 (`[[17, 39]]`) | 0 / 1 / 1 |
| `sub-sigint` | `String` | SIGINT to subscriber | 40 `send`, stop `completed` | 17 `recv`, then `stop` | `incomplete`, not ok, missing 23 (`[[17, 39]]`) | 0 / 1 / 1 |
| `pub-sigterm` | `String` | SIGTERM to publisher | 16 `send`, stop `stopped` | 16 `recv`, `timeout` (`idle`) | `timeout`, not ok, missing 24 (`[[16, 39]]`) | 0 / 1 / 1 |

No duplicate, reordered, unexpected or malformed message was counted in any case. In the fault
cases the publisher still exits 0: it records the stop reason in its log and leaves the verdict to
the subscriber. The exact counts in the fault cases depend on when the signal lands; they are not a
loss rate.

Excerpts, verbatim. `ok-bytes`, first `recv` and the subscriber `stop`:

```json
{"schema_version":1,"event":"recv","role":"subscriber","run_id":"ok-bytes","node":"seqprobe_sub","topic":"/seqprobe","seq":0,"payload_bytes":64,"ts_ns":1795138385634821129,"clock":"wall","sent_ts_ns":1795138385633419407,"sent_clock":"wall","detail":null}
{"schema_version":1,"event":"stop","role":"subscriber","run_id":"ok-bytes","node":"seqprobe_sub","topic":"/seqprobe","seq":null,"payload_bytes":null,"ts_ns":1795138389534464475,"clock":"wall","sent_ts_ns":null,"sent_clock":null,"detail":{"verdict":{"expected_count":40,"received_total":40,"unique":40,"duplicates":0,"reordered":0,"gap_events":0,"unexpected":0,"missing":0,"missing_ranges":[],"highest_seq":39,"completion":"complete","ok":true,"tail_loss_detectable":true},"malformed":0,"foreign_run":0}}
```

`sub-sigterm`, subscriber `stop` written after the signal:

```json
{"schema_version":1,"event":"stop","role":"subscriber","run_id":"sub-sigterm","node":"seqprobe_sub","topic":"/seqprobe","seq":null,"payload_bytes":null,"ts_ns":1795138396537878815,"clock":"wall","sent_ts_ns":null,"sent_clock":null,"detail":{"verdict":{"expected_count":40,"received_total":17,"unique":17,"duplicates":0,"reordered":0,"gap_events":0,"unexpected":0,"missing":23,"missing_ranges":[[17,39]],"highest_seq":16,"completion":"incomplete","ok":false,"tail_loss_detectable":true},"malformed":0,"foreign_run":0}}
```

`pub-sigterm`, publisher `stop`, then the subscriber's idle `timeout`:

```json
{"schema_version":1,"event":"stop","role":"publisher","run_id":"pub-sigterm","node":"seqprobe_pub","topic":"/seqprobe","seq":null,"payload_bytes":null,"ts_ns":1795138415024798783,"clock":"wall","sent_ts_ns":null,"sent_clock":null,"detail":{"sent":16,"reason":"stopped"}}
{"schema_version":1,"event":"timeout","role":"subscriber","run_id":"pub-sigterm","node":"seqprobe_sub","topic":"/seqprobe","seq":null,"payload_bytes":null,"ts_ns":1795138417949048438,"clock":"wall","sent_ts_ns":null,"sent_clock":null,"detail":{"reason":"idle","received_unique":16}}
```

The `ts_ns` values are wall-clock readings in one container; the difference between `ts_ns` and
`sent_ts_ns` is not reported as a latency (see the README, "Clock semantics").

Subscriber stderr in the fault cases (one line before the verdict JSON):

```text
interrupted by SIGTERM; the verdict covers only the messages seen so far
interrupted by SIGINT; the verdict covers only the messages seen so far
```

## Bug found and fixed in this change

On the first attempt (before the fix), SIGINT or SIGTERM to `subscriber_node.py` reached rclpy's
own signal handler, which shut the rclpy context down. `rclpy.spin_once` then raised
`rclpy.executors.ExternalShutdownException`, which the adapter did not catch, so **no `stop` event
was written**, and the final `rclpy.shutdown()` raised
`RCLError: failed to shutdown: rcl_shutdown already called on the given context`. The process
still exited 1 and `analyze` still reported `incomplete` / not ok from the `recv` lines, but the
log was missing its last record and stderr held two tracebacks.

The fix (`rclpy_adapters/subscriber_node.py`): install Python SIGINT/SIGTERM handlers after
`rclpy.init()` that set a stop flag (the same pattern the publisher already used, which behaved
correctly in this run), also catch `ExternalShutdownException`, and call `rclpy.try_shutdown()`.
`tests/test_subscriber_shutdown.py` reproduces both rclpy behaviours with a fake `rclpy` module
and checks that a `stop` event is written and the exit code is 1. Those tests do not need ROS and
do not exercise DDS.

## Not shown by this run

* Delivery between two containers, two hosts, kind nodes or Kubernetes workers.
* Behaviour under packet loss, delay, partition, CPU pressure or a different RMW.
* Best-effort reliability, transient-local durability, the `monotonic` and `ros` clocks, other
  depths, rates or payload sizes (all exist as flags; none was run here).
* Any repeatability claim: each case ran once.
