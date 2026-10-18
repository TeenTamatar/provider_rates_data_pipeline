"""
Network evidence deep-dive.

Questions:
  A. Do TINs with PPO-labelled rows also appear in shared/single territory?
     If yes → inclusive label (shared=PPO) is supported for those TINs.
  B. How many unique NPIs/TINs/codes at strict vs inclusive confidence?
  C. Rate-spread analysis for the 690 lowest-rate-resolved ambiguous keys.
  D. For each evidence_type, what fraction of keys per TIN are that type?
     (Supports / contradicts the 80% confidence threshold.)
"""
import argparse, json, sys
from pathlib import Path
import duckdb

ap = argparse.ArgumentParser()
ap.add_argument("--step7",  default="output/c2b_steps5to8/c2b_step7_labelled.parquet")
ap.add_argument("--c4",     default="output/c4_v2/c4_chosen.parquet")
ap.add_argument("--out",    default="output/c2b_steps5to8/network_analysis.json")
args = ap.parse_args()

con = duckdb.connect()

# ── Load ──────────────────────────────────────────────────────────────────────
con.execute(f"CREATE TABLE s7 AS SELECT * FROM '{args.step7}'")
con.execute(f"CREATE TABLE c4 AS SELECT * FROM '{args.c4}'")

results = {}

# ── A. PPO-labelled TINs: what other evidence types do they have? ──────────────
ppo_tins = con.execute("""
    SELECT DISTINCT tin FROM s7
    WHERE network_label = 'PPO' AND include_in_step7
""").fetchall()
ppo_tins_set = {r[0] for r in ppo_tins}
print(f"TINs with ≥1 strict-PPO key: {len(ppo_tins_set)}", flush=True)

# For PPO-labelled TINs: count keys by evidence_type
ppo_tin_profile = con.execute("""
    SELECT evidence_type, network_label,
           count(DISTINCT tin||'|'||npi||'|'||billing_code_type||'|'||billing_code||'|'||addl_key) AS keys,
           count(DISTINCT tin) AS tins
    FROM s7
    WHERE tin IN (SELECT DISTINCT tin FROM s7 WHERE network_label = 'PPO')
      AND include_in_step7
    GROUP BY ALL ORDER BY keys DESC
""").fetchall()

print("\nEvidence profile for TINs that have ≥1 PPO key:")
for row in ppo_tin_profile:
    print(f"  {row[0]:<30} {row[1]:<12} keys={row[2]:>6}  tins={row[3]:>5}")

results["ppo_tins_count"] = len(ppo_tins_set)
results["ppo_tin_evidence_profile"] = [
    {"evidence_type": r[0], "network_label": r[1], "keys": r[2], "tins": r[3]}
    for r in ppo_tin_profile
]

# ── B. Strict vs Inclusive unique provider/code counts ───────────────────────
strict = con.execute("""
    SELECT count(DISTINCT tin||'|'||npi||'|'||billing_code_type||'|'||billing_code||'|'||addl_key) AS keys,
           count(DISTINCT npi) AS npis,
           count(DISTINCT tin) AS tins,
           count(DISTINCT billing_code_type||'|'||billing_code) AS codes
    FROM s7 WHERE network_label = 'PPO' AND include_in_step7
""").fetchone()

inclusive = con.execute("""
    SELECT count(DISTINCT tin||'|'||npi||'|'||billing_code_type||'|'||billing_code||'|'||addl_key) AS keys,
           count(DISTINCT npi) AS npis,
           count(DISTINCT tin) AS tins,
           count(DISTINCT billing_code_type||'|'||billing_code) AS codes
    FROM s7 WHERE network_label IN ('PPO','shared') AND include_in_step7
""").fetchone()

print(f"\nStrict PPO   — keys={strict[0]}  NPIs={strict[1]}  TINs={strict[2]}  codes={strict[3]}")
print(f"Inclusive     — keys={inclusive[0]}  NPIs={inclusive[1]}  TINs={inclusive[2]}  codes={inclusive[3]}")

results["strict"] = {"keys": strict[0], "npis": strict[1], "tins": strict[2], "codes": strict[3]}
results["inclusive"] = {"keys": inclusive[0], "npis": inclusive[1], "tins": inclusive[2], "codes": inclusive[3]}

# ── C. Shared-key TINs: do they appear in PPO-labelled groups? ────────────────
# For shared keys: how many TINs ALSO have ≥1 PPO-labelled key?
shared_tins_with_ppo = con.execute("""
    SELECT count(DISTINCT tin) FROM s7
    WHERE network_label = 'shared' AND include_in_step7
      AND tin IN (SELECT DISTINCT tin FROM s7 WHERE network_label = 'PPO')
""").fetchone()[0]

shared_tins_total = con.execute("""
    SELECT count(DISTINCT tin) FROM s7
    WHERE network_label = 'shared' AND include_in_step7
""").fetchone()[0]

print(f"\nShared-key TINs also with ≥1 PPO key: {shared_tins_with_ppo} / {shared_tins_total}")
results["shared_tins_also_ppo_labelled"] = shared_tins_with_ppo
results["shared_tins_total"] = shared_tins_total

# ── D. For PPO-labelled TINs: what pct of their keys are PPO-labelled? ────────
ppo_pct = con.execute("""
    WITH per_tin AS (
        SELECT tin,
            count(DISTINCT tin||'|'||npi||'|'||billing_code_type||'|'||billing_code||'|'||addl_key) AS total_keys,
            sum(CASE WHEN network_label = 'PPO' THEN 1 ELSE 0 END) AS ppo_keys,
            sum(CASE WHEN network_label = 'shared' THEN 1 ELSE 0 END) AS shared_keys
        FROM s7
        WHERE tin IN (SELECT DISTINCT tin FROM s7 WHERE network_label = 'PPO')
          AND include_in_step7
        GROUP BY tin
    )
    SELECT
        median(100.0 * ppo_keys / total_keys)            AS median_pct_strict,
        median(100.0 * (ppo_keys+shared_keys) / total_keys) AS median_pct_inclusive,
        max(100.0 * ppo_keys / total_keys)               AS max_pct_strict,
        count(*) AS tins
    FROM per_tin
""").fetchone()
print(f"\nFor PPO-labelled TINs:")
print(f"  median strict-PPO %  of keys: {ppo_pct[0]:.1f}%")
print(f"  median inclusive %   of keys: {ppo_pct[1]:.1f}%")
print(f"  max strict-PPO %     of keys: {ppo_pct[2]:.1f}%")

results["ppo_tin_key_pct"] = {
    "median_pct_strict": round(ppo_pct[0], 2),
    "median_pct_inclusive": round(ppo_pct[1], 2),
    "max_pct_strict": round(ppo_pct[2], 2),
    "tins_analysed": ppo_pct[3],
}

# ── E. C4 conflict resolution: check ambiguous-key rate spread ────────────────
spread = con.execute("""
    SELECT resolved_by,
           count(*) AS keys,
           round(median(spread), 3) AS median_spread,
           round(max(spread), 3) AS max_spread,
           round(avg(spread), 3) AS mean_spread
    FROM c4
    GROUP BY resolved_by ORDER BY keys DESC
""").fetchall()
print("\nC4 resolved_by + rate spread:")
for r in spread:
    print(f"  {r[0]:<30} keys={r[1]:>6}  median_spread={r[2]}  max={r[3]}")

results["c4_conflict_resolution"] = [
    {"resolved_by": r[0], "keys": r[1],
     "median_spread": r[2], "max_spread": r[3], "mean_spread": r[4]}
    for r in spread
]

# ── F. Entry-label breakdown for PPO-network rows ─────────────────────────────
entry_labels = con.execute("""
    SELECT entry_label, count(DISTINCT tin) AS tins,
           count(DISTINCT npi) AS npis,
           count(DISTINCT tin||'|'||npi||'|'||billing_code_type||'|'||billing_code||'|'||addl_key) AS keys
    FROM s7
    WHERE network_label = 'PPO' AND include_in_step7
    GROUP BY ALL ORDER BY keys DESC
""").fetchall()
print("\nEntry labels for strict-PPO rows:")
for r in entry_labels:
    print(f"  entry_label={r[0]:<25} tins={r[1]:>4}  npis={r[2]:>5}  keys={r[3]:>5}")

results["ppo_entry_labels"] = [
    {"entry_label": r[0], "tins": r[1], "npis": r[2], "keys": r[3]}
    for r in entry_labels
]

# ── Write results ──────────────────────────────────────────────────────────────
out = Path(args.out)
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(results, indent=2))
print(f"\nWritten to {out}")
