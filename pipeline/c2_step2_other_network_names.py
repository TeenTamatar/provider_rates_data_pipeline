"""C2 step 2: list every plan whose name marks it as a non-PPO network type.

Reads the C1 plan table and keeps plans whose name either (except names that say
PPO and EPO only, e.g. "CUS PPO ... EPO": an EPO design on the PPO network, kept
as PPO in step 1 because C2 classifies by network, assumption A8)
  - names HMO, EPO or POS, as a whole word ("CA L SC HMO 50/1500d") or at the
    end of a longer token ("SALUDHMO"); a letter right after the type rules it
    out, so "POST" is not POS; or
  - contains a brand-specific plan code listed as non-PPO in
    reference/network_codes.csv (e.g. Health Net "EOA").
Each row records how it was classified, which words or codes matched, and
whether the name also says PPO (those plans also appear in step 1 and are
conflicts).

Output: output/c2/c2_step2_other_network_name_plans.csv

Usage:
    python pipeline/c2_step2_other_network_names.py
    python pipeline/c2_step2_other_network_names.py --plans output/c1/c1_plans.csv --codes reference/network_codes.csv   # all states
"""
import argparse
import csv
import os
import re
from collections import Counter

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PLANS = os.path.join(PROJECT_ROOT, "output", "c1", "c1_plans_ca.csv")
DEFAULT_CODES = os.path.join(PROJECT_ROOT, "reference", "network_codes.csv")
DEFAULT_OUT = os.path.join(PROJECT_ROOT, "output", "c2")

NETWORK_TYPES = ("HMO", "EPO", "POS")
WORD = {k: re.compile(r"(?<![A-Z])" + k + r"(?![A-Z])", re.I) for k in NETWORK_TYPES + ("PPO",)}
# A letter may come before the type ("CUSHMO") but not after it, so "POST" is not POS.
ANY = {k: re.compile(k + r"(?![A-Z])", re.I) for k in NETWORK_TYPES + ("PPO",)}
TOKEN = {k: re.compile(r"[A-Z0-9&+]*" + k + r"(?![A-Z])[0-9&+]*", re.I) for k in NETWORK_TYPES}


def load_codes(path):
    """Brand-specific non-PPO plan codes: {brand: [(code, compiled whole-word regex, row)]}."""
    codes = {}
    if not path or not os.path.exists(path):
        return codes
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["network_class"].strip().lower() != "non-ppo":
                continue
            rx = re.compile(r"(?<![A-Z0-9])" + re.escape(row["code"]) + r"(?![A-Z0-9])", re.I)
            codes.setdefault(row["brand"].strip().lower(), []).append((row["code"], rx, row))
    return codes


def classify(name, brand="", codes=None):
    """Return match details for one plan name, or None if nothing marks it non-PPO."""
    found = [k for k in NETWORK_TYPES if ANY[k].search(name)]
    says_ppo = bool(ANY["PPO"].search(name))
    if says_ppo and found == ["EPO"]:
        # PPO network with an EPO benefit design: counted as PPO in step 1 (assumption A8).
        return None
    if found:
        return {
            "classified_by": "network_word",
            "network_words": ";".join(found),
            "match_type": ";".join(f"{k}:{'word' if WORD[k].search(name) else 'embedded'}" for k in found),
            "matched_tokens": ";".join(sorted({t.upper() for k in found for t in TOKEN[k].findall(name)})),
            "code_status": "",
            "also_says_ppo": says_ppo,
        }
    hits = [(c, row) for c, rx, row in (codes or {}).get(brand.lower(), []) if rx.search(name)]
    if hits:
        return {
            "classified_by": "plan_code",
            "network_words": ";".join(c for c, _ in hits),
            "match_type": ";".join(f"{c}:code" for c, _ in hits),
            "matched_tokens": ";".join(c for c, _ in hits),
            "code_status": ";".join(row["status"] for _, row in hits),
            "also_says_ppo": says_ppo,
        }
    return None


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
        rows = []
        for row in reader:
            match = classify(row["plan_name"], row["brand"], codes)
            if match:
                rows.append({**row, **match})

    os.makedirs(args.out, exist_ok=True)
    out_path = os.path.join(args.out, "c2_step2_other_network_name_plans.csv")
    extra = ["classified_by", "network_words", "match_type", "matched_tokens", "code_status", "also_says_ppo"]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns + extra)
        w.writeheader()
        w.writerows(rows)

    print(f"wrote {len(rows)} plans to {out_path}")
    print("by brand / state:", dict(Counter((r["brand"], r["state"]) for r in rows).most_common()))
    print("classified by:", dict(Counter(r["classified_by"] for r in rows)))
    print("network words / codes:", dict(Counter(r["network_words"] for r in rows)))
    print("also says PPO:", sum(r["also_says_ppo"] for r in rows))


if __name__ == "__main__":
    main()
