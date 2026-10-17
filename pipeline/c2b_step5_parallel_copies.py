"""C2b step 5: parallel-contract test.

For every TIN-NPI pair and price key, do the provider groups that price it agree?

1. Deduplicate the step 3 price table to one row per group x key x rate. Health Net
   repeats revenue-code price lists under many items (RC 0636 appears as 129 items,
   each carrying the same 721 drug prices), so most rows are exact copies; the
   duplicate count is kept.
2. Join the deduplicated prices to the group members (step 1) to get pair x key rows.
   Only this test table is expanded to NPIs; it stays small.
3. Classify each pair x key:
     single                one group prices it, one rate
     within_group_variants one group prices it, several rates (left to C4)
     identical_copies      2+ groups, all the same rate: one contract
     differing_copies      2+ groups, different rates: parallel contracts (step 6)
4. Describe each case with what step 6 can use: rates, ratio, the groups involved,
   their step 2 markers and hints for this pair, group sizes, capitation.

The price key is the one from step 4 (R2.19): code type, code, additional_information,
arrangement, negotiated type, billing class, setting, place of service, modifier.

Outputs (output/c2b/):
  c2b_step5_price_keys.parquet       group x key x rate, with n_rows (duplicate count)
  c2b_step5_pair_key_copies.parquet  one row per TIN-NPI x key, with its status
  c2b_step5_group_copy_stats.csv     per group: pair-keys by status
  c2b_step5_summary.json

Usage:
    python pipeline/c2b_step5_parallel_copies.py
    python pipeline/c2b_step5_parallel_copies.py --dir output/c2b
"""
import argparse
import json
import os

import duckdb

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DIR = os.path.join(PROJECT_ROOT, "output", "c2b")

KEY = ["billing_code_type", "billing_code", "additional_information", "negotiation_arrangement", "negotiated_type",
       "billing_class", "setting", "service_code", "billing_code_modifier"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=DEFAULT_DIR, help="folder with the step 1-3 outputs, also the output folder")
    args = ap.parse_args()
    d = lambda name: os.path.join(args.dir, name).replace("\\", "/")

    con = duckdb.connect()
    q = lambda sql: con.execute(sql).fetchall()
    key = ", ".join(KEY)
    key_p = ", ".join(f"pk.{k}" for k in KEY)

    # 1. Deduplicate prices.
    con.execute(f"""create table pk as
        select rate_file, provider_group_id, {key}, negotiated_rate, count(*) n_rows,
               count(distinct item_index) n_items
        from read_parquet('{d("c2b_prices.parquet")}') group by all""")
    con.execute(f"copy pk to '{d('c2b_step5_price_keys.parquet')}' (format parquet)")

    # Members with their step 2 entry label; group labels and sizes.
    con.execute(f"""create table mem as
        select g.rate_file, g.provider_group_id, g.tin, g.npi,
               string_agg(distinct coalesce(nullif(x.member_label, ''), 'none'), ';') entry_label
        from read_parquet('{d("c2b_groups.parquet")}') g
        left join read_csv('{d("c2b_step2_member_labels.csv")}', all_varchar=true) x
          on x.rate_file = g.rate_file and x.provider_group_id::int = g.provider_group_id
         and x.member_index::int = g.member_index
        group by all""")
    con.execute(f"""create table grp as
        select l.rate_file, l.provider_group_id::int provider_group_id, l.name_label, coalesce(l.hints, '') hints,
               s.n_npis, s.n_tin_npi_pairs
        from read_csv('{d("c2b_step2_group_name_labels.csv")}', all_varchar=true) l
        join read_parquet('{d("c2b_group_summary.parquet")}') s
          on s.rate_file = l.rate_file and s.provider_group_id = l.provider_group_id::int""")

    # 2-3. Pair x key test table.
    con.execute(f"""create table pairkey as
        with x as (
            select m.rate_file, m.tin, m.npi, {key_p}, pk.provider_group_id, pk.negotiated_rate,
                   m.entry_label, g.name_label, g.hints, g.n_tin_npi_pairs
            from pk join mem m on m.rate_file = pk.rate_file and m.provider_group_id = pk.provider_group_id
            join grp g on g.rate_file = pk.rate_file and g.provider_group_id = pk.provider_group_id)
        select rate_file, tin, npi, {key},
               count(distinct provider_group_id) n_groups,
               count(distinct negotiated_rate) n_rates,
               min(negotiated_rate) min_rate, max(negotiated_rate) max_rate,
               max(negotiated_rate) / nullif(min(negotiated_rate), 0) rate_ratio,
               case when count(distinct provider_group_id) = 1 and count(distinct negotiated_rate) = 1 then 'single'
                    when count(distinct provider_group_id) = 1 then 'within_group_variants'
                    when count(distinct negotiated_rate) = 1 then 'identical_copies'
                    else 'differing_copies' end copy_status,
               count(*) > count(distinct provider_group_id) has_within_group_variants,
               string_agg(provider_group_id || '=' || negotiated_rate, ';' order by negotiated_rate, provider_group_id) group_rates,
               string_agg(distinct entry_label, ';') entry_labels,
               string_agg(distinct name_label, ';') group_labels,
               string_agg(distinct nullif(hints, ''), ';') group_hints,
               min(n_tin_npi_pairs) min_group_pairs, max(n_tin_npi_pairs) max_group_pairs,
               bool_or(negotiation_arrangement = 'capitation') capitation
        from x group by all""")
    con.execute(f"copy pairkey to '{d('c2b_step5_pair_key_copies.parquet')}' (format parquet)")

    # Per-group stats: every pair-key the group prices, by status.
    con.execute(f"""copy (
        select pk.rate_file, pk.provider_group_id,
               count(*) pair_keys,
               count(*) filter (where c.copy_status = 'single') single,
               count(*) filter (where c.copy_status = 'within_group_variants') within_group_variants,
               count(*) filter (where c.copy_status = 'identical_copies') identical_copies,
               count(*) filter (where c.copy_status = 'differing_copies') differing_copies
        from (select distinct rate_file, provider_group_id, {key} from pk) pk
        join mem m on m.rate_file = pk.rate_file and m.provider_group_id = pk.provider_group_id
        join pairkey c on c.rate_file = pk.rate_file and c.tin = m.tin and c.npi = m.npi
         and {" and ".join(f"c.{k} is not distinct from pk.{k}" for k in KEY)}
        group by 1, 2 order by 1, 2) to '{d("c2b_step5_group_copy_stats.csv")}' (header)""")

    dedup = q("select count(*), sum(n_rows), max(n_rows), count(*) filter (where n_rows > 1) from pk")[0]
    status = {r[0]: {"pair_keys": r[1], "pairs": r[2]} for r in q(
        "select copy_status, count(*), count(distinct tin || ':' || npi) from pairkey group by 1 order by 1")}
    by_type = {f"{r[0]} {r[1]}": {"multi_group": r[2], "differing": r[3]} for r in q(
        """select billing_code_type, billing_class, count(*) filter (where n_groups > 1),
                  count(*) filter (where copy_status = 'differing_copies') from pairkey group by 1, 2 order by 1, 2""")}
    diff = q("""select median(rate_ratio), quantile_cont(rate_ratio, 0.9), max(rate_ratio), max(n_groups),
                       count(*) filter (where n_rates > 2),
                       count(*) filter (where capitation),
                       count(*) filter (where entry_labels like '%ppo%'),
                       count(distinct tin || ':' || npi), count(distinct tin)
                from pairkey where copy_status = 'differing_copies'""")[0]
    hints = {r[0]: r[1] for r in q("""select coalesce(group_hints, '(none)'), count(*) from pairkey
                                      where copy_status = 'differing_copies' group by 1 order by 2 desc""")}
    ppo_marked = q("""select count(*), count(*) filter (where n_groups > 1) from pairkey
                      where entry_labels like '%ppo%' and entry_labels not like '%none%'""")[0]
    summary = {
        "dedup": {"price_rows": dedup[1], "distinct_group_key_rate": dedup[0], "max_copies_of_one_row": dedup[2],
                  "rows_repeated": dedup[3], "duplicate_share": round(1 - dedup[0] / dedup[1], 4)},
        "pair_keys_by_status": status,
        "by_code_type_and_class": by_type,
        "differing_copies": {
            "pairs": diff[7], "tins": diff[8],
            "median_rate_ratio": round(diff[0], 3), "p90_rate_ratio": round(diff[1], 3), "max_rate_ratio": round(diff[2], 3),
            "max_groups_per_case": diff[3], "cases_with_3plus_rates": diff[4], "cases_with_capitation": diff[5],
            "cases_touching_ppo_marked_entry": diff[6], "by_group_hints": hints},
        "ppo_marked_pair_keys": {"total": ppo_marked[0], "priced_by_another_group": ppo_marked[1]},
        "outputs": [d("c2b_step5_price_keys.parquet"), d("c2b_step5_pair_key_copies.parquet"),
                    d("c2b_step5_group_copy_stats.csv")],
    }
    with open(d("c2b_step5_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
