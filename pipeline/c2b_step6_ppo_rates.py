"""C2b step 6: the final rate table for the target network (closes C2b; PPO by default).

Applies every C2b decision to the deduplicated prices and writes the target network's rates
(`--network`, default PPO; the same rules for any network type) that the later challenges use. One row per TIN-NPI x provider group x price key x rate.
Each row is either kept or dropped with one reason (first that applies):

  excluded_entry     TIN entry marked other_network / other_product (step 2, R2.13-R2.14)
  capitation         negotiation_arrangement = capitation (step 3, R2.17)
  expired            expiration_date before the run date (step 4, R2.18)
  parallel_contract  the pair x key has different rates in different groups
                     (step 5 differing_copies; strict route, A9, R2.24)
  no_target_evidence the row has no target-network marker and unmarked rates are not kept
                     (`--unmarked drop`, the default for every network except PPO; A9's reason for
                     keeping unmarked rates, that PPO is the widest network, does not hold for others)

Kept rows carry an `evidence` value instead of a score (R2.26):
  <target>_marker     the TIN entry or its network_name says the target network (ppo_marker:
                      "PPO: <County>", "HealthNet PPO")
  unconflicted_rate   no marker and no evidence against it (A9)
plus `copy_status` (single, identical_copies, within_group_variants) and the
entry's hint (`entry_label`), if any.

Outputs (output/c2b/):
  c2b_<target>_rates.parquet / .csv   kept rows: the target network's rates (c2b_ppo_rates for PPO)
  c2b_step6_dropped.csv          dropped rows with drop_reason
  c2b_step6_summary.json

Usage:
    python pipeline/c2b_step6_ppo_rates.py
    python pipeline/c2b_step6_ppo_rates.py --run-date 2026-10-18
    python pipeline/c2b_step6_ppo_rates.py --dir output/c2b_hmo --network HMO --run-date 2026-10-18
"""
import argparse
import datetime as dt
import json
import os

import duckdb

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DIR = os.path.join(PROJECT_ROOT, "output", "c2b")

KEY = ["billing_code_type", "billing_code", "additional_information", "negotiation_arrangement", "negotiated_type",
       "billing_class", "setting", "service_code", "billing_code_modifier"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=DEFAULT_DIR, help="folder with the C2b step 1-5 outputs, also the output folder")
    ap.add_argument("--run-date", default=dt.date.today().isoformat(), help="prices expiring before this are dropped")
    ap.add_argument("--network", default="PPO", help="target network type, as in step 2 (default: PPO)")
    ap.add_argument("--unmarked", choices=["keep", "drop"],
                    help="unconflicted rates without a target marker (default: keep for PPO, A9; drop otherwise)")
    args = ap.parse_args()
    unmarked = args.unmarked or ("keep" if args.network.upper() == "PPO" else "drop")
    t = args.network.lower()
    out = f"c2b_{t}_rates"
    d = lambda name: os.path.join(args.dir, name).replace("\\", "/")

    con = duckdb.connect()
    q = lambda sql: con.execute(sql).fetchall()
    key_p = ", ".join(f"p.{k}" for k in KEY)

    con.execute(f"""create table mem as
        select g.rate_file, g.provider_group_id, g.tin, g.npi, g.tin_type, g.business_name,
               coalesce(nullif(x.member_label, ''), 'none') entry_label, coalesce(x.county, '') county
        from read_parquet('{d("c2b_groups.parquet")}') g
        join read_csv('{d("c2b_step2_member_labels.csv")}', all_varchar=true) x
          on x.rate_file = g.rate_file and x.provider_group_id::int = g.provider_group_id
         and x.member_index::int = g.member_index""")
    con.execute(f"""create table rows as
        select m.rate_file, m.tin, m.npi, m.tin_type, m.business_name, m.entry_label, m.county,
               p.provider_group_id, {key_p}, p.negotiated_rate, p.n_rows,
               e.expiration_date, c.copy_status,
               case when m.entry_label in ('other_network', 'other_product') then 'excluded_entry'
                    when p.negotiation_arrangement = 'capitation' then 'capitation'
                    when e.expiration_date < '{args.run_date}' then 'expired'
                    when c.copy_status = 'differing_copies' then 'parallel_contract'
                    when '{unmarked}' = 'drop' and m.entry_label <> '{t}' then 'no_target_evidence'
                    else '' end drop_reason
        from read_parquet('{d("c2b_step5_price_keys.parquet")}') p
        join (select rate_file, provider_group_id, {", ".join(KEY)}, negotiated_rate, min(expiration_date) expiration_date
              from read_parquet('{d("c2b_prices.parquet")}') group by all) e
          on e.rate_file = p.rate_file and e.provider_group_id = p.provider_group_id
         and e.negotiated_rate = p.negotiated_rate and {" and ".join(f"e.{k} is not distinct from p.{k}" for k in KEY)}
        join mem m on m.rate_file = p.rate_file and m.provider_group_id = p.provider_group_id
        left join read_parquet('{d("c2b_step5_pair_key_copies.parquet")}') c
          on c.rate_file = p.rate_file and c.tin = m.tin and c.npi = m.npi
         and {" and ".join(f"c.{k} is not distinct from p.{k}" for k in KEY)}""")
    con.execute(f"""create table kept as
        select * exclude (drop_reason),
               case when entry_label = '{t}' then '{t}_marker' else 'unconflicted_rate' end evidence
        from rows where drop_reason = ''
        order by tin, npi, billing_code_type, billing_code, additional_information, provider_group_id""")
    con.execute(f"copy kept to '{d(out + '.parquet')}' (format parquet)")
    con.execute(f"copy kept to '{d(out + '.csv')}' (header)")
    con.execute(f"""copy (select * from rows where drop_reason <> '' order by drop_reason, tin, npi, billing_code)
                    to '{d('c2b_step6_dropped.csv')}' (header)""")

    def counts(table, where="true"):
        r = q(f"""select count(*), sum(n_rows), count(distinct tin || ':' || npi), count(distinct npi), count(distinct tin),
                         count(distinct provider_group_id), count(distinct billing_code_type || billing_code),
                         count(distinct (tin, npi, {", ".join(KEY)}))
                  from {table} where {where}""")[0]
        return {"rows": r[0], "raw_price_rows": r[1], "tin_npi_pairs": r[2], "npis": r[3], "tins": r[4],
                "provider_groups": r[5], "billing_codes": r[6], "pair_keys": r[7]}

    summary = {
        "network": args.network.upper(),
        "unmarked_rates": unmarked,
        "run_date": args.run_date,
        "all": counts("rows"),
        "kept": {**counts("kept"),
                 "by_evidence": dict(q("select evidence, count(*) from kept group by 1 order by 1")),
                 "by_copy_status": dict(q("select copy_status, count(*) from kept group by 1 order by 1")),
                 "by_code_type": dict(q("select billing_code_type || ' ' || billing_class, count(*) from kept group by 1 order by 1")),
                 "by_entry_hint": dict(q("select entry_label, count(*) from kept group by 1 order by 1"))},
        "dropped": {reason: counts("rows", f"drop_reason = '{reason}'")
                    for reason in ("excluded_entry", "capitation", "expired", "parallel_contract")
                    + (("no_target_evidence",) if unmarked == "drop" else ())},
        "pairs_lost": q("""select count(*) from (select distinct tin, npi from rows)
                           where (tin, npi) not in (select tin, npi from kept)""")[0][0],
        "outputs": [d(out + ".parquet"), d(out + ".csv"), d("c2b_step6_dropped.csv")],
    }
    with open(d("c2b_step6_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
