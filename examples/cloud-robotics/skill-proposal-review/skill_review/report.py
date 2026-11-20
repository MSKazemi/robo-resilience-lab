"""Run the SYNTHETIC corpus through the reviewer and summarise per attack class.

Deterministic: no clocks, no randomness, no network. The advisory settings
below stand in for "what a model/advisor said"; the report proves the verdict
does not depend on them (issue #86: the advisor cannot elevate privileges).
"""

import hashlib
import json
from pathlib import Path

from .reviewer import ACCEPT, NEEDS_HUMAN, REJECT, VERDICTS, review

CORPUS_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "corpus.json"

BANNER = (
    "SYNTHETIC teaching corpus - not a security guarantee. Hand-written fixtures stand in for model output; "
    "no model, network, robot or ROS 2 was used. Results cover ONLY the enumerated cases."
)

# Synthetic advisor opinions. They are inputs to the audit record, never to the verdict.
ADVISORIES = (
    None,
    {"opinion": "approve", "confidence": 0.99},
    {"opinion": "reject", "confidence": 0.99},
)

NOT_RUN = (
    "any LLM or hosted advisor (no model output exists in this repo), latency, cost, reviewer effort, "
    "abstention rates, cross-worker/DDS/Kubernetes behaviour, the downstream action gate, any simulator executor"
)


def load_corpus(path=CORPUS_PATH):
    text = Path(path).read_bytes()
    corpus = json.loads(text.decode("utf-8"))
    corpus["sha256"] = hashlib.sha256(text).hexdigest()
    return corpus


def case_input(case):
    if "input_hex" in case:
        return bytes.fromhex(case["input_hex"])
    return case["input_text"]


def run_case(case, contexts, advisory=None, review_fn=review):
    """Return the ReviewResult for one corpus case (context is deep-copied by value)."""
    context = json.loads(json.dumps(contexts[case["context"]]))
    return review_fn(case_input(case), context, advisory)


def evaluate(corpus, review_fn=review):
    contexts = corpus["contexts"]
    rows = []
    advisory_changes = 0
    for case in corpus["cases"]:
        base = run_case(case, contexts, None, review_fn)
        for adv in ADVISORIES[1:]:
            other = run_case(case, contexts, adv, review_fn)
            if (other.verdict, other.primary_reason) != (base.verdict, base.primary_reason):
                advisory_changes += 1
        rows.append(
            {
                "id": case["id"],
                "attack_class": case["attack_class"],
                "kind": case["kind"],
                "expected_verdict": case["expected_verdict"],
                "expected_reason": case["expected_reason"],
                "actual_verdict": base.verdict,
                "actual_reason": base.primary_reason,
                "verdict_ok": base.verdict == case["expected_verdict"],
                "reason_ok": base.primary_reason == case["expected_reason"],
            }
        )

    classes = {}
    for r in rows:
        s = classes.setdefault(r["attack_class"], {"n": 0, "verdict_ok": 0, "reason_ok": 0, "mismatches": []})
        s["n"] += 1
        s["verdict_ok"] += r["verdict_ok"]
        s["reason_ok"] += r["reason_ok"]
        if not (r["verdict_ok"] and r["reason_ok"]):
            s["mismatches"].append(r["id"])

    confusion = {e: {a: 0 for a in VERDICTS} for e in VERDICTS}
    for r in rows:
        confusion[r["expected_verdict"]][r["actual_verdict"]] += 1

    unauthorized = [r for r in rows if r["kind"] == "adversarial"]
    benign = [r for r in rows if r["kind"] == "benign"]
    human = [r for r in rows if r["kind"] == "human_review"]
    summary = {
        "cases": len(rows),
        "corpus_sha256": corpus.get("sha256"),
        "unauthorized_total": len(unauthorized),
        "unauthorized_rejected": sum(r["actual_verdict"] == REJECT for r in unauthorized),
        "false_accepts": sum(r["expected_verdict"] != ACCEPT and r["actual_verdict"] == ACCEPT for r in rows),
        "benign_total": len(benign),
        "benign_accepted": sum(r["actual_verdict"] == ACCEPT for r in benign),
        "human_total": len(human),
        "human_routed": sum(r["actual_verdict"] == NEEDS_HUMAN for r in human),
        "verdict_mismatches": sum(not r["verdict_ok"] for r in rows),
        "reason_mismatches": sum(not r["reason_ok"] for r in rows),
        "internal_errors": sum(r["actual_reason"] == "INTERNAL_ERROR" for r in rows),
        "advisory_settings": len(ADVISORIES) - 1,
        "advisory_verdict_changes": advisory_changes,
    }
    return {"label": BANNER, "classes": classes, "confusion": confusion, "summary": summary, "rows": rows,
            "not_run": NOT_RUN}


def exit_ok(result):
    s = result["summary"]
    return (
        s["verdict_mismatches"] == 0
        and s["reason_mismatches"] == 0
        and s["unauthorized_rejected"] == s["unauthorized_total"]
        and s["false_accepts"] == 0
        and s["benign_accepted"] == s["benign_total"]
        and s["human_routed"] == s["human_total"]
        and s["internal_errors"] == 0
        and s["advisory_verdict_changes"] == 0
    )


def format_text(result):
    s = result["summary"]
    out = [result["label"], ""]
    out.append(f"corpus sha256: {s['corpus_sha256']}   cases: {s['cases']}")
    out.append("")
    out.append(f"{'attack class':<26}{'n':>4}{'verdict ok':>12}{'reason ok':>11}  mismatches")
    for name in sorted(result["classes"]):
        c = result["classes"][name]
        out.append(f"{name:<26}{c['n']:>4}{c['verdict_ok']:>12}{c['reason_ok']:>11}  {', '.join(c['mismatches']) or '-'}")
    out.append("")
    out.append("confusion matrix (rows = expected, columns = actual)")
    out.append(f"{'':<14}" + "".join(f"{v:>13}" for v in VERDICTS))
    for e in VERDICTS:
        out.append(f"{e:<14}" + "".join(f"{result['confusion'][e][a]:>13}" for a in VERDICTS))
    out.append("")
    out.append(f"unauthorized fixtures rejected : {s['unauthorized_rejected']}/{s['unauthorized_total']}")
    out.append(f"false accepts                  : {s['false_accepts']}")
    out.append(f"benign look-alikes accepted    : {s['benign_accepted']}/{s['benign_total']}  (over-blocking check)")
    out.append(f"human-review cases routed      : {s['human_routed']}/{s['human_total']}")
    out.append(f"internal errors                : {s['internal_errors']}")
    out.append(
        f"advisory influence             : {s['advisory_verdict_changes']} verdict changes across "
        f"{s['advisory_settings']} advisory settings x {s['cases']} cases"
    )
    out.append("")
    out.append("NOT RUN / not measured: " + result["not_run"])
    out.append("RESULT: " + ("PASS" if exit_ok(result) else "FAIL"))
    return "\n".join(out)
