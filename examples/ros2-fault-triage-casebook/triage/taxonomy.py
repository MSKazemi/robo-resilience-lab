"""Closed vocabularies for the casebook. Every other module imports its enums from here.

Everything in this file is a teaching convention for SYNTHETIC cases. A label is "the root cause the
case author constructed, and wrote the evidence bundle to support"; for ``insufficient_evidence`` the
bundle deliberately under-determines the cause.
"""

SCHEMA = "rrl.triage-case/1"
MANIFEST_SCHEMA = "rrl.triage-manifest/1"
BANNER = "SYNTHETIC cases: a teaching baseline, not a benchmark"

# Hard limits so hostile or accidental input cannot make a run slow or huge.
MAX_FILE_BYTES = 65_536
MAX_CASES = 500
MAX_TEXT = 300
MAX_LOG_LINE = 200
MAX_COUNT = 1_000_000          # message counts
MAX_NS = 10**12                # latency values (1000 s) in nanoseconds
MAX_RESTARTS = 10_000
MAX_DEPTH = 10_000             # QoS depth (same bound as the sequence probe's qos.py)
MAX_DOMAIN_ID = 232            # ROS_DOMAIN_ID upper bound in ROS 2 docs (not re-verified here)

# Teaching constant used by the clock-skew rules; a convention, not a measurement.
LATENCY_SUSPICIOUS_NS = 1_000_000_000

# --- root-cause labels (closed taxonomy) -------------------------------------------------------
LABELS = (
    "no_fault",
    "pod_not_running",
    "discovery_server_unreachable",
    "network_path_blocked",
    "discovery_multicast_blocked",
    "domain_id_mismatch",
    "rmw_mismatch",
    "qos_incompatible_reliability",
    "qos_incompatible_durability",
    "clock_skew_misleading_latency",
    "network_partition",
    "publisher_restart",
    "insufficient_evidence",   # the abstain label: evidence does not determine one cause
)
ABSTAIN = "insufficient_evidence"

LABEL_DESCRIPTIONS = {
    "no_fault": "Messages arrived in full; no fault is visible in the bundle.",
    "pod_not_running": "A publisher or subscriber pod is Pending, crash-looping or otherwise not Ready.",
    "discovery_server_unreachable": "A Discovery Server is configured but cannot be reached.",
    "network_path_blocked": "Plain UDP unicast between the two pods does not get through.",
    "discovery_multicast_blocked": "Default multicast discovery is in use; unicast UDP works but multicast does not.",
    "domain_id_mismatch": "Publisher and subscriber use different ROS_DOMAIN_ID under default (multicast) discovery.",
    "rmw_mismatch": "Different RMW implementations and nothing else explains zero messages.",
    "qos_incompatible_reliability": "Publisher is best-effort but the subscriber requests reliable: no match, no data.",
    "qos_incompatible_durability": "Publisher is volatile but the subscriber requests transient-local: no match, no data.",
    "clock_skew_misleading_latency": "Delivery is complete; latency looks bad only because two clocks disagree.",
    "network_partition": "A mid-stream gap that coincides with partition markers; delivery resumed.",
    "publisher_restart": "The publisher started more than once under one run id and sequence numbers repeated.",
    "insufficient_evidence": "The bundle supports more than one cause, or none: abstain and gather more evidence.",
}

# Precedence used by the baseline for zero-message incidents (documented in the README):
# pod -> discovery server -> path -> multicast -> domain -> rmw -> QoS reliability -> QoS durability.

# --- case roles ----------------------------------------------------------------------------------
ROLES = ("positive", "hard_negative", "ambiguous")
ROLE_DESCRIPTIONS = {
    "positive": "The labelled fault is present and the bundle supports it (or the label is no_fault on a healthy run).",
    "hard_negative": "Looks like another fault on the surface (see looks_like) but the label is different.",
    "ambiguous": "The bundle does not determine one cause: the right answer is to abstain.",
}
SPLITS = ("dev", "holdout")

# --- safe next diagnostic steps (all read-only; none changes a cluster, a robot or a setting) ------
NEXT_STEPS = {
    "inspect_pod_status_and_events": "Read the pod phase, Ready condition, restart count and events for both pods (read-only).",
    "check_discovery_server_reachability": "Check from the subscriber's namespace that the configured Discovery Server address and UDP port answer (read-only probe).",
    "run_udp_unicast_check": "Send a plain UDP datagram pod to pod on a spare port and see whether it arrives (read-only; no firewall change).",
    "run_udp_multicast_vs_unicast_check": "Repeat the plain UDP check for unicast and multicast and compare; then evaluate an explicit-discovery option instead of loosening the firewall.",
    "compare_domain_and_discovery_settings": "Print ROS_DOMAIN_ID and the discovery settings from both pods and compare them.",
    "compare_rmw_settings": "Print RMW_IMPLEMENTATION from both pods; retest with the same value on both sides.",
    "compare_qos_profiles": "Print the QoS profile of each endpoint (for example with ros2 topic info --verbose) and compare reliability and durability.",
    "check_clock_offset_before_trusting_latency": "Measure the clock offset between the two nodes before reading any cross-node latency.",
    "check_fault_markers_and_network_events": "Line up the gap in sequence numbers against network event markers and pod events.",
    "check_publisher_start_events": "Count publisher start events per run id and check the publisher pod's restart history.",
    "collect_more_evidence": "Capture the missing items (QoS of both endpoints, discovery settings, a UDP check, pod status) before naming a cause.",
    "no_action": "Nothing to fix from this bundle; keep the evidence with the run.",
}
LABEL_NEXT_STEP = {
    "no_fault": "no_action",
    "pod_not_running": "inspect_pod_status_and_events",
    "discovery_server_unreachable": "check_discovery_server_reachability",
    "network_path_blocked": "run_udp_unicast_check",
    "discovery_multicast_blocked": "run_udp_multicast_vs_unicast_check",
    "domain_id_mismatch": "compare_domain_and_discovery_settings",
    "rmw_mismatch": "compare_rmw_settings",
    "qos_incompatible_reliability": "compare_qos_profiles",
    "qos_incompatible_durability": "compare_qos_profiles",
    "clock_skew_misleading_latency": "check_clock_offset_before_trusting_latency",
    "network_partition": "check_fault_markers_and_network_events",
    "publisher_restart": "check_publisher_start_events",
    "insufficient_evidence": "collect_more_evidence",
}

# --- evidence vocabularies --------------------------------------------------------------------------
POD_PHASES = ("Pending", "Running", "Succeeded", "Failed", "Unknown")
POD_REASONS = ("Unschedulable", "CrashLoopBackOff", "ImagePullBackOff")        # current waiting reason
POD_LAST_TERMINATIONS = ("OOMKilled", "Error")
RMWS = ("rmw_fastrtps_cpp", "rmw_cyclonedds_cpp", "rmw_zenoh_cpp")             # same list as e02's matrix
RELIABILITIES = ("reliable", "best-effort")                                    # spelling of seqprobe/qos.py
DURABILITIES = ("volatile", "transient-local")
DISCOVERY_MODES = ("multicast_default", "discovery_server")
SERVER_CHECKS = ("reachable", "unreachable", "not_checked")
UDP_RESULTS = ("received", "timed_out", "not_run")
COMPLETIONS = ("complete", "timeout", "incomplete", "unknown")                 # seqprobe verdict
TIMEOUT_REASONS = ("first_message", "idle", "overall")                         # seqprobe timeout event
CLOCK_DOMAINS = ("wall_cross_node", "same_node")
OFFSET_CHECKS = ("not_run", "offset_small", "offset_large")
FAULT_MARKERS = ("partition_begin", "partition_end", "restart_begin", "restart_end")  # e02 marker names

# --- citation registry: every case must cite at least one id from here -------------------------------
# kind "repo": a path (with optional #anchor) relative to the repository root; a test checks it exists.
# kind "external": an upstream document that this change did NOT re-fetch or re-verify.
CITATIONS = {
    "guide-checklist": ("repo", "docs/ROS2-KUBERNETES-DDS-GUIDE.md#troubleshooting-checklist"),
    "guide-discovery": ("repo", "docs/ROS2-KUBERNETES-DDS-GUIDE.md#why-ros-2-discovery-matters-on-kubernetes"),
    "guide-support-states": ("repo", "docs/ROS2-KUBERNETES-DDS-GUIDE.md#support-states-and-evidence"),
    "kind-findings": ("repo", "docs/KIND-DEMO.md#dds-discovery-findings-what-was-tested"),
    "probe-schema": ("repo", "examples/ros2-sequence-probe/README.md#log-schema-version-1"),
    "probe-clocks": ("repo", "examples/ros2-sequence-probe/README.md#clock-semantics"),
    "probe-verdict": ("repo", "examples/ros2-sequence-probe/README.md#verdict"),
    "e02-validation": ("repo", "experiments/e02-dds-resilience/README.md#validation-behaviour"),
    "e02-matrix": ("repo", "experiments/e02-dds-resilience/matrix/matrix.yaml"),
    "ros2-qos-upstream": ("external", "https://docs.ros.org/en/rolling/Concepts/Intermediate/About-Quality-of-Service-Settings.html"),
}
