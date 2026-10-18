"""C2 final: one network class per plan for the target network (PPO by default), with the evidence behind it.

Combines the three C2 sheets into one table. Every plan gets a class and an
`evidence` value that says which rule decided it; there is no numeric confidence
and no threshold, because every rule is a yes/no rule on explicit evidence
(LOGIC.md R2.26). A plan with no evidence would be `unlabelled` and is not guessed.

  network_class  <target> | non_<target> | unlabelled      (ppo | non_ppo | unlabelled by default)
  network_type   PPO, HMO, POS, EPO, EOA, SAL (from the matched word or code); a plan code counts as
                 the target when its `network_type` in reference/network_codes.csv is the target
                 (SAL is an HMO, EOA its own hybrid type)
  evidence       name_word           "PPO"/"HMO"/... stands alone in the name   (R2.1, R2.2)
                 name_embedded       "PPO" inside a token, e.g. CUSPPO           (R2.1)
                 name_word_epo_design  PPO + EPO in the name: PPO network, EPO
                                     benefit design, team decision A8            (R2.5)
                 plan_code_<status>  brand plan code from reference/network_codes.csv (R2.3)
                 none                no network signal                           (R2.4)

Outputs (output/c2/):
  c2_plan_network.csv   one row per plan x rate file (c2_plan_network_<target>.csv for other networks)
  c2_summary.json       (c2_summary_<target>.json)

Usage:
    python pipeline/c2_final_plans.py
    python pipeline/c2_final_plans.py --dir output/c2
    python pipeline/c2_final_plans.py --network HMO
"""
import argparse
import csv
import json
import os
from collections import Counter

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DIR = os.path.join(PROJECT_ROOT, "output", "c2")
DEFAULT_CODES = os.path.join(PROJECT_ROOT, "reference", "network_codes.csv")

KEEP = ["brand", "plan_name", "plan_id", "plan_id_type", "plan_market_type", "state", "rate_file_name"]
OUT_COLS = KEEP + ["network_class", "network_type", "evidence", "evidence_detail", "rule"]


def read(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=DEFAULT_DIR, help="folder with the C2 step sheets, also the output folder")
    ap.add_argument("--codes", default=DEFAULT_CODES, help="plan code table (default: reference/network_codes.csv)")
    ap.add_argument("--network", default="PPO", help="target network type (default: PPO)")
    args = ap.parse_args()
    d = lambda name: os.path.join(args.dir, name)
    target, t = args.network.upper(), args.network.lower()
    suffix = "" if target == "PPO" else f"_{t}"
    code_type = {}
    if os.path.exists(args.codes):
        for r in read(args.codes):
            code_type[r["code"].upper()] = (r.get("network_type") or r["code"]).upper()
    cls = lambda network_type: t if network_type == target else f"non_{t}"

    rows = []
    for r in read(d("c2_step1_ppo_name_plans.csv")):
        if r["network_note"]:
            evidence, rule = "name_word_epo_design", "R2.5 / A8"
        elif r["ppo_match_type"] == "embedded":
            evidence, rule = "name_embedded", "R2.1"
        else:
            evidence, rule = "name_word", "R2.1"
        rows.append({**{k: r[k] for k in KEEP}, "network_class": cls("PPO"), "network_type": "PPO",
                     "evidence": evidence, "evidence_detail": r["ppo_matched_token"] + (f"; {r['network_note']}" if r["network_note"] else ""),
                     "rule": rule})
    for r in read(d("c2_step2_other_network_name_plans.csv")):
        if r["classified_by"] == "plan_code":
            evidence, rule = f"plan_code_{r['code_status'] or 'unknown'}", "R2.3"
        else:
            evidence, rule = "name_word", "R2.2"
        types = r["network_words"].upper()
        types = code_type.get(types, types) if r["classified_by"] == "plan_code" else types
        rows.append({**{k: r[k] for k in KEEP}, "network_class": cls(types), "network_type": r["network_words"],
                     "evidence": evidence, "evidence_detail": r["matched_tokens"], "rule": rule})
    for r in read(d("c2_step3_unlabelled_name_plans.csv")):
        rows.append({**{k: r[k] for k in KEEP}, "network_class": "unlabelled", "network_type": "",
                     "evidence": "none", "evidence_detail": r.get("name_code_words", ""), "rule": "R2.4"})

    with open(d(f"c2_plan_network{suffix}.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OUT_COLS)
        w.writeheader()
        w.writerows(rows)

    by_class = Counter(r["network_class"] for r in rows)
    summary = {
        **({"network": target} if suffix else {}),
        "plans": len(rows),
        "by_class": dict(by_class),
        "by_class_and_evidence": {f"{k[0]}:{k[1]}": v for k, v in sorted(Counter((r["network_class"], r["evidence"]) for r in rows).items())},
        "by_type": dict(Counter(r["network_type"] for r in rows)),
        f"{t}_rate_files": sorted({r["rate_file_name"] for r in rows if r["network_class"] == t}),
        f"rate_files_shared_with_non_{t}": sorted({r["rate_file_name"] for r in rows if r["network_class"] == t}
                                                  & {r["rate_file_name"] for r in rows if r["network_class"] != t}),
        "threshold": "none: every class comes from a yes/no rule on explicit evidence (R2.26)",
        "outputs": [d(f"c2_plan_network{suffix}.csv")],
    }
    with open(d(f"c2_summary{suffix}.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
