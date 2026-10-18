"""C2b step 4: contract-date check.

Two questions, both answered from the step 3 price table:

1. Are any prices expired? A price whose `expiration_date` is before the run date is
   flagged `drop_expired` (no longer payable).
2. Do contract dates explain the parallel copies? Groups with the same
   `member_signature` (identical TIN-NPI sets, step 1) are compared on every price key
   (code type, code, sub-code in `additional_information`, arrangement, negotiated
   type, billing class, setting, place of service, modifier). Health Net puts the
   HCPCS/CPT or HIPPS code under each revenue code in `additional_information`
   (RC 0250 + J2405), so it is part of the key. A "case" is one signature x key priced
   by 2+ groups. If copies that disagree on the rate also disagree on
   `expiration_date`, dates explain them; otherwise they are true parallel contracts
   for step 5.

Outputs (output/c2b/):
  c2b_step4_copy_cases.parquet   one row per case: signature, key, n_groups, n_rates,
                                 rates differ, dates differ, min/max rate, group ids
  c2b_step4_summary.json

Usage:
    python pipeline/c2b_step4_contract_dates.py
    python pipeline/c2b_step4_contract_dates.py --run-date 2026-10-17
"""
import argparse
import datetime as dt
import json
import os

import duckdb

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DIR = os.path.join(PROJECT_ROOT, "output", "c2b")

KEY = ["billing_code_type", "billing_code", "additional_information", "negotiation_arrangement", "negotiated_type", "billing_class",
       "setting", "service_code", "billing_code_modifier"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=DEFAULT_DIR, help="folder with the step 1 and 3 outputs (default: output/c2b)")
    ap.add_argument("--run-date", default=dt.date.today().isoformat(), help="date prices must be valid on (default: today)")
    args = ap.parse_args()

    con = duckdb.connect()
    con.execute(f"create view p as select * from read_parquet('{os.path.join(args.dir, 'c2b_prices.parquet')}')")
    con.execute(f"create view g as select * from read_parquet('{os.path.join(args.dir, 'c2b_group_summary.parquet')}')")
    q = lambda sql: con.execute(sql).fetchall()

    expiry = q(f"""select rate_file, count(*), count(*) filter (where expiration_date < '{args.run_date}'),
                          count(*) filter (where expiration_date is null or expiration_date = ''),
                          count(distinct expiration_date), min(expiration_date), max(expiration_date)
                   from p group by 1""")
    key = ", ".join(f"p.{k}" for k in KEY)
    con.execute(f"""create table cases as
        select p.rate_file, g.member_signature, {key},
               count(distinct p.provider_group_id) n_groups,
               count(distinct p.negotiated_rate) n_rates,
               count(distinct p.negotiated_rate) > 1 rates_differ,
               count(distinct p.expiration_date) > 1 dates_differ,
               min(p.negotiated_rate) min_rate, max(p.negotiated_rate) max_rate,
               string_agg(distinct cast(p.provider_group_id as varchar), ';' order by cast(p.provider_group_id as varchar)) group_ids
        from p join g on g.rate_file = p.rate_file and g.provider_group_id = p.provider_group_id
        where g.member_signature in (select member_signature from g group by 1 having count(*) > 1)
        group by all having count(distinct p.provider_group_id) > 1""")
    cases_path = os.path.join(args.dir, "c2b_step4_copy_cases.parquet")
    con.execute(f"copy cases to '{cases_path}' (format parquet)")
    c = q("""select count(*), count(*) filter (where rates_differ),
                    count(*) filter (where rates_differ and dates_differ),
                    count(distinct member_signature), count(distinct member_signature) filter (where rates_differ),
                    median(max_rate / nullif(min_rate, 0)) filter (where rates_differ)
             from cases""")[0]
    by_type = dict(q("""select billing_code_type || ' ' || billing_class, count(*) filter (where rates_differ) || ' of ' || count(*)
                        from cases group by 1 order by 1"""))
    within = q("""select count(*) from (select rate_file, provider_group_id, billing_code_type, billing_code,
                      additional_information, negotiation_arrangement, negotiated_type, billing_class, setting, service_code, billing_code_modifier
                  from p group by all having count(distinct negotiated_rate) > 1)""")[0][0]

    summary = {
        "run_date": args.run_date,
        "expiry": {r[0]: {"prices": r[1], "expired": r[2], "missing_date": r[3], "distinct_dates": r[4],
                          "min_date": r[5], "max_date": r[6]} for r in expiry},
        "parallel_copies": {
            "cases": c[0], "cases_rates_differ": c[1],
            "share_rates_differ": round(c[1] / c[0], 3) if c[0] else None,
            "rates_differ_and_dates_differ": c[2],
            "signatures_with_cases": c[3], "signatures_with_differing_rates": c[4],
            "median_max_to_min_rate_ratio": round(c[5], 3) if c[5] else None,
            "differing_by_code_type_and_class": by_type,
        },
        "same_group_same_key_different_rates": within,
        "outputs": [cases_path],
    }
    with open(os.path.join(args.dir, "c2b_step4_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
