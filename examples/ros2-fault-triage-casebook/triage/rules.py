"""Deterministic rule-based baseline triager. Readable rules, no learning, no randomness.

Each rule returns the list of evidence fields it used (``path=value`` strings) or ``None``. A rule
has an id, a label, a confidence tier and a citation id from ``taxonomy.CITATIONS``.

Behaviour that matters:

* Evidence is validated first. Invalid evidence never raises: the result abstains with rule ``R00``.
* The free-text ``log_excerpt`` and the case ``symptom`` are NEVER read, so a prompt-injection string
  in a log cannot change the answer (a test pins this).
* All rules are evaluated. If matching rules name more than one label, the triager abstains
  ("conflicting_rules"). If nothing matches it abstains ("no_rule_matched" / "no_observation").
* Confidence tiers are ``high``, ``medium`` and ``abstain``. A tier says how many independent pieces of
  evidence the rule needed; it is NOT a probability and NOT proof.
"""

from __future__ import annotations

from collections import namedtuple

from . import taxonomy as T
from . import validator as V

Rule = namedtuple("Rule", "rule_id label tier cite doc fn")
Triage = namedtuple("Triage", "label confidence rule_id rules_fired evidence_used reason safe_next_step")


def triage_to_dict(t):
    return {
        "label": t.label,
        "confidence": t.confidence,
        "rule_id": t.rule_id,
        "rules_fired": list(t.rules_fired),
        "evidence_used": list(t.evidence_used),
        "reason": t.reason,
        "safe_next_step": t.safe_next_step,
    }


def _g(ev, *path):
    cur = ev
    for k in path:
        if type(cur) is not dict or k not in cur:
            return None
        cur = cur[k]
    return cur


def _used(ev, *paths):
    out = []
    for p in paths:
        val = _g(ev, *p.split("."))
        out.append(f"{p}={val!r}" if type(val) is not str else f"{p}={val}")
    return out


class Ctx:
    """Facts derived once from validated evidence; rules read these, never raw free text."""

    def __init__(self, ev):
        self.ev = ev
        self.probe = _g(ev, "probe_summary")
        self.no_messages = self.probe is not None and self.probe["received_total"] == 0
        self.delivered = self.probe is not None and self.probe["ok"] is True
        pods = _g(ev, "pods") or {}
        self.broken_pods = sorted(
            role for role, p in pods.items()
            if p["ready"] is False and (p["phase"] != "Running" or p["reason"] is not None)
        )
        self.pods_broken = bool(self.broken_pods)
        self.mode = _g(ev, "discovery", "mode")
        self.server_check = _g(ev, "discovery", "server_check")
        self.uni = _g(ev, "udp_check", "unicast")
        self.multi = _g(ev, "udp_check", "multicast")
        pub, sub = _g(ev, "ros", "publisher"), _g(ev, "ros", "subscriber")
        self.have_ros = pub is not None and sub is not None
        self.pub, self.sub = pub, sub
        self.domain_differs = self.have_ros and pub["domain_id"] != sub["domain_id"]
        self.domain_same = self.have_ros and pub["domain_id"] == sub["domain_id"]
        self.rmw_differs = self.have_ros and pub["rmw"] != sub["rmw"]
        self.rel_incompatible = self.have_ros and pub["qos"]["reliability"] == "best-effort" and sub["qos"]["reliability"] == "reliable"
        self.dur_incompatible = self.have_ros and pub["qos"]["durability"] == "volatile" and sub["qos"]["durability"] == "transient-local"
        self.qos_compatible = self.have_ros and not self.rel_incompatible and not self.dur_incompatible
        # discovery health from what was actually checked; "unknown" when nothing was checked
        broken = (
            (self.mode == "discovery_server" and self.server_check == "unreachable")
            or self.uni == "timed_out"
            or (self.mode == "multicast_default" and self.uni == "received" and self.multi == "timed_out")
        )
        healthy = self.server_check == "reachable" or (self.mode == "multicast_default" and self.multi == "received")
        self.disc_state = "broken" if broken else ("ok" if healthy else "unknown")
        self.lat = _g(ev, "latency_summary")
        self.offset = _g(ev, "clock_offset_check") or "not_run"
        self.markers = _g(ev, "fault_markers") or []
        # a zero-message network incident that no earlier explanation (pods) already owns
        self.net_incident = self.no_messages and not self.pods_broken

    def latency_ok(self):
        return self.lat is None or (self.lat["negative_samples"] == 0 and self.lat["max_ns"] < T.LATENCY_SUSPICIOUS_NS)


# --- rules (evaluation order is irrelevant: all are evaluated and conflicts abstain) --------------
def r01_pod_not_running(c):
    if c.pods_broken and not c.delivered:
        return [f"pods.{role}.{f}={c.ev['pods'][role][f]}" for role in c.broken_pods for f in ("phase", "ready")] + \
               [f"pods.{role}.reason={c.ev['pods'][role]['reason']}" for role in c.broken_pods]


def r02_discovery_server_unreachable(c):
    if c.net_incident and c.mode == "discovery_server" and c.server_check == "unreachable":
        return _used(c.ev, "discovery.mode", "discovery.server_check", "probe_summary.received_total")


def r03_network_path_blocked(c):
    if c.net_incident and c.uni == "timed_out":
        return _used(c.ev, "udp_check.unicast", "probe_summary.received_total")


def r04_discovery_multicast_blocked(c):
    if c.net_incident and c.mode == "multicast_default" and c.uni == "received" and c.multi == "timed_out":
        return _used(c.ev, "discovery.mode", "udp_check.unicast", "udp_check.multicast", "probe_summary.received_total")


def r05_domain_id_mismatch(c):
    if c.net_incident and c.mode == "multicast_default" and c.domain_differs and c.disc_state != "broken":
        return _used(c.ev, "discovery.mode", "ros.publisher.domain_id", "ros.subscriber.domain_id", "probe_summary.received_total")


def r06_rmw_mismatch(c):
    if c.net_incident and c.rmw_differs and c.domain_same and c.qos_compatible and c.disc_state == "ok":
        return _used(c.ev, "ros.publisher.rmw", "ros.subscriber.rmw", "ros.publisher.domain_id", "ros.subscriber.domain_id",
                     "probe_summary.received_total")


def _qos_gate(c):
    """Common gate for the QoS rules: a zero-message incident, discovery not known broken, and no
    domain mismatch that would isolate the endpoints."""
    return c.net_incident and c.have_ros and c.disc_state != "broken" and not (c.domain_differs and c.mode != "discovery_server")


def r07_qos_reliability_high(c):
    if _qos_gate(c) and c.rel_incompatible and c.disc_state == "ok" and not c.rmw_differs:
        return _used(c.ev, "ros.publisher.qos.reliability", "ros.subscriber.qos.reliability", "probe_summary.received_total")


def r07b_qos_reliability_medium(c):
    if _qos_gate(c) and c.rel_incompatible and not (c.disc_state == "ok" and not c.rmw_differs):
        return _used(c.ev, "ros.publisher.qos.reliability", "ros.subscriber.qos.reliability", "probe_summary.received_total")


def r08_qos_durability_high(c):
    if _qos_gate(c) and c.dur_incompatible and not c.rel_incompatible and c.disc_state == "ok" and not c.rmw_differs:
        return _used(c.ev, "ros.publisher.qos.durability", "ros.subscriber.qos.durability", "probe_summary.received_total")


def r08b_qos_durability_medium(c):
    if _qos_gate(c) and c.dur_incompatible and not c.rel_incompatible and not (c.disc_state == "ok" and not c.rmw_differs):
        return _used(c.ev, "ros.publisher.qos.durability", "ros.subscriber.qos.durability", "probe_summary.received_total")


def r09_clock_skew(c):
    if not (c.delivered and c.lat is not None and c.lat["clock_domain"] == "wall_cross_node"):
        return None
    if c.lat["negative_samples"] > 0 and c.offset != "offset_small":
        return _used(c.ev, "latency_summary.clock_domain", "latency_summary.negative_samples", "clock_offset_check")
    if c.offset == "offset_large" and c.lat["max_ns"] >= T.LATENCY_SUSPICIOUS_NS:
        return _used(c.ev, "latency_summary.clock_domain", "latency_summary.max_ns", "clock_offset_check")


def r10_partition_high(c):
    if c.probe is not None and c.probe["missing"] > 0 and c.probe["received_total"] > 0 and not c.pods_broken \
            and "partition_begin" in c.markers and "partition_end" in c.markers:
        return _used(c.ev, "probe_summary.missing_ranges", "fault_markers")


def r10b_partition_medium(c):
    if c.probe is not None and c.probe["missing"] > 0 and c.probe["received_total"] > 0 and not c.pods_broken \
            and "partition_begin" in c.markers and "partition_end" not in c.markers:
        return _used(c.ev, "probe_summary.missing_ranges", "fault_markers")


def r11_publisher_restart_high(c):
    if c.probe is not None and c.probe["publisher_start_events"] >= 2 and c.probe["duplicates"] > 0:
        return _used(c.ev, "probe_summary.publisher_start_events", "probe_summary.duplicates")


def r11b_publisher_restart_medium(c):
    if c.probe is not None and c.probe["publisher_start_events"] >= 2 and c.probe["duplicates"] == 0:
        return _used(c.ev, "probe_summary.publisher_start_events", "probe_summary.duplicates")


def r12_no_fault(c):
    if c.delivered and c.latency_ok():
        return _used(c.ev, "probe_summary.ok", "probe_summary.completion")


RULES = (
    Rule("R01", "pod_not_running", "high", "guide-checklist", "A pod that is not Ready owns the incident; nothing downstream can be judged.", r01_pod_not_running),
    Rule("R02", "discovery_server_unreachable", "high", "kind-findings", "Zero messages and the configured Discovery Server does not answer.", r02_discovery_server_unreachable),
    Rule("R03", "network_path_blocked", "high", "kind-findings", "Zero messages and plain UDP unicast pod to pod timed out.", r03_network_path_blocked),
    Rule("R04", "discovery_multicast_blocked", "high", "kind-findings", "Zero messages, default discovery, unicast works, multicast times out.", r04_discovery_multicast_blocked),
    Rule("R05", "domain_id_mismatch", "high", "guide-checklist", "Zero messages, default discovery, different ROS_DOMAIN_ID. (A shared Discovery Server does not isolate domains, so this never fires in that mode.)", r05_domain_id_mismatch),
    Rule("R06", "rmw_mismatch", "medium", "guide-checklist", "Zero messages, different RMW, everything else compatible and discovery shown healthy.", r06_rmw_mismatch),
    Rule("R07", "qos_incompatible_reliability", "high", "ros2-qos-upstream", "Zero messages, publisher best-effort and subscriber reliable, discovery shown healthy.", r07_qos_reliability_high),
    Rule("R07b", "qos_incompatible_reliability", "medium", "ros2-qos-upstream", "Same QoS pair, but discovery health is unchecked or the RMWs differ.", r07b_qos_reliability_medium),
    Rule("R08", "qos_incompatible_durability", "high", "ros2-qos-upstream", "Zero messages, publisher volatile and subscriber transient-local, reliability compatible, discovery shown healthy.", r08_qos_durability_high),
    Rule("R08b", "qos_incompatible_durability", "medium", "ros2-qos-upstream", "Same durability pair, but discovery health is unchecked or the RMWs differ.", r08b_qos_durability_medium),
    Rule("R09", "clock_skew_misleading_latency", "high", "probe-clocks", "Complete delivery with cross-node wall-clock latency that is negative, or large with a measured large clock offset.", r09_clock_skew),
    Rule("R10", "network_partition", "high", "e02-validation", "A gap in sequence ids with partition_begin and partition_end markers, and delivery resumed.", r10_partition_high),
    Rule("R10b", "network_partition", "medium", "e02-validation", "A gap with only a partition_begin marker (the outage may still be running).", r10b_partition_medium),
    Rule("R11", "publisher_restart", "high", "probe-schema", "Two or more publisher start events under one run id and repeated sequence ids.", r11_publisher_restart_high),
    Rule("R11b", "publisher_restart", "medium", "probe-schema", "Two or more publisher start events but no repeated sequence id.", r11b_publisher_restart_medium),
    Rule("R12", "no_fault", "high", "probe-verdict", "Every message arrived exactly once and latency shows nothing suspicious.", r12_no_fault),
)

_TIER_RANK = {"high": 2, "medium": 1}


def _abstain(rule_id, reason, fired=(), used=()):
    return Triage(T.ABSTAIN, "abstain", rule_id, tuple(fired), tuple(used), reason, T.LABEL_NEXT_STEP[T.ABSTAIN])


def triage(evidence):
    """Return a ``Triage`` for one evidence bundle. Never raises; never reads free text."""
    diags = V.validate_evidence(evidence)
    if diags:
        codes = sorted({d.code for d in diags})
        return _abstain("R00", "invalid_evidence: " + ",".join(codes))
    ctx = Ctx(evidence)
    hits = []
    for rule in RULES:
        used = rule.fn(ctx)
        if used is not None:
            hits.append((rule, used))
    labels = sorted({r.label for r, _ in hits})
    fired = [r.rule_id for r, _ in hits]
    if len(labels) > 1:
        return _abstain("R90", "conflicting_rules: " + ",".join(fired), fired)
    if not hits:
        observed = ctx.probe is not None or ctx.pods_broken
        return _abstain("R91", "no_rule_matched" if observed else "no_observation: no probe summary or broken pod in the bundle")
    rule, used = max(hits, key=lambda h: (_TIER_RANK[h[0].tier], -hits.index(h)))
    return Triage(rule.label, rule.tier, rule.rule_id, tuple(fired), tuple(used), "", T.LABEL_NEXT_STEP[rule.label])
