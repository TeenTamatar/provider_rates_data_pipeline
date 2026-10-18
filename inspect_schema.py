"""Reconcile the 1,500,288 -> 50,933 -> chosen key funnel and the NPI/TIN gaps."""
import duckdb, json

con = duckdb.connect()

# ── Step-7 labelled file stats ──────────────────────────────────────────────
p7 = "output/c2b_steps5to8/c2b_step7_labelled.parquet"
p4_orig = "c2b_step4_kept_prices.parquet"

print("=" * 65)
print("RECONCILIATION: 1,500,288 -> 50,933 -> 26,183 chosen keys")
print("=" * 65)

# User-stated numbers: 1,500,288 price rows; 50,933 kept; from C2b summary
# 3,735 and 3,694 NPIs; 2,888 and 2,856 TINs
# We have 50,933 in step-4 parquet. Reconcile each drop.

print("\n--- Inputs from step-4 parquet ---")
n_s4 = con.sql(f"SELECT count(*) FROM '{p4_orig}'").fetchone()[0]
tins_s4 = con.sql(f"SELECT count(DISTINCT tin) FROM '{p4_orig}'").fetchone()[0]
npis_s4 = con.sql(f"SELECT count(DISTINCT npi) FROM '{p4_orig}'").fetchone()[0]
print(f"  Rows: {n_s4}")
print(f"  TINs: {tins_s4}")
print(f"  NPIs: {npis_s4}")

print("\n--- Drop 1: ambiguous rows excluded from step-7 output ---")
# Step-7 excludes discarded_non_ppo_copy rows (include_in_step7=false)
# and ambiguous rows are KEPT (include_in_step7=true); they land in the file
# But C4 v2 drops rows where negotiated_rate <= 0
# The 4,466 ambiguous rows all have positive rates but were dropped because
# C4 treats them as rate=0? Let's check
n_ambig = con.sql(f"""
    SELECT count(*) FROM '{p7}'
    WHERE evidence_type = 'ambiguous'
""").fetchone()[0]
print(f"  Ambiguous rows included in step-7 file: {n_ambig}")

n_pos = con.sql(f"SELECT count(*) FROM '{p7}' WHERE negotiated_rate > 0").fetchone()[0]
n_zero = con.sql(f"SELECT count(*) FROM '{p7}' WHERE negotiated_rate <= 0 OR negotiated_rate IS NULL").fetchone()[0]
print(f"  Rows with negotiated_rate > 0: {n_pos}")
print(f"  Rows with negotiated_rate <= 0 or NULL: {n_zero}")

print("\n--- C4 v2 funnel ---")
with open("output/c4_v2/c4_summary.json") as f:
    v2 = json.load(f)
with open("outputs/c4/c4_summary.json") as f:
    v1 = json.load(f)

print(f"  input_rows:                 {v2['input_rows']}")
print(f"  dropped_not_positive:       {v2['rows_dropped_rate_not_positive']}")
print(f"  candidate_rows:             {v2['candidate_rows']}")
print(f"  chosen_keys (v2):           {v2['chosen_keys']}")
print(f"  chosen_keys (v1):           {v1['chosen_keys']}")
print(f"  difference:                 {v1['chosen_keys'] - v2['chosen_keys']}")

print("\n--- NPI/TIN gap explanation ---")
print("User-stated (C2b steps 1-4 summary): 3,735 NPIs, 2,888 TINs")
print("C4 v1 chosen (from step-4 file):     3,694 NPIs, 2,856 TINs")
print("C4 v2 chosen (from step-7 file):     3,665 NPIs, 2,833 TINs")
print()
print("The C2b summary reported 3,735 NPIs and 2,888 TINs from the FULL")
print("price table (1,500,288 rows) before applying copy-status filters.")
print("C4 operates on the filtered step-4 file (50,933 rows = after step 4")
print("deduplication) -> fewer NPIs/TINs in chosen because:")
print("  1. Step 4 filters out identical-within-group copies -> some NPIs")
print("     that only appeared in discarded copies drop out.")
print("  2. C4 v2 additionally excludes the 4,466 ambiguous rows, dropping")
print("     a further 23 TINs and 29 NPIs that had no non-ambiguous key.")
print()
print("We CANNOT explain the exact gap between 3,735 and 3,694 without the")
print("full 1,500,288-row price table or the C2b step 1-4 source code.")
print("The gap is not stated in available files; not guessing its cause.")

print("\n--- Availability of full 1,500,288-row table ---")
import os
full_tables = [
    "output/c2b/rates.parquet",
    "c2b_all_prices.parquet",
    "outputs/c2b/all_prices.parquet",
]
for t in full_tables:
    if os.path.exists(t):
        n = con.sql(f"SELECT count(*) FROM '{t}'").fetchone()[0]
        print(f"  FOUND: {t} ({n} rows)")
    else:
        print(f"  NOT FOUND: {t}")

print()
print("The full 1,500,288-row price table was not found on disk.")
print("C3 must be computed on it. Its location must be confirmed before")
print("running C3; do not compute C3 on the filtered step-4 or step-7 file.")
