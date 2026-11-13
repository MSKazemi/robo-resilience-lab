"""Evaluate a triager against the casebook labels: per-class results, confusion table, abstention.

The cases are SYNTHETIC and were written together with the rules, so a high score here is expected and
says nothing about real clusters. The output is labelled accordingly.
"""

from __future__ import annotations

import json

from . import taxonomy as T
from .rules import triage as default_triage


SMALL_N = 3  # classes with fewer expected cases than this get no precision/recall (no statistical claim from n<3)
CIRCULAR_NOTE = "circular: cases and rules were authored together; this is a regression check, not accuracy"


def _ratio(num, den):
    return None if den == 0 else round(num / den, 3)


def evaluate(cases, triage_fn=default_triage):
    """Run ``triage_fn(evidence)`` on every case (sorted by case_id). Returns a plain dict.

    ``triage_fn`` must return an object with ``.label``, ``.confidence`` and ``.rule_id`` (a
    ``rules.Triage``). Cases are processed in a fixed order so the report is byte-identical on re-run.
    """
    rows = []
    for case in sorted(cases, key=lambda c: c["case_id"]):
        t = triage_fn(case["evidence"])
        rows.append((case, t))
    n = len(rows)
    labels = T.LABELS
    invalid = "<invalid>"  # a triager that returns something outside the closed taxonomy is counted, not hidden
    support = {l: 0 for l in labels}
    predicted = {l: 0 for l in labels + (invalid,)}
    tp = {l: 0 for l in labels}
    confusion = {}
    tiers = {}
    misclassified = []
    for case, t in rows:
        truth = case["expected"]["label"]
        pred = t.label if type(t.label) is str and t.label in labels else invalid
        support[truth] += 1
        predicted[pred] += 1
        confusion.setdefault(truth, {}).setdefault(pred, 0)
        confusion[truth][pred] += 1
        tier = tiers.setdefault(t.confidence, {"n": 0, "correct": 0})
        tier["n"] += 1
        if truth == pred:
            tp[truth] += 1
            tier["correct"] += 1
        else:
            misclassified.append({"case_id": case["case_id"], "family": case["incident_family_id"],
                                  "truth": truth, "predicted": pred, "confidence": t.confidence, "rule_id": t.rule_id})
    abstained = predicted[T.ABSTAIN]
    answered = n - abstained
    answered_correct = sum(tp[l] for l in labels if l != T.ABSTAIN)
    per_class = {}
    for l in labels + (invalid,):
        small = support.get(l, 0) < SMALL_N
        per_class[l] = {"support": support.get(l, 0), "predicted": predicted[l], "correct": tp.get(l, 0), "small_n": small,
                        "precision": None if small else _ratio(tp.get(l, 0), predicted[l]),
                        "recall": None if small else _ratio(tp.get(l, 0), support.get(l, 0))}
    return {
        "banner": T.BANNER,
        "circular_note": CIRCULAR_NOTE,
        "cases": n,
        "exact_match": {"correct": sum(tp.values()), "of": n, "ratio": _ratio(sum(tp.values()), n)},
        "coverage": {"answered": answered, "abstained": abstained, "coverage": _ratio(answered, n)},
        "accuracy_on_answered": {"correct": answered_correct, "of": answered, "ratio": _ratio(answered_correct, answered)},
        "abstention": {
            "correct_abstentions": tp[T.ABSTAIN],
            "abstained_on_answerable": sum(1 for c, t in rows if t.label == T.ABSTAIN and c["expected"]["label"] != T.ABSTAIN),
            "answered_when_should_abstain": sum(1 for c, t in rows if t.label != T.ABSTAIN and c["expected"]["label"] == T.ABSTAIN),
        },
        "per_class": per_class,
        "confusion": {truth: dict(sorted(row.items())) for truth, row in sorted(confusion.items())},
        "by_confidence_tier": dict(sorted(tiers.items())),
        "misclassified": misclassified,
    }


def _fmt(x):
    return "n/a" if x is None else f"{x:.3f}"


def render_text(result):
    out = [result["banner"], f"cases: {result['cases']}", ""]
    em, cov, aa, ab = result["exact_match"], result["coverage"], result["accuracy_on_answered"], result["abstention"]
    out.append(f"exact match (label == expected, abstain counts as a label): {em['correct']}/{em['of']}")
    out.append(f"coverage (answered, i.e. did not abstain): {cov['answered']}/{result['cases']} = {_fmt(cov['coverage'])}; abstained: {cov['abstained']}")
    out.append(f"accuracy on answered: {aa['correct']}/{aa['of']}")
    out.append(f"abstention: correct={ab['correct_abstentions']}  abstained_on_answerable={ab['abstained_on_answerable']}  "
               f"answered_when_should_abstain={ab['answered_when_should_abstain']}")
    out.append(result["circular_note"])
    out.append("")
    out.append(f"per class (precision/recall are n/a (n<{SMALL_N}) when a class has fewer than {SMALL_N} expected cases; counts are still shown):")
    out.append(f"  {'label':32} {'support':>7} {'predicted':>9} {'correct':>7} {'precision':>9} {'recall':>9}")
    for l in T.LABELS + ("<invalid>",):
        r = result["per_class"][l]
        if l == "<invalid>" and r["predicted"] == 0:
            continue
        if r["small_n"]:
            out.append(f"  {l:32} {r['support']:>7} {r['predicted']:>9} {r['correct']:>7} {'n/a (n<3)':>9} {'n/a (n<3)':>9}  n<3: not meaningful")
        else:
            out.append(f"  {l:32} {r['support']:>7} {r['predicted']:>9} {r['correct']:>7} {_fmt(r['precision']):>9} {_fmt(r['recall']):>9}")
    out.append("")
    out.append("confusion (rows = expected label, columns = predicted label; only non-zero cells):")
    if not result["confusion"]:
        out.append("  (no cases)")
    for truth, row in result["confusion"].items():
        out.append("  " + truth + " -> " + ", ".join(f"{p}: {k}" for p, k in row.items()))
    out.append("")
    out.append("by confidence tier (a tier is not a probability):")
    for tier, v in result["by_confidence_tier"].items():
        out.append(f"  {tier:8} n={v['n']} correct={v['correct']}")
    out.append("")
    mis = result["misclassified"]
    out.append(f"misclassified cases: {len(mis)}")
    for m in mis:
        out.append(f"  {m['case_id']} ({m['family']}): expected {m['truth']}, got {m['predicted']} [{m['confidence']}, {m['rule_id']}]")
    return "\n".join(out) + "\n"


def render_json(result):
    return json.dumps(result, indent=2, sort_keys=True) + "\n"


def coverage_table(cases):
    """Counts of cases per expected label and role, hard negatives that resemble each label, families."""
    table = {l: {r: 0 for r in T.ROLES} for l in T.LABELS}
    resembled = {l: 0 for l in T.LABELS}
    for c in cases:
        table[c["expected"]["label"]][c["case_role"]] += 1
        if c["looks_like"] is not None:
            resembled[c["looks_like"]] += 1
    families = sorted({c["incident_family_id"] for c in cases})
    return {"cases": len(cases), "families": families, "by_label_and_role": table, "resembled_by_hard_negatives": resembled}


def render_coverage(cov):
    out = [T.BANNER, f"cases: {cov['cases']}  families: {len(cov['families'])}", "",
           f"  {'expected label':32} " + " ".join(f"{r:>13}" for r in T.ROLES) + "   total  resembled_by_hard_negative"]
    for l in T.LABELS:
        row = cov["by_label_and_role"][l]
        out.append(f"  {l:32} " + " ".join(f"{row[r]:>13}" for r in T.ROLES)
                   + f"   {sum(row.values()):>5}  {cov['resembled_by_hard_negatives'][l]:>28}")
    return "\n".join(out) + "\n"
