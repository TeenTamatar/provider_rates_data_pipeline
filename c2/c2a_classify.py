"""C2a: classify each plan's network type from its plan name.

Reads c1_plans*.csv, writes one row per plan with network_type, is_ppo,
the matched token, evidence type and confidence. Plans with no usable token
stay 'unknown' and go to the document-reading stage (c2_network_evidence.py).

Rules (state each in LOGIC.md):
  R1  PPO anywhere in the name counts (CUSPPO is a PPO). A letter AFTER the token rules it out.
  R2  HMO / EPO / POS must stand alone as a token (no letter on either side), so POST is not POS.
  R3  Payer plan codes (e.g. EOA, SAL) map to the type their documents give; add them in PLAN_CODES.
  R4  PPO + EPO together = PPO (an EPO design on the PPO network, assumption A8).
  R5  PPO together with HMO or POS = conflict, left 'unknown' for review.
  R6  No token = unknown (never guessed).

Usage:
  python c2a_classify.py --plans output/c1/c1_plans_ca.csv --out-dir output/c2a
"""
import argparse
import csv
import json
import re
from collections import Counter
from pathlib import Path

# Payer-specific codes, each sourced from the payer's own documents.
# Value = (network_type, is_ppo). Keep this list per payer/state and cite the source.
PLAN_CODES = {
    "EOA": ("EOA", False),   # Health Net Elect Open Access, an HMO-based plan
    "SAL": ("SAL", False),   # Health Net Salud HMO y Mas
}

PPO_RX = re.compile(r"PPO(?![A-Z])")
STANDALONE = {t: re.compile(rf"(?<![A-Z]){t}(?![A-Z])") for t in ["HMO", "EPO", "POS", *PLAN_CODES]}


def classify(plan_name):
    name = (plan_name or "").upper()
    found = set()
    if PPO_RX.search(name):
        found.add("PPO")
    for token, rx in STANDALONE.items():
        if rx.search(name):
            found.add(token)

    if not found:
        return "unknown", None, "", "none", "unknown"

    if found == {"PPO"}:
        return "PPO", True, "PPO", "name_keyword", "high"
    if found == {"PPO", "EPO"}:
        return "PPO", True, "PPO+EPO", "name_keyword", "medium"
    if "PPO" in found:
        return "unknown", None, "+".join(sorted(found)), "name_conflict", "low"

    others = sorted(found)
    if len(others) == 1:
        t = others[0]
        net, is_ppo = PLAN_CODES.get(t, (t, False))
        return net, is_ppo, t, "name_keyword", "high"
    # several non-PPO tokens, e.g. HMO + POS: still non-PPO but type is mixed
    return "+".join(others), False, "+".join(others), "name_keyword", "medium"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--plans", required=True, help="c1_plans*.csv")
    ap.add_argument("--out-dir", default="output/c2a")
    args = ap.parse_args(argv)

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    with open(args.plans, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    result = []
    for r in rows:
        net, is_ppo, token, evidence, conf = classify(r.get("plan_name", ""))
        result.append({**r, "network_type": net,
                       "is_ppo": "" if is_ppo is None else str(is_ppo).lower(),
                       "matched_token": token, "evidence_type": evidence,
                       "confidence": conf})

    cols = list(result[0].keys()) if result else []
    with open(out / "c2a_plans.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(result)

    unresolved = [r for r in result if r["network_type"] == "unknown"]
    with open(out / "c2a_unresolved.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(unresolved)

    n = len(result)
    summary = {
        "plans": n,
        "by_network_type": dict(Counter(r["network_type"] for r in result).most_common()),
        "ppo": sum(r["is_ppo"] == "true" for r in result),
        "non_ppo": sum(r["is_ppo"] == "false" for r in result),
        "unknown": len(unresolved),
        "conflicts": sum(r["evidence_type"] == "name_conflict" for r in result),
        "by_state": {s: dict(Counter(r["network_type"] for r in result if r.get("state") == s))
                     for s in sorted({r.get("state", "") for r in result})},
        "reconciles": n == sum(r["is_ppo"] in ("true", "false") for r in result) + len(unresolved),
    }
    (out / "c2a_summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    return summary


if __name__ == "__main__":
    main()
