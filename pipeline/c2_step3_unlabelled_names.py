"""C2 step 3: list every plan whose name names no network type.

The complement of steps 1 and 2: plans whose name contains neither PPO nor
HMO, EPO or POS, nor a non-PPO plan code from reference/network_codes.csv,
using the same matching rules. Each row also lists the short
code words in the name (e.g. "EOA", "SAL", "CUS") so they can be decoded.

Output: output/c2/c2_step3_unlabelled_name_plans.csv

Usage:
    python pipeline/c2_step3_unlabelled_names.py
    python pipeline/c2_step3_unlabelled_names.py --plans output/c1/c1_plans.csv --codes reference/network_codes.csv   # all states
"""
import argparse
import csv
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from c2_step1_ppo_names import classify as ppo_match  # noqa: E402
from c2_step2_other_network_names import classify as other_match, load_codes  # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PLANS = os.path.join(PROJECT_ROOT, "output", "c1", "c1_plans_ca.csv")
DEFAULT_CODES = os.path.join(PROJECT_ROOT, "reference", "network_codes.csv")
DEFAULT_OUT = os.path.join(PROJECT_ROOT, "output", "c2")

# Short all-capital words, likely abbreviations ("EOA", "SAL", "CUS"); state codes excluded.
CODE_WORD = re.compile(r"\b[A-Z][A-Z&]{1,5}\b")
SKIP = {"CA", "OR", "AR", "NY"}


def code_words(name):
    return ";".join(w for w in dict.fromkeys(CODE_WORD.findall(name)) if w not in SKIP)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plans", default=DEFAULT_PLANS, help="C1 plan table (default: output/c1/c1_plans_ca.csv, California only; pass output/c1/c1_plans.csv for all states)")
    ap.add_argument("--codes", default=DEFAULT_CODES, help="plan code table (default: reference/network_codes.csv)")
    ap.add_argument("--out", default=DEFAULT_OUT, help="output folder (default: output/c2)")
    args = ap.parse_args()

    codes = load_codes(args.codes)
    with open(args.plans, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        columns = reader.fieldnames
        total, rows = 0, []
        for row in reader:
            total += 1
            name = row["plan_name"]
            if not ppo_match(name) and not other_match(name, row["brand"], codes):
                rows.append({**row, "name_code_words": code_words(name)})

    os.makedirs(args.out, exist_ok=True)
    out_path = os.path.join(args.out, "c2_step3_unlabelled_name_plans.csv")
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns + ["name_code_words"])
        w.writeheader()
        w.writerows(rows)

    print(f"wrote {len(rows)} of {total} plans to {out_path}")
    print("by brand / state:", dict(Counter((r["brand"], r["state"]) for r in rows).most_common()))
    ca = [r for r in rows if r["state"] == "CA"]
    words = Counter(w for r in ca for w in r["name_code_words"].split(";") if w)
    print("California code words:", dict(words.most_common(25)))


if __name__ == "__main__":
    main()
