"""C2b step 3: build the price table and flag capitation.

Reads the prices of every in-scope rate file from the shared scan (pipeline/rate_scan.py,
output/scan/; the plan table picks the files, as in step 1), so no rate file is parsed again,
into one normalized price table, one row per published price x provider group it applies to:

  c2b_prices.parquet   rate_file, item_index, billing_code_type, billing_code,
                       negotiation_arrangement, rate_index, provider_group_id,
                       price_index, negotiated_type, negotiated_rate, expiration_date,
                       billing_class, setting, service_code (;-joined),
                       billing_code_modifier (;-joined), additional_information

Prices are not expanded to NPIs. Later steps (4-6) read this table.

Capitation (negotiation_arrangement = capitation) is a fixed per-member payment, the
HMO payment model, so capitation prices are flagged `exclude_capitation`. The flag is
per price; a group is only excluded as a whole if all its prices are capitation.

  c2b_step3_capitation_groups.csv   one row per group with any capitation price
  c2b_step3_summary.json

Usage:
    python pipeline/c2b_step3_capitation.py
    python pipeline/c2b_step3_capitation.py --plans output/c1/c1_plans_ca.csv --scan output/scan --out output/c2b
"""
import argparse
import json
import os
import sys

import duckdb

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from c2b_step1_groups import DEFAULT_OUT, DEFAULT_PLANS, DEFAULT_SCAN, nonempty, scanned_files_for  # noqa: E402

TEXT_COLS = ["billing_code_type", "billing_code", "negotiation_arrangement", "negotiated_type", "expiration_date",
             "billing_class", "setting", "service_code", "billing_code_modifier", "additional_information"]
# Scan counters reported per file, under the names this step has always used.
STAT_NAMES = {"items": "item_objects", "negotiated_rates": "rate_objects", "prices": "price_objects",
              "price_group_rows": "price_group_rows", "inline_provider_groups": "inline_group_sets"}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plans", default=DEFAULT_PLANS, help="in-scope plan table (default: output/c1/c1_plans_ca.csv)")
    ap.add_argument("--scan", default=DEFAULT_SCAN, help="shared scan folder (default: output/scan)")
    ap.add_argument("--out", default=DEFAULT_OUT, help="output folder (default: output/c2b)")
    args = ap.parse_args()

    files = scanned_files_for(args.plans, args.scan, "prices")
    os.makedirs(args.out, exist_ok=True)
    con = duckdb.connect()
    text = {c: nonempty(c) for c in TEXT_COLS}
    union = " union all ".join(
        f"""select '{label}' rate_file, cast(item_index as integer) item_index, {text['billing_code_type']},
                   {text['billing_code']}, {text['negotiation_arrangement']}, cast(rate_index as integer) rate_index,
                   cast(provider_group_id as integer) provider_group_id, cast(price_index as integer) price_index,
                   {text['negotiated_type']}, negotiated_rate, {text['expiration_date']}, {text['billing_class']},
                   {text['setting']}, {text['service_code']}, {text['billing_code_modifier']},
                   {text['additional_information']}
            from '{path}' where provider_group_id <> '-1'""" for label, path in files)
    con.execute(f"create table p as {union}")
    prices_path = os.path.join(args.out, "c2b_prices.parquet")
    con.execute(f"copy p to '{prices_path}' (format parquet)")

    q = lambda sql: con.execute(sql).fetchall()
    cap_path = os.path.join(args.out, "c2b_step3_capitation_groups.csv")
    con.execute(f"""copy (select rate_file, provider_group_id, count(*) prices,
                                 count(*) filter (where negotiation_arrangement = 'capitation') capitation_prices,
                                 string_agg(distinct billing_code, ';' order by billing_code)
                                   filter (where negotiation_arrangement = 'capitation') capitation_codes,
                                 case when count(*) = count(*) filter (where negotiation_arrangement = 'capitation')
                                      then 'excluded' else 'capitation_prices_flagged' end group_effect
                          from p group by 1, 2 having capitation_prices > 0 order by 1, 2)
                    to '{cap_path}' (header)""")
    per_file = {}
    for label, _ in files:
        with open(os.path.join(args.scan, f"{label}_stats.json"), encoding="utf-8") as f:
            counts = json.load(f)["counts"]
        stats = {name: counts.get(key, 0) for name, key in STAT_NAMES.items()}
        arr = dict(q(f"select negotiation_arrangement, count(*) from p where rate_file = '{label}' group by 1"))
        cap = q(f"""select count(distinct billing_code), count(distinct provider_group_id), count(*),
                           count(distinct (item_index, rate_index, price_index))
                    from p where rate_file = '{label}' and negotiation_arrangement = 'capitation'""")[0]
        cap_types = dict(q(f"""select negotiated_type || ' / ' || billing_class, count(*) from p
                               where rate_file = '{label}' and negotiation_arrangement = 'capitation' group by 1"""))
        groups_all_cap = q(f"select count(*) from read_csv('{cap_path}') where rate_file = '{label}' and group_effect = 'excluded'")[0][0]
        per_file[label] = {**stats, "price_group_rows_by_arrangement": arr,
                           "capitation": {"codes": cap[0], "groups": cap[1], "price_group_rows": cap[2],
                                          "published_prices": cap[3], "by_type_and_class": cap_types,
                                          "groups_with_only_capitation": groups_all_cap}}
    summary = {"rate_files": per_file, "outputs": [prices_path, cap_path]}
    with open(os.path.join(args.out, "c2b_step3_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
