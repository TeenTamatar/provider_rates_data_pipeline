"""
C2 merge: apply CMS Plan Attributes join to all 1,570 unknowns,
produce final c2_plans_network.csv with evidence columns,
write summary JSON, update LOGIC.md counts.

No network calls. Uses data/plan_attributes_py2026.csv (already on disk).
"""
import argparse, csv, json, sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
import pandas as pd

ap = argparse.ArgumentParser()
ap.add_argument("--all-plans",   default="output/c2a_network/c2a_plans.csv")
ap.add_argument("--unresolved",  default="output/c2a_network/c2a_unresolved.csv")
ap.add_argument("--cms-file",    default="data/plan_attributes_py2026.csv")
ap.add_argument("--out-dir",     default="output/c2")
args = ap.parse_args()

out_dir = Path(args.out_dir)
out_dir.mkdir(parents=True, exist_ok=True)
SEP = "─" * 70

def log(m): print(m, flush=True)

# ── Load CMS file ─────────────────────────────────────────────────────────────
log(SEP)
log("Loading CMS Plan Attributes PY2026 ...")
cms_rows = []
with open(args.cms_file, newline="", encoding="utf-8-sig") as f:
    for row in csv.DictReader(f):
        cms_rows.append(row)
log(f"  {len(cms_rows)} CMS rows loaded.")

# Build product-level index (year=2026 only)
cms_by_prod: dict[str, defaultdict[str,int]] = defaultdict(lambda: defaultdict(int))
cms_sbc: dict[str, str] = {}
for r in cms_rows:
    if r.get("BusinessYear","").strip() != "2026": continue
    prod = r.get("HIOSProductId","").strip()
    if not prod: continue
    ptype = r.get("PlanType","").strip()
    cms_by_prod[prod][ptype] += 1
    sbc = r.get("URLForSummaryofBenefitsCoverage","").strip()
    if sbc: cms_sbc[prod] = sbc

log(f"  {len(cms_by_prod)} distinct HIOSProductId entries for 2026.")

# ── Load all plans ─────────────────────────────────────────────────────────────
all_plans = pd.read_csv(args.all_plans)
log(f"\n  All plans: {len(all_plans)} rows")

# Verify starting totals
total     = len(all_plans)
ppo_c2a   = (all_plans["network_type"] == "PPO").sum()
nonppo_c2a = ((all_plans["network_type"] != "PPO") & (all_plans["network_type"] != "unknown")).sum()
unk_c2a   = (all_plans["network_type"] == "unknown").sum()
log(f"  Before merge: PPO={ppo_c2a}, non-PPO={nonppo_c2a}, unknown={unk_c2a}, total={total}")
if total != 3214:
    log(f"  !! STOP: expected 3,214 plans, got {total}")
    sys.exit(1)

# ── Apply CMS evidence to unknowns ─────────────────────────────────────────────
log(SEP)
log("Applying CMS join to unknown plans ...")

# Normalise PlanType
def normalise_net(t):
    t = t.strip().upper()
    if t in ("PPO","PREFERRED PROVIDER ORGANIZATION"): return "PPO", True
    if t in ("HMO","HEALTH MAINTENANCE ORGANIZATION"): return "HMO", False
    if t in ("EPO","EXCLUSIVE PROVIDER ORGANIZATION"): return "EPO", False
    if t in ("POS","POINT OF SERVICE"):               return "POS", False
    return t, None

def cms_lookup(hpid):
    """Return (network_type, is_ppo, quote, sbc_url, reason) or None."""
    if pd.isna(hpid) or str(hpid).strip() == "":
        return None, "no_hios_product_id"
    prod = str(hpid).strip()
    if prod not in cms_by_prod:
        return None, "not_in_cms"
    types = cms_by_prod[prod]
    distinct = set(t for t in types if t)
    if len(distinct) != 1:
        return None, "ambiguous_cms"
    raw = list(distinct)[0]
    net, is_ppo = normalise_net(raw)
    return {"network_type": net, "is_ppo": "" if is_ppo is None else str(is_ppo).lower(),
            "matched_token": raw,
            "evidence_type": "cms_plan_attributes",
            "source_url": f"cms_plan_attributes:HIOSProductId={prod}",
            "quote": f"PlanType={raw}",
            "confidence": "high",
            "sbc_url": cms_sbc.get(prod, "")}, "matched"

# Add new columns
new_cols = ["evidence_type","source_url","quote","confidence","sbc_url","cms_reason"]
for col in new_cols:
    if col not in all_plans.columns:
        all_plans[col] = ""

n_resolved = 0
n_group_no_doc = 0
n_no_hpid = 0
n_not_in_cms = 0
n_ambiguous_cms = 0

for idx, row in all_plans.iterrows():
    if row["network_type"] != "unknown":
        # Already labelled by C2a — preserve, no CMS needed
        if all_plans.at[idx, "evidence_type"] == "":
            all_plans.at[idx, "evidence_type"] = "name_keyword"
            all_plans.at[idx, "confidence"] = "high"
            all_plans.at[idx, "quote"] = f"plan_name contains '{row.get('matched_token','')}'"
        continue

    # Unknown — try CMS
    market = str(row.get("plan_market_type","")).strip()
    brand  = str(row.get("brand","")).strip()

    # Group plans (AR/OR) have no public CMS document
    if market == "group":
        all_plans.at[idx, "evidence_type"] = "unknown"
        all_plans.at[idx, "cms_reason"] = "no_public_document"
        all_plans.at[idx, "confidence"] = ""
        n_group_no_doc += 1
        continue

    result, reason = cms_lookup(row.get("hios_product_id"))
    if result is None:
        all_plans.at[idx, "evidence_type"] = "unknown"
        all_plans.at[idx, "cms_reason"] = reason
        all_plans.at[idx, "confidence"] = ""
        if reason == "no_hios_product_id": n_no_hpid += 1
        elif reason == "not_in_cms": n_not_in_cms += 1
        else: n_ambiguous_cms += 1
    else:
        all_plans.at[idx, "network_type"]  = result["network_type"]
        all_plans.at[idx, "is_ppo"]        = result["is_ppo"]
        all_plans.at[idx, "matched_token"] = result["matched_token"]
        all_plans.at[idx, "evidence_type"] = result["evidence_type"]
        all_plans.at[idx, "source_url"]    = result["source_url"]
        all_plans.at[idx, "quote"]         = result["quote"]
        all_plans.at[idx, "confidence"]    = result["confidence"]
        all_plans.at[idx, "sbc_url"]       = result["sbc_url"]
        all_plans.at[idx, "cms_reason"]    = "matched"
        n_resolved += 1

log(f"  Resolved via CMS:          {n_resolved}")
log(f"  Group (no_public_document):{n_group_no_doc}")
log(f"  No hios_product_id:        {n_no_hpid}")
log(f"  Not in CMS:                {n_not_in_cms}")
log(f"  Ambiguous CMS:             {n_ambiguous_cms}")

# ── Final counts ─────────────────────────────────────────────────────────────
log(SEP)
log("FINAL COUNTS")
log(SEP)

final_ppo    = (all_plans["network_type"] == "PPO").sum()
final_nonppo = ((all_plans["network_type"] != "PPO") & (all_plans["network_type"] != "unknown")).sum()
final_unk    = (all_plans["network_type"] == "unknown").sum()
final_total  = len(all_plans)

log(f"  PPO:     {final_ppo}")
log(f"  non-PPO: {final_nonppo}")
log(f"  unknown: {final_unk}")
log(f"  TOTAL:   {final_total}  (must be 3,214)")
if final_total != 3214:
    log("  !! STOP: total mismatch"); sys.exit(1)
if final_ppo + final_nonppo + final_unk != 3214:
    log("  !! STOP: ppo+nonppo+unknown != 3214"); sys.exit(1)
log("  ✓ Totals check out.")

log("\n  By state and market:")
pivot = all_plans.groupby(["state","plan_market_type","network_type"]).size().unstack(fill_value=0)
log(pivot.to_string())

log("\n  Unknown reasons:")
unk_df = all_plans[all_plans["network_type"] == "unknown"]
log(unk_df["cms_reason"].value_counts().to_string())

# ── Write c2_plans_network.csv ─────────────────────────────────────────────────
out_file = out_dir / "c2_plans_network.csv"
# Guard against silent overwrite
if out_file.exists():
    bak = out_dir / f"c2_plans_network_bak_{datetime.now().strftime('%H%M%S')}.csv"
    out_file.rename(bak)
    log(f"\n  Existing file backed up to {bak}")

all_plans.to_csv(out_file, index=False)
log(f"  Written: {out_file} ({out_file.stat().st_size:,} bytes, {len(all_plans)} rows)")

# ── Summary JSON ─────────────────────────────────────────────────────────────
summary = {
    "run_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "total_plans": int(final_total),
    "ppo": int(final_ppo),
    "non_ppo": int(final_nonppo),
    "unknown": int(final_unk),
    "resolved_via_cms": int(n_resolved),
    "group_no_public_document": int(n_group_no_doc),
    "no_hios_product_id": int(n_no_hpid),
    "not_in_cms": int(n_not_in_cms),
    "ambiguous_cms": int(n_ambiguous_cms),
    "ca_group_ppo_unchanged": int((all_plans[(all_plans["state"]=="CA") &
                                             (all_plans["plan_market_type"]=="group") &
                                             (all_plans["network_type"]=="PPO")].shape[0])),
    "cms_file": "data/plan_attributes_py2026.csv",
    "cms_rows": len(cms_rows),
    "cms_products_2026": len(cms_by_prod),
    "evidence_breakdown": all_plans.groupby("evidence_type").size().to_dict(),
    "by_state_market_network": (
        all_plans.groupby(["state","plan_market_type","network_type"])
        .size().reset_index(name="n")
        .to_dict(orient="records")
    ),
    "unknown_reasons": unk_df["cms_reason"].value_counts().to_dict(),
}
summary_path = out_dir / "c2_summary.json"
summary_path.write_text(json.dumps(summary, indent=2))
log(f"  Summary: {summary_path}")

# ── Sanity: CA group PPO = 456 ─────────────────────────────────────────────────
ca_ppo = summary["ca_group_ppo_unchanged"]
log(f"\n  CA group PPO (must be 456): {ca_ppo}")
if ca_ppo != 456:
    log("  !! STOP: CA PPO changed — must investigate"); sys.exit(1)
log("  ✓ CA group PPO unchanged.")
