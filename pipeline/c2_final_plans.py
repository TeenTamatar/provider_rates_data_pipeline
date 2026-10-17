"""C2 final: one network class per plan, with the evidence behind it.

Combines the three C2 sheets into one table. Every plan gets a class and an
`evidence` value that says which rule decided it; there is no numeric confidence
and no threshold, because every rule is a yes/no rule on explicit evidence
(LOGIC.md R2.26). A plan with no evidence would be `unlabelled` and is not guessed.

  network_class  ppo | non_ppo | unlabelled
  network_type   PPO, HMO, POS, EPO, EOA, SAL (from the matched word or code)
  evidence       name_word           "PPO"/"HMO"/... stands alone in the name   (R2.1, R2.2)
                 name_embedded       "PPO" inside a token, e.g. CUSPPO           (R2.1)
                 name_word_epo_design  PPO + EPO in the name: PPO network, EPO
                                     benefit design, team decision A8            (R2.5)
                 plan_code_<status>  brand plan code from reference/network_codes.csv (R2.3)
                 none                no network signal                           (R2.4)

Outputs (output/c2/):
  c2_plan_network.csv   one row per plan x rate file
  c2_summary.json

Usage:
    python pipeline/c2_final_plans.py
    python pipeline/c2_final_plans.py --dir output/c2
"""
import argparse
import csv
import json
import os
from collections import Counter

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DIR = os.path.join(PROJECT_ROOT, "output", "c2")

KEEP = ["brand", "plan_name", "plan_id", "plan_id_type", "plan_market_type", "state", "rate_file_name"]
OUT_COLS = KEEP + ["network_class", "network_type", "evidence", "evidence_detail", "rule"]


def read(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=DEFAULT_DIR, help="folder with the C2 step sheets, also the output folder")
    args = ap.parse_args()
    d = lambda name: os.path.join(args.dir, name)

    rows = []
    for r in read(d("c2_step1_ppo_name_plans.csv")):
        if r["network_note"]:
            evidence, rule = "name_word_epo_design", "R2.5 / A8"
        elif r["ppo_match_type"] == "embedded":
            evidence, rule = "name_embedded", "R2.1"
        else:
            evidence, rule = "name_word", "R2.1"
        rows.append({**{k: r[k] for k in KEEP}, "network_class": "ppo", "network_type": "PPO",
                     "evidence": evidence, "evidence_detail": r["ppo_matched_token"] + (f"; {r['network_note']}" if r["network_note"] else ""),
                     "rule": rule})
    for r in read(d("c2_step2_other_network_name_plans.csv")):
        if r["classified_by"] == "plan_code":
            evidence, rule = f"plan_code_{r['code_status'] or 'unknown'}", "R2.3"
        else:
            evidence, rule = "name_word", "R2.2"
        rows.append({**{k: r[k] for k in KEEP}, "network_class": "non_ppo", "network_type": r["network_words"],
                     "evidence": evidence, "evidence_detail": r["matched_tokens"], "rule": rule})
    for r in read(d("c2_step3_unlabelled_name_plans.csv")):
        rows.append({**{k: r[k] for k in KEEP}, "network_class": "unlabelled", "network_type": "",
                     "evidence": "none", "evidence_detail": r.get("name_code_words", ""), "rule": "R2.4"})

    with open(d("c2_plan_network.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OUT_COLS)
        w.writeheader()
        w.writerows(rows)

    by_class = Counter(r["network_class"] for r in rows)
    summary = {
        "plans": len(rows),
        "by_class": dict(by_class),
        "by_class_and_evidence": {f"{k[0]}:{k[1]}": v for k, v in sorted(Counter((r["network_class"], r["evidence"]) for r in rows).items())},
        "by_type": dict(Counter(r["network_type"] for r in rows)),
        "ppo_rate_files": sorted({r["rate_file_name"] for r in rows if r["network_class"] == "ppo"}),
        "rate_files_shared_with_non_ppo": sorted({r["rate_file_name"] for r in rows if r["network_class"] == "ppo"}
                                                 & {r["rate_file_name"] for r in rows if r["network_class"] != "ppo"}),
        "threshold": "none: every class comes from a yes/no rule on explicit evidence (R2.26)",
        "outputs": [d("c2_plan_network.csv")],
    }
    with open(d("c2_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
