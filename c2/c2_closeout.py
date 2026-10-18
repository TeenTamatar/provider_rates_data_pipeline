"""
C2 close-out checks (items 1-4) + C4/provider deep-dive (item 5).
No network calls. Reads from cached parquets and CSVs.
"""
import csv, json, sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
import pandas as pd
import duckdb

SEP = "=" * 70
def log(m=""): print(m, flush=True)

# ── Load data ─────────────────────────────────────────────────────────────────
merged = pd.read_csv("output/c2/c2_plans_network.csv")
log(f"Merged plans: {len(merged)} rows")

# Load CMS file for raw PlanType lookup
cms_by_prod = defaultdict(lambda: defaultdict(int))  # prod -> {raw_type: count}
with open("data/plan_attributes_py2026.csv", newline="", encoding="utf-8-sig") as f:
    for row in csv.DictReader(f):
        if row.get("BusinessYear","").strip() != "2026": continue
        prod = row.get("HIOSProductId","").strip()
        if not prod: continue
        cms_by_prod[prod][row.get("PlanType","").strip()] += 1

# ─────────────────────────────────────────────────────────────────────────────
log(SEP)
log("ITEM 1: Agreement check — name-based type vs CMS PlanType")
log(SEP)

# Plans with BOTH: evidence_type=name_keyword AND hios_product_id in CMS
def normalise_net(t):
    t = t.strip().upper()
    if "HMO" in t or t == "EOA": return "HMO"
    if t in ("PPO",): return "PPO"
    if t in ("EPO",): return "EPO"
    if t in ("POS",): return "POS"
    return t

name_plans  = merged[merged["evidence_type"] == "name_keyword"].copy()
name_plans["has_hpid"] = name_plans["hios_product_id"].notna() & \
                         (name_plans["hios_product_id"].astype(str).str.strip() != "")

overlap = []
for _, row in name_plans.iterrows():
    if not row["has_hpid"]: continue
    prod = str(row["hios_product_id"]).strip()
    if prod not in cms_by_prod: continue
    types = set(t for t in cms_by_prod[prod] if t)
    if len(types) != 1: continue
    cms_type = normalise_net(list(types)[0])
    our_type = normalise_net(str(row["network_type"]))
    overlap.append({
        "plan_name": row.get("plan_name",""),
        "brand": row.get("brand",""),
        "state": row.get("state",""),
        "our_type": row["network_type"],
        "cms_type": list(types)[0],
        "agree": our_type == cms_type,
    })

log(f"  Name-labelled plans:              {len(name_plans)}")
log(f"  With hios_product_id:             {name_plans['has_hpid'].sum()}")
log(f"  With matching hios_product_id in CMS (unambiguous): {len(overlap)}")
if len(overlap) == 0:
    log("  → ZERO plans have both a name-based label and a CMS record.")
    log("    Reason: name-labelled plans are Health Net CA/AR/OR group plans with")
    log("    EIN-based IDs (not in federal CMS PUF) or OR Health Net plans with EIN.")
    log("    CMS-resolved plans were all previously 'unknown' (Ambetter individual).")
    log("    No cross-validation is possible with this data.")
else:
    matches   = sum(1 for x in overlap if x["agree"])
    mismatches = sum(1 for x in overlap if not x["agree"])
    log(f"  Match:    {matches}")
    log(f"  Mismatch: {mismatches}")
    if mismatches:
        log("\n  Mismatches:")
        for x in overlap:
            if not x["agree"]:
                log(f"    {x['brand']}/{x['state']}  our={x['our_type']:<12}  cms={x['cms_type']:<10}  {x['plan_name'][:55]}")

# ─────────────────────────────────────────────────────────────────────────────
log()
log(SEP)
log("ITEM 2: Pilot — 5 CMS-resolved plans")
log(SEP)

cms_plans = merged[merged["evidence_type"] == "cms_plan_attributes"].copy()
log(f"  Total CMS-resolved plans: {len(cms_plans)}")
log()
for i, (_, row) in enumerate(cms_plans.head(5).iterrows(), 1):
    log(f"  Plan {i}:")
    log(f"    plan_id          : {row.get('plan_id','')}")
    log(f"    plan_name        : {str(row.get('plan_name',''))[:60]}")
    log(f"    brand/state      : {row.get('brand','')}/{row.get('state','')}")
    log(f"    CMS join key     : HIOSProductId={row.get('hios_product_id','')!r}")
    log(f"    CMS PlanType     : {row.get('matched_token','')}")
    log(f"    network_type     : {row.get('network_type','')}")
    log(f"    source_col       : HIOSProductId → CMS PlanType")
    log(f"    confidence       : {row.get('confidence','')}")
    log(f"    quote            : {row.get('quote','')}")
    log(f"    sbc_url          : {str(row.get('sbc_url',''))[:70] or '(none)'}")
    log()

# ─────────────────────────────────────────────────────────────────────────────
log(SEP)
log("ITEM 3: CMS-resolved non-PPO plans — raw PlanType breakdown and mapping")
log(SEP)

# For each CMS-resolved plan, look up raw PlanType
cms_nonppo = cms_plans[cms_plans["network_type"] != "PPO"].copy()
log(f"  CMS-resolved PPO:     {(cms_plans['network_type']=='PPO').sum()}")
log(f"  CMS-resolved non-PPO: {len(cms_nonppo)}")
log()

# Count raw CMS PlanType for non-PPO resolved plans
raw_type_counts = defaultdict(int)
for _, row in cms_nonppo.iterrows():
    raw = str(row.get("matched_token","")).strip()
    raw_type_counts[raw] += 1

log("  Raw CMS PlanType → mapped network_type:")
log(f"  {'Raw CMS PlanType':<30} {'Mapped to':<15} {'Count':>6}")
log(f"  {'-'*30} {'-'*15} {'-'*6}")
type_mapping = {
    "PPO": "PPO",
    "HMO": "HMO",
    "EPO": "EPO",
    "POS": "POS",
    "Indemnity": "Indemnity",
    "HDHP": "HDHP",
}
for raw, count in sorted(raw_type_counts.items(), key=lambda x: -x[1]):
    mapped = type_mapping.get(raw, raw)
    log(f"  {raw:<30} {mapped:<15} {count:>6}")

# Also show CMS-resolved PPO raw types
cms_ppo = cms_plans[cms_plans["network_type"] == "PPO"]
ppo_raw_counts = defaultdict(int)
for _, row in cms_ppo.iterrows():
    ppo_raw_counts[str(row.get("matched_token","")).strip()] += 1
log()
log("  CMS-resolved PPO raw PlanType values:")
for raw, count in sorted(ppo_raw_counts.items(), key=lambda x: -x[1]):
    log(f"    {raw:<30} {count:>6}")

# ─────────────────────────────────────────────────────────────────────────────
log()
log(SEP)
log("ITEM 4: Group-plans-only headline and c2_summary.json update")
log(SEP)

group = merged[merged["plan_market_type"] == "group"]
g_total   = len(group)
g_ppo     = (group["network_type"] == "PPO").sum()
g_nonppo  = ((group["network_type"] != "PPO") & (group["network_type"] != "unknown")).sum()
g_unknown = (group["network_type"] == "unknown").sum()

log(f"  Group plans total  : {g_total}")
log(f"  Group PPO          : {g_ppo}")
log(f"  Group non-PPO      : {g_nonppo}")
log(f"  Group unknown      : {g_unknown}")
log(f"  Group resolved     : {g_ppo + g_nonppo}")
log(f"  Check: {g_ppo}+{g_nonppo}+{g_unknown} = {g_ppo+g_nonppo+g_unknown}")
if g_total != 2000 or g_ppo != 607 or g_nonppo != 857 or g_unknown != 536:
    log("  !! MISMATCH from expected 2000/607/857/536 — reporting actuals")
else:
    log("  ✓ All match expected numbers.")

# Update c2_summary.json
summary_path = Path("output/c2/c2_summary.json")
s = json.loads(summary_path.read_text())
s["group_plans_headline"] = {
    "total": int(g_total),
    "ppo": int(g_ppo),
    "non_ppo": int(g_nonppo),
    "unknown": int(g_unknown),
    "resolved": int(g_ppo + g_nonppo),
    "note": "Scope for deliverable: group plans only (CA 1312, AR 307, OR 381)",
}
s["individual_plans_note"] = (
    "1,214 individual plans classified for completeness; "
    "out of scope for PPO rate file deliverable per organiser guidance 2026-10-17. "
    "469 resolved by CMS join; 565 remain unknown (not_in_cms). No further work."
)
s["run_at_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
summary_path.write_text(json.dumps(s, indent=2))
log(f"\n  c2_summary.json updated: {summary_path}")

# ─────────────────────────────────────────────────────────────────────────────
log()
log(SEP)
log("ITEM 5a: All 27,565 C4 chosen keys by network_label + evidence_type")
log(SEP)

con = duckdb.connect()
con.execute("CREATE TABLE s7 AS SELECT * FROM 'output/c2b_steps5to8/c2b_step7_labelled.parquet'")
con.execute("CREATE TABLE c4 AS SELECT * FROM 'output/c4_v2/c4_chosen.parquet'")

# Step 7 labelled rows joined to c4 chosen (one row per chosen key)
r = con.execute("""
    SELECT s7.network_label, s7.evidence_type, s7.confidence,
           count(DISTINCT c4.tin||'|'||c4.npi||'|'||c4.billing_code_type||'|'||c4.billing_code||'|'||c4.addl_key) AS keys,
           count(DISTINCT c4.tin) AS tins,
           count(DISTINCT c4.npi) AS npis
    FROM c4
    JOIN s7 USING (tin, npi, billing_code_type, billing_code, addl_key)
    GROUP BY ALL
    ORDER BY keys DESC
""").fetchall()

log("  network_label   evidence_type            confidence  keys    TINs    NPIs")
log("  " + "-"*72)
for row in r:
    log(f"  {row[0]:<15} {row[1]:<25} {row[2]:<12} {row[3]:>6} {row[4]:>7} {row[5]:>7}")

# Total in join
total_keys_joined = sum(x[3] for x in r)
log(f"\n  Keys in join: {total_keys_joined}  (total c4 chosen: 27,565)")

# NPIs in c4 total
c4_npis = con.execute("SELECT count(DISTINCT npi) FROM c4").fetchone()[0]
c4_tins = con.execute("SELECT count(DISTINCT tin) FROM c4").fetchone()[0]
log(f"  C4 total: {c4_tins} TINs, {c4_npis} NPIs")

# NPIs in inclusive set (PPO + shared)
inclusive_npis = con.execute("""
    SELECT count(DISTINCT c4.npi) FROM c4
    JOIN s7 USING (tin, npi, billing_code_type, billing_code, addl_key)
    WHERE s7.network_label IN ('PPO','shared')
""").fetchone()[0]
inclusive_tins = con.execute("""
    SELECT count(DISTINCT c4.tin) FROM c4
    JOIN s7 USING (tin, npi, billing_code_type, billing_code, addl_key)
    WHERE s7.network_label IN ('PPO','shared')
""").fetchone()[0]
log(f"\n  Inclusive (PPO+shared): {inclusive_tins} TINs, {inclusive_npis} NPIs")

# NPIs outside inclusive set
outside_npis = con.execute("""
    SELECT count(DISTINCT c4.npi) FROM c4
    JOIN s7 USING (tin, npi, billing_code_type, billing_code, addl_key)
    WHERE s7.network_label NOT IN ('PPO','shared')
""").fetchone()[0]
outside_tins = con.execute("""
    SELECT count(DISTINCT c4.tin) FROM c4
    JOIN s7 USING (tin, npi, billing_code_type, billing_code, addl_key)
    WHERE s7.network_label NOT IN ('PPO','shared')
""").fetchone()[0]
log(f"  Outside inclusive (unknown+ambiguous): {outside_tins} TINs, {outside_npis} NPIs")
log()
log(f"  Explanation of ~{c4_npis - inclusive_npis} NPIs outside inclusive set:")
log("    'unknown' NPIs: single-entry keys with no cross-group price signal.")
log("      Their rate exists and is kept in C4; they are not PPO-confirmed.")
log("    'ambiguous' NPIs: parallel-contract entries resolved by C4 rules 2-5")
log("      (modifier/POS/majority/lowest-rate); network label is indeterminate.")
log("    Both groups appear in the FULL rate file for 'all plans' but cannot be")
log("    attributed to the PPO network specifically without additional evidence.")

# ─────────────────────────────────────────────────────────────────────────────
log()
log(SEP)
log("ITEM 5b: Strict PPO keys (524) — behavioral-health share and top-20 business_names")
log(SEP)

# Behavioral health codes: 90832-90838
bh_codes = [str(c) for c in range(90832, 90839)]  # 90832,90833,...,90838
bh_list = "'" + "','".join(bh_codes) + "'"

bh = con.execute(f"""
    SELECT count(*) AS bh_keys,
           count(DISTINCT tin) AS bh_tins,
           count(DISTINCT npi) AS bh_npis
    FROM s7
    WHERE network_label = 'PPO' AND include_in_step7
      AND billing_code IN ({bh_list})
""").fetchone()

total_ppo = con.execute("""
    SELECT count(*) FROM s7 WHERE network_label='PPO' AND include_in_step7
""").fetchone()[0]

log(f"  Strict PPO total rows (include_in_step7=true): {total_ppo}")
log(f"  Behavioral health (90832-90838) rows: {bh[0]} ({100*bh[0]//max(total_ppo,1)}%)")
log(f"  BH TINs: {bh[1]}   BH NPIs: {bh[2]}")
log()

# Top-20 business_names in strict PPO
# Check column names first
cols = [c[0] for c in con.execute("DESCRIBE s7").fetchall()]
bname_col = next((c for c in cols if "business" in c.lower()), None)
log(f"  business_name column: {bname_col!r}")
log()
if bname_col:
    top20 = con.execute(f"""
        SELECT "{bname_col}", count(DISTINCT tin) AS tins,
               count(DISTINCT npi) AS npis, count(*) AS rows
        FROM s7
        WHERE network_label = 'PPO' AND include_in_step7
        GROUP BY "{bname_col}"
        ORDER BY tins DESC
        LIMIT 20
    """).fetchall()
    log(f"  Top-20 business_name values in strict-PPO rows (by TIN count):")
    log(f"  {'business_name':<50} {'TINs':>5} {'NPIs':>5} {'rows':>6}")
    log("  " + "-"*68)
    for row in top20:
        name = str(row[0])[:50] if row[0] else "(null)"
        log(f"  {name:<50} {row[1]:>5} {row[2]:>5} {row[3]:>6}")
else:
    log("  No business_name column found in s7. Columns:", cols[:10])

log()
log("DONE. All 5 items complete.")
