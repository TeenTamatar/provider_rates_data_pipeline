"""C2b step 3: build the price table and flag capitation.

Streams the `in_network` section of every in-scope rate file (from the plan table,
as in step 1) into one normalized price table, one row per published price x
provider group it applies to:

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
    python pipeline/c2b_step3_capitation.py --plans output/c1/c1_plans_ca.csv --rate-dir Data/rate_files --out output/c2b
"""
import argparse
import concurrent.futures as cf
import csv
import json
import os
import sys
import time
from collections import Counter

import duckdb
import ijson

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from c2b_step1_groups import DEFAULT_OUT, DEFAULT_PLANS, DEFAULT_RATE_DIR, rate_files_for  # noqa: E402

PRICE_COLS = ["rate_file", "item_index", "billing_code_type", "billing_code", "negotiation_arrangement",
              "rate_index", "provider_group_id", "price_index", "negotiated_type", "negotiated_rate",
              "expiration_date", "billing_class", "setting", "service_code", "billing_code_modifier",
              "additional_information"]


def parse_prices(job):
    """Stream one rate file's in_network section into a temporary CSV."""
    label, path, tmp_dir = job
    t0 = time.time()
    out = os.path.join(tmp_dir, f"{label}_prices.csv")
    stats = Counter()
    with open(path, "rb") as src, open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        for i, item in enumerate(ijson.items(src, "in_network.item", use_float=False)):
            stats["items"] += 1
            head = [label, i, item.get("billing_code_type", ""), str(item.get("billing_code", "")),
                    item.get("negotiation_arrangement", "")]
            for r, rate in enumerate(item.get("negotiated_rates") or []):
                stats["negotiated_rates"] += 1
                refs = rate.get("provider_references") or []
                if rate.get("provider_groups"):
                    stats["inline_provider_groups"] += 1  # not used by this file; counted so it is visible
                for p, price in enumerate(rate.get("negotiated_prices") or []):
                    stats["prices"] += 1
                    tail = [p, price.get("negotiated_type", ""), str(price.get("negotiated_rate", "")),
                            price.get("expiration_date", ""), price.get("billing_class", ""),
                            price.get("setting", ""), ";".join(map(str, price.get("service_code") or [])),
                            ";".join(map(str, price.get("billing_code_modifier") or [])),
                            price.get("additional_information", "") or ""]
                    for gid in refs:
                        w.writerow(head + [r, gid] + tail)
                        stats["price_group_rows"] += 1
    stats["seconds"] = round(time.time() - t0, 1)
    return label, out, dict(stats)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plans", default=DEFAULT_PLANS, help="in-scope plan table (default: output/c1/c1_plans_ca.csv)")
    ap.add_argument("--rate-dir", default=DEFAULT_RATE_DIR, help="folder holding the rate files (default: Data/rate_files)")
    ap.add_argument("--out", default=DEFAULT_OUT, help="output folder (default: output/c2b)")
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 2)
    args = ap.parse_args()

    files = rate_files_for(args.plans, args.rate_dir)
    tmp_dir = os.path.join(args.out, "_tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    print(f"parsing prices in {len(files)} rate file(s): {[f for f, _ in files]}", file=sys.stderr)
    with cf.ProcessPoolExecutor(max_workers=min(args.workers, len(files))) as pool:
        results = list(pool.map(parse_prices, [(label, path, tmp_dir) for label, path in files]))

    con = duckdb.connect()
    sources = ", ".join(f"'{p}'" for _, p, _ in results)
    con.execute(f"""create table p as select * from read_csv([{sources}], header=false, all_varchar=true,
                    names={PRICE_COLS})""")
    for col in ("item_index", "rate_index", "provider_group_id", "price_index"):
        con.execute(f"alter table p alter {col} type integer")
    con.execute("alter table p alter negotiated_rate type double")
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
    for label, _, stats in results:
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
    for _, path, _ in results:
        os.remove(path)
    os.rmdir(tmp_dir)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
