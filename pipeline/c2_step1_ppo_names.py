"""C2 step 1: list every plan whose name contains "PPO".

Reads the C1 plan table and keeps plans whose name contains "PPO" anywhere,
as a whole word ("CA L PPO 30/500") or inside a longer token ("CUSPPO").
Each row records how it matched and whether the name also mentions another
network type, so later steps can weigh the evidence.

Output: output/c2/c2_step1_ppo_name_plans.csv

Usage:
    python pipeline/c2_step1_ppo_names.py
    python pipeline/c2_step1_ppo_names.py --plans output/c1/c1_plans.csv --out output/c2   # all states
"""
import argparse
import csv
import os
import re
from collections import Counter

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PLANS = os.path.join(PROJECT_ROOT, "output", "c1", "c1_plans_ca.csv")
DEFAULT_OUT = os.path.join(PROJECT_ROOT, "output", "c2")

PPO_ANY = re.compile(r"PPO", re.I)
PPO_WORD = re.compile(r"(?<![A-Z])PPO(?![A-Z])", re.I)
OTHER_TYPES = {k: re.compile(r"(?<![A-Z])" + k + r"(?![A-Z])", re.I) for k in ("HMO", "EPO", "POS")}
TOKEN = re.compile(r"[A-Z0-9&+]*PPO[A-Z0-9&+]*", re.I)


def classify(name):
    """Return match details for one plan name, or None if it has no 'PPO'."""
    if not PPO_ANY.search(name):
        return None
    others = [k for k, rx in OTHER_TYPES.items() if rx.search(name)]
    if not others:
        note = ""
    elif others == ["EPO"]:
        # e.g. "CA L CUS PPO SACRAMENTO CITY USD- EPO": an EPO benefit design on the
        # PPO network. C2 classifies by network, so these count as PPO (assumption A8).
        note = "PPO network, EPO benefit design"
    else:
        note = "conflict: also names " + ";".join(others)
    return {
        "ppo_match_type": "word" if PPO_WORD.search(name) else "embedded",
        "ppo_matched_token": ";".join(sorted({t.upper() for t in TOKEN.findall(name)})),
        "other_network_words": ";".join(others),
        "network_note": note,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plans", default=DEFAULT_PLANS, help="C1 plan table (default: output/c1/c1_plans_ca.csv, California only; pass output/c1/c1_plans.csv for all states)")
    ap.add_argument("--out", default=DEFAULT_OUT, help="output folder (default: output/c2)")
    args = ap.parse_args()

    with open(args.plans, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        columns = reader.fieldnames
        rows = []
        for row in reader:
            match = classify(row["plan_name"])
            if match:
                rows.append({**row, **match})

    os.makedirs(args.out, exist_ok=True)
    out_path = os.path.join(args.out, "c2_step1_ppo_name_plans.csv")
    extra = ["ppo_match_type", "ppo_matched_token", "other_network_words", "network_note"]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns + extra)
        w.writeheader()
        w.writerows(rows)

    print(f"wrote {len(rows)} plans to {out_path}")
    print("by brand / state:", dict(Counter((r["brand"], r["state"]) for r in rows).most_common()))
    print("match type:", dict(Counter(r["ppo_match_type"] for r in rows)))
    print("tokens:", dict(Counter(r["ppo_matched_token"] for r in rows).most_common()))
    print("also names another type:", dict(Counter(r["other_network_words"] or "none" for r in rows)))


if __name__ == "__main__":
    main()
