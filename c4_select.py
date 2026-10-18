"""C4: choose one rate per provider key from the C2b-kept price table.

Key = TIN x NPI x billing_code_type x billing_code x additional_information.
Rules are applied as a funnel; each rule only looks at keys that still hold
more than one distinct rate. Every dropped row is kept in an alternatives
table with a reason. Counts go to c4_summary.json for the logic file.

Terminal:  python c4_select.py --kept data/c2b_step4_kept_prices.parquet --out-dir outputs/c4
Notebook:  main(["--kept", "data/c2b_step4_kept_prices.parquet", "--out-dir", "outputs/c4"])
"""
import argparse
import json
from pathlib import Path

import duckdb

KEY = "tin, npi, billing_code_type, billing_code, addl_key"

# Provisional network ranking (lower = preferred). Replace with the C2b step 6-8
# labels when they exist. Unknown labels fall to the last rank.
NETWORK_RANK_SQL = """
CASE entry_label
  WHEN 'ppo' THEN 0
  WHEN 'direct_ffs_commercial' THEN 1
  WHEN 'direct_network' THEN 1
  WHEN 'none' THEN 2
  WHEN 'ipa' THEN 3
  ELSE 4 END
"""

# (stage table, human label, rule name written to resolved_by)
STAGES = [
    ("s1", "after network label", "network"),
    ("s2", "after modifier (whole service first)", "modifier"),
    ("s3", "after place of service (office first)", "place_of_service"),
    ("s4", "after majority contract", "majority_contract"),
    ("s5", "after lowest rate", "lowest_rate_unresolved"),
]


def sql_path(p):
    return str(Path(p).resolve()).replace("\\", "/")


def conflict_keys(con, table):
    return con.sql(
        f"SELECT count(*) FROM (SELECT 1 FROM {table} GROUP BY {KEY} "
        f"HAVING count(DISTINCT negotiated_rate) > 1)"
    ).fetchone()[0]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--kept", required=True, help="c2b_step4_kept_prices.parquet")
    ap.add_argument("--out-dir", default="outputs/c4")
    args = ap.parse_args(argv)

    kept = sql_path(args.kept)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    o = lambda name: sql_path(out / name)

    con = duckdb.connect()

    # ---- Stage 0: candidates -------------------------------------------------
    n_input = con.sql(f"SELECT count(*) FROM '{kept}'").fetchone()[0]
    con.sql(f"""
        CREATE OR REPLACE TABLE s0 AS
        SELECT *, row_number() OVER () AS price_id,
          coalesce(additional_information, '') AS addl_key,
          {NETWORK_RANK_SQL} AS network_rank,
          CASE WHEN coalesce(billing_code_modifier, '') = '' THEN 0 ELSE 1 END AS modifier_rank,
          CASE WHEN service_code = '11' THEN 0 WHEN service_code = '22' THEN 1 ELSE 2 END AS pos_rank
        FROM '{kept}'
        WHERE negotiated_rate > 0
    """)
    n_candidates = con.sql("SELECT count(*) FROM s0").fetchone()[0]
    n_nonpositive = n_input - n_candidates
    con.sql(f"COPY (SELECT * FROM '{kept}' WHERE negotiated_rate IS NULL OR negotiated_rate <= 0) "
            f"TO '{o('c4_dropped_nonpositive.parquet')}' (FORMAT parquet)")

    # ---- Rules 1-3: keep the best-ranked rows within each key ----------------
    for i, col in enumerate(["network_rank", "modifier_rank", "pos_rank"], start=1):
        con.sql(f"""
            CREATE OR REPLACE TABLE s{i} AS
            SELECT * FROM s{i-1}
            QUALIFY {col} = min({col}) OVER (PARTITION BY {KEY})
        """)

    # ---- Rule 4: majority contract = rate carried by most provider groups ----
    con.sql(f"""
        CREATE OR REPLACE TABLE s4 AS
        WITH sup AS (
          SELECT {KEY}, negotiated_rate,
                 count(DISTINCT provider_group_id) AS n_groups_with_rate
          FROM s3 GROUP BY ALL)
        SELECT s3.*, sup.n_groups_with_rate
        FROM s3 JOIN sup USING ({KEY}, negotiated_rate)
        QUALIFY n_groups_with_rate = max(n_groups_with_rate) OVER (PARTITION BY {KEY})
    """)

    # ---- Rule 5: still tied -> lowest rate (deterministic, flagged) ----------
    con.sql(f"""
        CREATE OR REPLACE TABLE s5 AS
        SELECT * FROM s4
        QUALIFY negotiated_rate = min(negotiated_rate) OVER (PARTITION BY {KEY})
    """)

    # ---- One row per key ------------------------------------------------------
    con.sql(f"""
        CREATE OR REPLACE TABLE chosen_base AS
        SELECT * FROM s5
        QUALIFY row_number() OVER (PARTITION BY {KEY} ORDER BY price_id) = 1
    """)

    # ---- Audit: which rule resolved each key ---------------------------------
    for i in range(6):
        con.sql(f"""
            CREATE OR REPLACE TABLE t{i} AS
            SELECT {KEY}, count(DISTINCT negotiated_rate) AS n{i},
                   min(negotiated_rate) AS min_rate, max(negotiated_rate) AS max_rate
            FROM s{i} GROUP BY {KEY}
        """)
    joins = " ".join(f"JOIN t{i} USING ({KEY})" for i in range(1, 6))
    con.sql(f"""
        CREATE OR REPLACE TABLE key_audit AS
        SELECT t0.{KEY.replace(', ', ', t0.')},
               t0.n0 AS n_distinct_rates, t0.min_rate, t0.max_rate,
               t0.max_rate / t0.min_rate AS spread,
               CASE WHEN t0.n0 = 1 THEN 'no_conflict'
                    WHEN t1.n1 = 1 THEN 'network'
                    WHEN t2.n2 = 1 THEN 'modifier'
                    WHEN t3.n3 = 1 THEN 'place_of_service'
                    WHEN t4.n4 = 1 THEN 'majority_contract'
                    ELSE 'lowest_rate_unresolved' END AS resolved_by
        FROM t0 {joins}
    """)

    con.sql(f"""
        CREATE OR REPLACE TABLE chosen AS
        SELECT c.*, a.n_distinct_rates, a.min_rate, a.max_rate, a.spread, a.resolved_by,
               (a.resolved_by = 'lowest_rate_unresolved') AS conflict_unresolved
        FROM chosen_base c JOIN key_audit a USING ({KEY})
    """)

    # ---- Alternatives with a reason ------------------------------------------
    con.sql(f"""
        CREATE OR REPLACE TABLE alternatives AS
        WITH best AS (
          SELECT {KEY},
                 min(network_rank)  AS best_net,
                 min(modifier_rank) AS best_mod,
                 min(pos_rank)      AS best_pos
          FROM s0 GROUP BY {KEY})
        SELECT s0.*, ch.negotiated_rate AS chosen_rate,
          CASE WHEN s0.negotiated_rate = ch.negotiated_rate THEN 'identical_rate'
               WHEN s0.network_rank  > b.best_net THEN 'other_network'
               WHEN s0.modifier_rank > b.best_mod THEN 'modifier_variant'
               WHEN s0.pos_rank      > b.best_pos THEN 'different_setting'
               ELSE 'parallel_contract_alternative' END AS reason_not_chosen
        FROM s0
        JOIN best b USING ({KEY})
        JOIN chosen_base ch USING ({KEY})
        WHERE s0.price_id <> ch.price_id
    """)

    # ---- Checks ----------------------------------------------------------------
    n_keys = con.sql(f"SELECT count(*) FROM (SELECT DISTINCT {KEY} FROM s0)").fetchone()[0]
    n_chosen = con.sql("SELECT count(*) FROM chosen").fetchone()[0]
    n_alt = con.sql("SELECT count(*) FROM alternatives").fetchone()[0]
    assert n_chosen == n_keys, f"chosen {n_chosen} != distinct keys {n_keys}"
    assert n_chosen + n_alt == n_candidates, "chosen + alternatives must equal candidates"
    assert conflict_keys(con, "s5") == 0, "conflicts remain after the last rule"

    # ---- Outputs ---------------------------------------------------------------
    con.sql(f"COPY chosen TO '{o('c4_chosen.parquet')}' (FORMAT parquet)")
    con.sql(f"COPY alternatives TO '{o('c4_alternatives.parquet')}' (FORMAT parquet)")
    con.sql(f"COPY key_audit TO '{o('c4_key_audit.csv')}' (HEADER, DELIMITER ',')")

    funnel = [{"stage": "start (keys with more than one distinct rate)",
               "conflicting_keys": conflict_keys(con, "s0")}]
    for table, label, _ in STAGES:
        funnel.append({"stage": label, "conflicting_keys": conflict_keys(con, table)})
    for prev, cur in zip(funnel, funnel[1:]):
        cur["resolved_by_this_rule"] = prev["conflicting_keys"] - cur["conflicting_keys"]

    by_rule = {r: n for r, n in con.sql(
        "SELECT resolved_by, count(*) FROM chosen GROUP BY ALL").fetchall()}
    by_reason = {r: n for r, n in con.sql(
        "SELECT reason_not_chosen, count(*) FROM alternatives GROUP BY ALL").fetchall()}
    unresolved_spread = con.sql("""
        SELECT median(spread), max(spread) FROM chosen WHERE conflict_unresolved""").fetchone()
    by_code_type = {r: n for r, n in con.sql(
        "SELECT billing_code_type, count(*) FROM chosen GROUP BY ALL").fetchall()}

    summary = {
        "input_rows": n_input,
        "rows_dropped_rate_not_positive": n_nonpositive,
        "candidate_rows": n_candidates,
        "chosen_keys": n_chosen,
        "alternative_rows": n_alt,
        "unique_tins": con.sql("SELECT count(DISTINCT tin) FROM chosen").fetchone()[0],
        "unique_npis": con.sql("SELECT count(DISTINCT npi) FROM chosen").fetchone()[0],
        "keys_by_code_type": by_code_type,
        "keys_resolved_by": by_rule,
        "share_no_conflict": round(by_rule.get("no_conflict", 0) / n_chosen, 4),
        "share_unresolved_lowest_rate": round(by_rule.get("lowest_rate_unresolved", 0) / n_chosen, 4),
        "unresolved_spread_median": unresolved_spread[0],
        "unresolved_spread_max": unresolved_spread[1],
        "alternatives_by_reason": by_reason,
        "funnel": funnel,
    }
    (out / "c4_summary.json").write_text(json.dumps(summary, indent=2, default=str))

    print(json.dumps(summary, indent=2, default=str))
    return summary


if __name__ == "__main__":
    main()
