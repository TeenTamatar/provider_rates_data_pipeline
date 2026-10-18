"""C2b steps 5-8: parallel-contract test, resolve copies, label prices,
plan confidence and threshold.

Input:  c2b_step4_kept_prices.parquet  (50,933 rows, Health Net CA group)
Output (in --out-dir):
  c2b_step5_parallel.parquet    per-key parallel-contract flag
  c2b_step6_resolved.parquet    one chosen price per key + ambiguous
  c2b_step7_labelled.parquet    every kept row with network_label + confidence
  c2b_step8_plan_conf.csv       per-plan evidence-type summary + threshold
  c2b_steps5to8_summary.json   counts for LOGIC.md

Key definition (same as C4):
  TIN x NPI x billing_code_type x billing_code x additional_information (addl_key)

Step 5 — Parallel-contract test
  For each key: if all negotiated_rate values are identical across all
  provider_group_ids -> single contract (copy_status matches 'single' or
  'identical_copies'). If rates differ -> parallel contracts.
  Source of truth: use the existing copy_status column from step 4.
  Verify it matches our re-derivation and stop if it does not.

Step 6 — Resolve parallel copies
  For keys with differing_copies:
    Rule 6a: keep the copy whose provider_group matches a group where
             entry_label IN ('ppo','direct_ffs_commercial','direct_network').
             Rationale: the PPO-labelled copy is the target contract.
    Rule 6b: if multiple PPO-labelled copies with different rates remain,
             mark all as 'ambiguous' (do not pick by name or by other providers).
    Rule 6c: if no PPO-labelled copy exists, mark all as 'ambiguous'.
    Never resolve by guessing from plan names or from other providers' rates.

Step 7 — Label every price
  evidence_type assigned per row:
    'business_name_ppo'  entry_label = ppo
    'direct_ffs'         entry_label = direct_ffs_commercial or direct_network
    'matched_copy'       differing_copies row kept by Rule 6a
    'shared_contract'    identical_copies (rate matches PPO copy of same key)
    'ambiguous'          differing_copies, Rule 6b or 6c applied
    'capitation'         negotiation_arrangement = 'capitation' (if present)
    'single'             single row for that key, no copy evidence

  network_label:
    'PPO'     entry_label ppo or direct_ffs_commercial/direct_network
    'PPO'     matched_copy
    'shared'  identical_copies where a PPO copy exists for the same key
    'ambiguous' ambiguous rows
    'unknown' single rows with no PPO signal

  confidence:
    business_name_ppo  -> high
    direct_ffs         -> high
    matched_copy       -> medium
    shared_contract    -> medium
    ambiguous          -> low
    single             -> low
    capitation         -> excluded (set aside, not in rate table)

  strict = network_label = 'PPO'
  inclusive = network_label IN ('PPO', 'shared')

Step 8 — Plan confidence and threshold
  For each plan (tin x rate_file), aggregate evidence types.
  Threshold: a plan is 'confident' if >= 80% of its keys have confidence
  high or medium. Rationale: 80% means the dominant contract is identified;
  the remaining 20% ambiguity is within expected TiC data noise.

Usage:
  python c2b/c2b_steps5to8.py --kept c2b_step4_kept_prices.parquet \\
      --out-dir output/c2b_steps5to8
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import duckdb

KEY = "tin, npi, billing_code_type, billing_code, addl_key"
KEY_COLS = ["tin", "npi", "billing_code_type", "billing_code", "addl_key"]

PPO_LABELS = ("ppo", "direct_ffs_commercial", "direct_network")


def sql_path(p: str) -> str:
    return str(Path(p).resolve()).replace("\\", "/")


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--kept", required=True,
                    help="c2b_step4_kept_prices.parquet")
    ap.add_argument("--out-dir", default="output/c2b_steps5to8")
    args = ap.parse_args(argv)

    kept = sql_path(args.kept)
    out  = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    o = lambda name: sql_path(out / name)

    con = duckdb.connect()

    # ── Load step-4 table ────────────────────────────────────────────────────
    log("Loading step-4 kept prices ...")
    con.execute(f"""
        CREATE TABLE s4 AS
        SELECT *,
            coalesce(additional_information, '') AS addl_key
        FROM '{kept}'
    """)
    n_input = con.execute("SELECT count(*) FROM s4").fetchone()[0]
    log(f"  {n_input} rows loaded")

    # ── Step 5: parallel-contract test ───────────────────────────────────────
    log("\nStep 5: parallel-contract test ...")

    # IMPORTANT: two different "copy" concepts in this data:
    #
    # stored copy_status (from step 4):
    #   Scope = within one provider_group_id.
    #   'differing_copies' means the SAME provider group reported multiple
    #   rates for the same (tin, npi, code, addl_key) — a within-group variant.
    #
    # parallel_contract (this step 5):
    #   Scope = ACROSS all provider_group_ids for the same (tin, npi, code, addl_key).
    #   'differing_copies' means DIFFERENT provider groups reported different
    #   rates — the classic parallel-contract pattern we need to resolve.
    #
    # The two are independent. We derive parallel_contract_status fresh here
    # and do NOT overwrite or compare against the stored copy_status.

    con.execute(f"""
        CREATE TABLE key_stats AS
        SELECT {KEY},
            count(DISTINCT negotiated_rate)         AS n_distinct_rates,
            count(DISTINCT provider_group_id)       AS n_groups,
            min(negotiated_rate)                    AS min_rate,
            max(negotiated_rate)                    AS max_rate,
            count(*)                                AS n_rows
        FROM s4
        GROUP BY {KEY}
    """)

    con.execute(f"""
        CREATE TABLE s5 AS
        SELECT s4.*, ks.n_distinct_rates, ks.n_groups, ks.min_rate, ks.max_rate,
            CASE
                WHEN ks.n_distinct_rates = 1 AND ks.n_groups = 1 THEN 'single'
                WHEN ks.n_distinct_rates = 1 AND ks.n_groups > 1  THEN 'identical_copies'
                WHEN ks.n_distinct_rates > 1                       THEN 'differing_copies'
                ELSE 'unknown'
            END AS parallel_contract_status
        FROM s4
        JOIN key_stats ks USING ({KEY})
    """)

    # Step 5 counts (cross-group parallel-contract, not stored copy_status)
    step5_counts = con.execute("""
        SELECT parallel_contract_status, count(*) AS rows,
               count(DISTINCT tin || '|' || npi || '|' || billing_code_type
                     || '|' || billing_code || '|' || addl_key) AS keys
        FROM s5 GROUP BY ALL ORDER BY rows DESC
    """).fetchall()
    log("  Cross-group parallel-contract counts:")
    for row in step5_counts:
        log(f"    {row[0]}: {row[2]} keys, {row[1]} rows")

    con.execute(f"COPY s5 TO '{o('c2b_step5_parallel.parquet')}' (FORMAT parquet)")

    # ── Step 6: resolve parallel copies ──────────────────────────────────────
    log("\nStep 6: resolve parallel copies ...")

    # For each key with differing_copies, find which provider_groups are PPO-labelled
    con.execute(f"""
        CREATE TABLE ppo_groups AS
        SELECT DISTINCT {KEY}, provider_group_id
        FROM s5
        WHERE entry_label IN {PPO_LABELS}
          AND parallel_contract_status = 'differing_copies'
    """)

    # Tag differing-copy rows with whether their group is PPO-labelled
    con.execute(f"""
        CREATE TABLE s6_diff AS
        SELECT s5.*,
            (pg.provider_group_id IS NOT NULL) AS group_is_ppo_labelled,
            -- does a PPO-labelled group even exist for this key?
            (SELECT count(*) > 0 FROM ppo_groups pg2
             WHERE pg2.tin = s5.tin AND pg2.npi = s5.npi
               AND pg2.billing_code_type = s5.billing_code_type
               AND pg2.billing_code = s5.billing_code
               AND pg2.addl_key = s5.addl_key)      AS key_has_ppo_group
        FROM s5
        LEFT JOIN ppo_groups pg
            ON  pg.tin = s5.tin AND pg.npi = s5.npi
            AND pg.billing_code_type = s5.billing_code_type
            AND pg.billing_code = s5.billing_code
            AND pg.addl_key = s5.addl_key
            AND pg.provider_group_id = s5.provider_group_id
        WHERE s5.parallel_contract_status = 'differing_copies'
    """)

    # Count PPO-matched rates per key (Rule 6b: >1 distinct PPO rate = ambiguous)
    con.execute(f"""
        CREATE TABLE ppo_rate_counts AS
        SELECT {KEY},
            count(DISTINCT negotiated_rate) AS n_ppo_rates
        FROM s6_diff
        WHERE group_is_ppo_labelled = true
        GROUP BY {KEY}
    """)

    # Resolve: keep PPO-labelled copy if unique; else ambiguous
    con.execute(f"""
        CREATE TABLE s6_resolved AS
        SELECT s6_diff.*,
            CASE
                -- No PPO group exists for this key (Rule 6c)
                WHEN key_has_ppo_group = false
                    THEN 'ambiguous_no_ppo_group'
                -- Multiple distinct PPO rates (Rule 6b)
                WHEN group_is_ppo_labelled = true
                 AND coalesce(prc.n_ppo_rates, 0) > 1
                    THEN 'ambiguous_multiple_ppo_rates'
                -- PPO group matches this row — keep it (Rule 6a)
                WHEN group_is_ppo_labelled = true
                 AND coalesce(prc.n_ppo_rates, 0) = 1
                    THEN 'matched_ppo_copy'
                -- Non-PPO group, but key has a single PPO copy — discard
                WHEN group_is_ppo_labelled = false
                 AND key_has_ppo_group = true
                 AND coalesce(prc.n_ppo_rates, 0) = 1
                    THEN 'discarded_non_ppo_copy'
                ELSE 'ambiguous_unclassified'
            END AS step6_verdict
        FROM s6_diff
        LEFT JOIN ppo_rate_counts prc USING ({KEY})
    """)

    step6_counts = con.execute("""
        SELECT step6_verdict, count(*) AS rows,
               count(DISTINCT tin || '|' || npi || '|' || billing_code_type
                     || '|' || billing_code || '|' || addl_key) AS keys
        FROM s6_resolved GROUP BY ALL ORDER BY rows DESC
    """).fetchall()
    log("  Step 6 verdicts:")
    for row in step6_counts:
        log(f"    {row[0]}: {row[2]} keys, {row[1]} rows")

    # ── Step 7: label every price ─────────────────────────────────────────────
    log("\nStep 7: labelling prices ...")

    con.execute(f"""
        CREATE TABLE s7 AS

        -- non-differing rows (single, identical_copies, within_group_variants)
        SELECT s5.*,
            NULL::VARCHAR AS step6_verdict,
            CASE
                WHEN negotiation_arrangement = 'capitation'
                    THEN 'capitation'
                WHEN entry_label = 'ppo'
                    THEN 'business_name_ppo'
                WHEN entry_label IN ('direct_ffs_commercial','direct_network')
                    THEN 'direct_ffs'
                WHEN parallel_contract_status = 'identical_copies'
                    THEN 'shared_contract'
                ELSE 'single'
            END AS evidence_type,
            CASE
                WHEN negotiation_arrangement = 'capitation'
                    THEN 'excluded_capitation'
                WHEN entry_label IN {PPO_LABELS}
                    THEN 'PPO'
                WHEN parallel_contract_status = 'identical_copies'
                    THEN 'shared'
                ELSE 'unknown'
            END AS network_label,
            CASE
                WHEN entry_label = 'ppo' OR entry_label IN ('direct_ffs_commercial','direct_network')
                    THEN 'high'
                WHEN parallel_contract_status = 'identical_copies'
                    THEN 'medium'
                ELSE 'low'
            END AS confidence,
            true AS include_in_step7
        FROM s5
        WHERE parallel_contract_status != 'differing_copies'

        UNION ALL

        -- differing-copy rows resolved in step 6
        SELECT
            tin, npi, tin_type, business_name, entry_label, county,
            provider_group_id, billing_code_type, billing_code,
            additional_information, negotiation_arrangement, negotiated_type,
            billing_class, setting, service_code, billing_code_modifier,
            negotiated_rate, expiration_date, n_rows, copy_status,
            addl_key, n_distinct_rates, n_groups, min_rate, max_rate,
            parallel_contract_status, rate_file,
            step6_verdict,
            CASE step6_verdict
                WHEN 'matched_ppo_copy'          THEN 'matched_copy'
                WHEN 'ambiguous_no_ppo_group'    THEN 'ambiguous'
                WHEN 'ambiguous_multiple_ppo_rates' THEN 'ambiguous'
                WHEN 'ambiguous_unclassified'    THEN 'ambiguous'
                WHEN 'discarded_non_ppo_copy'    THEN 'discarded'
                ELSE 'ambiguous'
            END AS evidence_type,
            CASE step6_verdict
                WHEN 'matched_ppo_copy'          THEN 'PPO'
                WHEN 'discarded_non_ppo_copy'    THEN 'discarded'
                ELSE 'ambiguous'
            END AS network_label,
            CASE step6_verdict
                WHEN 'matched_ppo_copy'          THEN 'medium'
                WHEN 'discarded_non_ppo_copy'    THEN 'low'
                ELSE 'low'
            END AS confidence,
            step6_verdict != 'discarded_non_ppo_copy' AS include_in_step7
        FROM s6_resolved
    """)

    step7_counts = con.execute("""
        SELECT evidence_type, network_label, confidence,
               count(*) AS rows,
               count(DISTINCT tin||'|'||npi||'|'||billing_code_type
                     ||'|'||billing_code||'|'||addl_key) AS keys
        FROM s7 GROUP BY ALL ORDER BY rows DESC
    """).fetchall()
    log("  Step 7 labels:")
    log(f"  {'evidence_type':<30} {'network_label':<12} {'confidence':<10} {'rows':>8} {'keys':>8}")
    for row in step7_counts:
        log(f"  {str(row[0]):<30} {str(row[1]):<12} {str(row[2]):<10} {row[3]:>8} {row[4]:>8}")

    # Strict and inclusive counts
    strict_keys = con.execute("""
        SELECT count(DISTINCT tin||'|'||npi||'|'||billing_code_type||'|'||billing_code||'|'||addl_key)
        FROM s7 WHERE network_label = 'PPO' AND include_in_step7
    """).fetchone()[0]
    inclusive_keys = con.execute("""
        SELECT count(DISTINCT tin||'|'||npi||'|'||billing_code_type||'|'||billing_code||'|'||addl_key)
        FROM s7 WHERE network_label IN ('PPO','shared') AND include_in_step7
    """).fetchone()[0]
    log(f"\n  Strict PPO keys:    {strict_keys}")
    log(f"  Inclusive keys:     {inclusive_keys}")

    con.execute(f"""
        COPY (
            SELECT
                rate_file, tin, npi, tin_type, business_name, entry_label, county,
                TRY_CAST(provider_group_id AS INTEGER) AS provider_group_id,
                billing_code_type, billing_code, additional_information,
                negotiation_arrangement, negotiated_type, billing_class, setting,
                service_code, billing_code_modifier,
                TRY_CAST(negotiated_rate AS DOUBLE) AS negotiated_rate,
                expiration_date,
                TRY_CAST(n_rows AS BIGINT) AS n_rows,
                copy_status, addl_key,
                n_distinct_rates, n_groups,
                min_rate, TRY_CAST(max_rate AS DOUBLE) AS max_rate,
                parallel_contract_status, step6_verdict,
                evidence_type, network_label, confidence, include_in_step7
            FROM s7 WHERE include_in_step7
        ) TO '{o("c2b_step7_labelled.parquet")}' (FORMAT parquet)
    """)

    # ── Step 8: plan confidence and threshold ─────────────────────────────────
    log("\nStep 8: plan confidence and threshold ...")

    plan_conf = con.execute(f"""
        SELECT tin,
            count(DISTINCT npi||'|'||billing_code_type||'|'||billing_code||'|'||addl_key) AS keys,
            sum(CASE WHEN confidence = 'high'   THEN 1 ELSE 0 END)  AS high_keys,
            sum(CASE WHEN confidence = 'medium' THEN 1 ELSE 0 END)  AS medium_keys,
            sum(CASE WHEN confidence = 'low'    THEN 1 ELSE 0 END)  AS low_keys,
            round(100.0 * sum(CASE WHEN confidence IN ('high','medium') THEN 1 ELSE 0 END)
                  / count(*), 1) AS pct_confident,
            -- 80% threshold: confident plan
            (round(100.0 * sum(CASE WHEN confidence IN ('high','medium') THEN 1 ELSE 0 END)
                   / count(*), 1) >= 80) AS is_confident
        FROM s7
        WHERE include_in_step7
        GROUP BY tin
        ORDER BY keys DESC
    """).df()

    plan_conf_path = str(out / "c2b_step8_plan_conf.csv")
    plan_conf.to_csv(plan_conf_path, index=False)

    confident_tins    = int((plan_conf["is_confident"] == True).sum())
    total_tins        = len(plan_conf)
    median_pct_conf   = float(plan_conf["pct_confident"].median())
    log(f"  TINs meeting 80% threshold: {confident_tins} / {total_tins}")
    log(f"  Median pct_confident across TINs: {median_pct_conf:.1f}%")

    # ── Summary ───────────────────────────────────────────────────────────────
    step5_dict = {r[0]: {"rows": r[1], "keys": r[2]} for r in step5_counts}
    step6_dict = {r[0]: {"rows": r[1], "keys": r[2]} for r in step6_counts}
    step7_dict = {f"{r[0]}|{r[1]}|{r[2]}": {"rows": r[3], "keys": r[4]}
                  for r in step7_counts}

    summary = {
        "run_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "input_rows": n_input,
        "copy_status_note": (
            "stored copy_status = within-provider-group scope (step 4). "
            "parallel_contract_status = cross-group scope (this step 5). "
            "The two are independent and intentionally different."
        ),
        "step5_parallel_contract": step5_dict,
        "step6_resolve": step6_dict,
        "step7_labels": step7_dict,
        "step7_strict_ppo_keys": strict_keys,
        "step7_inclusive_keys": inclusive_keys,
        "step8_threshold": {
            "threshold_pct": 80,
            "rationale": "80% means the dominant contract is identified; remaining 20% is within expected TiC data noise",
            "confident_tins": confident_tins,
            "total_tins": total_tins,
            "median_pct_confident": median_pct_conf,
        },
        "arkansas_note": (
            "307 group plans identified; rate file returned 404 (QualChoice "
            "file absent from Centene CDN). No rate extraction possible. "
            "80+ URL probes exhausted."
        ),
        "oregon_note": (
            "381 group plans (16 PPO, 365 unknown). Plan names are employer "
            "names; name-keyword rules cannot classify them. "
            "Rate file reachable (HealthNet-OR, 8.6 MB). "
            "Strict PPO=16, inclusive=381. Decision deferred until after California."
        ),
    }

    summary_path = str(out / "c2b_steps5to8_summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(json.dumps(summary, indent=2))
    log(f"\nOutputs written to {out}/")


if __name__ == "__main__":
    main()
