"""
CMS Plan Attributes PUF PY2026 — download, manifest, and analysis.

Steps performed:
  0. Download to data/plan_attributes_py2026.csv (one network call, then cached).
  1. Print all 151 column names. Report distinct MarketCoverage and BusinessYear.
  2. Reconcile hios_product_id coverage vs brand/market breakdown.
  3. Check ID format alignment (hios_product_id vs CMS HIOSProductId / StandardComponentId).
  4. Join at product level; accept plan type only if all CMS rows for that product+year agree.
  5. Validate: for plans already labelled by C2a, compare with CMS PlanType.
  6. Print 5-plan pilot (matched individual plans). STOP.

Rules: evidence or unknown; no guessing; no additional network calls beyond the one download.
"""
import argparse
import csv
import hashlib
import json
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# ── args ──────────────────────────────────────────────────────────────────────
ap = argparse.ArgumentParser()
ap.add_argument("--unresolved",  default="output/c2a_network/c2a_unresolved.csv")
ap.add_argument("--all-plans",   default="output/c2a_network/c2a_plans.csv")
ap.add_argument("--out-dir",     default="data")
ap.add_argument("--cms-url",     default="https://data.healthcare.gov/datafile/py2026/plan_attributes_PUF.csv")
ap.add_argument("--cms-file",    default="data/plan_attributes_py2026.csv")
ap.add_argument("--manifest",    default="data/MANIFEST.json")
args = ap.parse_args()

out_dir = Path(args.out_dir)
out_dir.mkdir(parents=True, exist_ok=True)
cms_path = Path(args.cms_file)

def log(msg): print(msg, flush=True)
SEP = "─" * 70

# ── Step 0: Download (or use cached) ──────────────────────────────────────────
log(SEP)
log("STEP 0: Download CMS Plan Attributes PUF PY2026")
log(SEP)

manifest_path = Path(args.manifest)

if cms_path.exists():
    log(f"  Already on disk: {cms_path} ({cms_path.stat().st_size:,} bytes) — skipping download.")
    sha256 = hashlib.sha256(cms_path.read_bytes()).hexdigest()
    download_date = "previously downloaded"
    size_bytes = cms_path.stat().st_size
else:
    log(f"  Downloading {args.cms_url} ...")
    req = urllib.request.Request(
        args.cms_url,
        headers={"User-Agent": "hilabs-pipeline/1.0 (price-transparency-research)"}
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()
    cms_path.write_bytes(data)
    size_bytes = len(data)
    sha256 = hashlib.sha256(data).hexdigest()
    download_date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    log(f"  Saved {size_bytes:,} bytes ({size_bytes/1024/1024:.1f} MB)")

manifest = {
    "file": str(cms_path),
    "source_url": args.cms_url,
    "download_date_utc": download_date,
    "size_bytes": size_bytes,
    "sha256": sha256,
}
manifest_path.write_text(json.dumps(manifest, indent=2))
log(f"  SHA-256: {sha256}")
log(f"  Manifest written to {manifest_path}")

# ── Load CMS file ──────────────────────────────────────────────────────────────
log("")
log(SEP)
log("STEP 1: Column names and market coverage")
log(SEP)

cms_rows = []
with open(cms_path, newline="", encoding="utf-8-sig") as f:
    reader = csv.DictReader(f)
    fieldnames = reader.fieldnames or []
    for row in reader:
        cms_rows.append(row)

log(f"  Total CMS rows: {len(cms_rows)}")
log(f"  Total columns:  {len(fieldnames)}")
log("")
log("  All 151 column names:")
for i, col in enumerate(fieldnames, 1):
    log(f"    {i:>3}. {col}")

# Distinct MarketCoverage values
market_col = next((c for c in fieldnames if "market" in c.lower() and "coverage" in c.lower()), None)
year_col = next((c for c in fieldnames if "year" in c.lower() and "business" in c.lower()), None)
log("")
if market_col:
    markets = defaultdict(int)
    for r in cms_rows:
        markets[r.get(market_col,"")]+= 1
    log(f"  Distinct values of '{market_col}':")
    for k, v in sorted(markets.items(), key=lambda x: -x[1]):
        log(f"    {k!r:30s}: {v:>6} rows")
if year_col:
    years = defaultdict(int)
    for r in cms_rows:
        years[r.get(year_col,"")]+= 1
    log(f"  Distinct values of '{year_col}':")
    for k, v in sorted(years.items()):
        log(f"    {k!r}: {v:>6} rows")

# ── Step 2: Reconcile plan ID coverage ────────────────────────────────────────
log("")
log(SEP)
log("STEP 2: hios_product_id coverage reconciliation")
log(SEP)

import pandas as pd
unresolved = pd.read_csv(args.unresolved)
all_plans  = pd.read_csv(args.all_plans)

log(f"  Total unknown plans: {len(unresolved)}")
log("")
log("  Breakdown by brand x market x has_hios_product_id:")
unresolved["has_hpid"] = unresolved["hios_product_id"].notna() & \
                         (unresolved["hios_product_id"].astype(str).str.strip() != "")
tbl = unresolved.groupby(["brand","plan_market_type","has_hpid"]).size().reset_index(name="n")
for _, row in tbl.iterrows():
    log(f"    {row['brand']:<15} {row['plan_market_type']:<12} has_hpid={str(row['has_hpid']):<6} n={row['n']}")

log("")
log("  Summary:")
log(f"    With hios_product_id:    {unresolved['has_hpid'].sum()}")
log(f"    Without hios_product_id: {(~unresolved['has_hpid']).sum()}")
log(f"    Total:                   {len(unresolved)}")
log(f"    Group unknowns (AR+OR):  {len(unresolved[unresolved['plan_market_type']=='group'])}")
log(f"    Individual unknowns:     {len(unresolved[unresolved['plan_market_type']=='individual'])}")

# ── Step 3: ID format check ────────────────────────────────────────────────────
log("")
log(SEP)
log("STEP 3: ID format alignment check (5 sample rows)")
log(SEP)

# CMS columns for plan ID
std_comp_col = next((c for c in fieldnames if "standardcomponent" in c.lower()), None)
hios_prod_col = next((c for c in fieldnames if "hiosproduct" in c.lower()), None)
plan_type_col = next((c for c in fieldnames if c == "PlanType"), None)
sbc_url_col   = next((c for c in fieldnames if "summaryofbenefits" in c.lower()), None)

log(f"  CMS StandardComponentId column: {std_comp_col!r}")
log(f"  CMS HIOSProductId column:       {hios_prod_col!r}")
log(f"  CMS PlanType column:            {plan_type_col!r}")
log(f"  CMS SBC URL column:             {sbc_url_col!r}")
log("")

# Sample our hios_product_id values
our_hpid_samples = unresolved[unresolved["has_hpid"]]["hios_product_id"].dropna().unique()[:5]
log("  Our hios_product_id samples (from c2a_unresolved.csv):")
for pid in our_hpid_samples:
    log(f"    {pid!r:30s}  len={len(str(pid))}")

log("")
log("  CMS StandardComponentId samples (first 5 distinct):")
seen = set()
count = 0
for r in cms_rows:
    v = r.get(std_comp_col,"")
    if v and v not in seen:
        seen.add(v)
        log(f"    {v!r:30s}  len={len(v)}")
        count += 1
        if count >= 5: break

log("")
log("  CMS HIOSProductId samples (first 5 distinct):")
seen2 = set()
count2 = 0
for r in cms_rows:
    v = r.get(hios_prod_col,"")
    if v and v not in seen2:
        seen2.add(v)
        log(f"    {v!r:30s}  len={len(v)}")
        count2 += 1
        if count2 >= 5: break

# Detect format of our hios_product_id
our_pid_len = len(str(our_hpid_samples[0]).strip()) if len(our_hpid_samples) > 0 else 0
log(f"\n  Our hios_product_id length: {our_pid_len} chars")

# ── Step 4: Join ──────────────────────────────────────────────────────────────
log("")
log(SEP)
log("STEP 4: Join at product level")
log(SEP)

# Build CMS lookup: HIOSProductId -> {PlanType: count}
# Accept PlanType only if all rows for that product+year agree
cms_by_prod: dict[str, defaultdict[str,int]] = defaultdict(lambda: defaultdict(int))
cms_by_prod_sbc: dict[str, str] = {}

BUSI_YEAR = "2026"
for r in cms_rows:
    if year_col and r.get(year_col,"").strip() != BUSI_YEAR:
        continue
    prod = r.get(hios_prod_col,"").strip()
    if not prod:
        continue
    ptype = r.get(plan_type_col,"").strip()
    cms_by_prod[prod][ptype] += 1
    # Store SBC URL if available
    if sbc_url_col and r.get(sbc_url_col,"").strip():
        cms_by_prod_sbc[prod] = r.get(sbc_url_col,"").strip()

log(f"  CMS products indexed for year {BUSI_YEAR}: {len(cms_by_prod)}")

# Join results per plan
matched = []
ambiguous = []
no_match = []

for _, row in unresolved.iterrows():
    if not row["has_hpid"]:
        no_match.append({"brand": row["brand"], "market": row["plan_market_type"],
                         "reason": "no_hios_product_id"})
        continue
    prod_id = str(row["hios_product_id"]).strip()
    if prod_id not in cms_by_prod:
        no_match.append({"brand": row["brand"], "market": row["plan_market_type"],
                         "reason": "not_in_cms"})
        continue
    types = cms_by_prod[prod_id]
    distinct_types = set(t for t in types if t)
    if len(distinct_types) == 1:
        plan_type = list(distinct_types)[0]
        matched.append({
            "plan_id": row.get("plan_id",""),
            "plan_name": row.get("plan_name",""),
            "brand": row["brand"],
            "market": row["plan_market_type"],
            "state": row.get("state",""),
            "hios_product_id": prod_id,
            "cms_plan_type": plan_type,
            "cms_row_count": sum(types.values()),
            "sbc_url": cms_by_prod_sbc.get(prod_id,""),
            "evidence_type": "cms_plan_attributes",
            "confidence": "high",
        })
    else:
        ambiguous.append({"brand": row["brand"], "market": row["plan_market_type"],
                          "types": list(distinct_types), "prod_id": prod_id})

log(f"\n  Join results:")
log(f"    Matched (all CMS rows agree):  {len(matched)}")
log(f"    Ambiguous (CMS rows disagree): {len(ambiguous)}")
log(f"    No match:                      {len(no_match)}")
log(f"    Total:                         {len(matched)+len(ambiguous)+len(no_match)}")

# By brand
from collections import Counter
log("\n  Coverage by brand (unknown plans):")
brands = unresolved["brand"].unique()
for brand in sorted(brands):
    m = sum(1 for x in matched if x["brand"]==brand)
    a = sum(1 for x in ambiguous if x["brand"]==brand)
    nm = sum(1 for x in no_match if x["brand"]==brand)
    tot = m+a+nm
    log(f"    {brand:<15} total={tot:>4}  matched={m:>4}  ambiguous={a:>4}  no_match={nm:>4}")

if ambiguous:
    log("\n  Ambiguous cases:")
    for x in ambiguous[:10]:
        log(f"    {x['brand']}/{x['market']} prod={x['prod_id']} types={x['types']}")

# ── Step 5: Validation — C2a labelled vs CMS ──────────────────────────────────
log("")
log(SEP)
log("STEP 5: Validate C2a name-based labels against CMS PlanType")
log(SEP)

# Plans already labelled by C2a (not unknown)
labelled = all_plans[all_plans["network_type"] != "unknown"].copy()
labelled["has_hpid"] = labelled["hios_product_id"].notna() & \
                       (labelled["hios_product_id"].astype(str).str.strip() != "")

match_count = mismatch_count = no_cms = 0
mismatches = []

for _, row in labelled.iterrows():
    if not row["has_hpid"]:
        no_cms += 1
        continue
    prod_id = str(row["hios_product_id"]).strip()
    if prod_id not in cms_by_prod:
        no_cms += 1
        continue
    types = cms_by_prod[prod_id]
    distinct_types = set(t for t in types if t)
    if len(distinct_types) != 1:
        no_cms += 1
        continue
    cms_type = list(distinct_types)[0]
    our_type = str(row["network_type"]).upper()
    # Normalise: HMO+SAL -> HMO, EOA -> HMO, SAL -> HMO for comparison
    def normalise(t):
        t = t.upper()
        if "HMO" in t: return "HMO"
        if t in ("EOA",): return "HMO"
        return t
    if normalise(our_type) == normalise(cms_type):
        match_count += 1
    else:
        mismatch_count += 1
        mismatches.append({
            "plan_name": row.get("plan_name",""),
            "brand": row["brand"],
            "state": row.get("state",""),
            "c2a_type": row["network_type"],
            "cms_type": cms_type,
        })

log(f"  C2a labelled plans: {len(labelled)}")
log(f"    No CMS record (no hios_product_id or not in CMS): {no_cms}")
log(f"    CMS match:    {match_count}")
log(f"    CMS mismatch: {mismatch_count}")
if mismatches:
    log("\n  Mismatches:")
    for m in mismatches:
        log(f"    {m['brand']}/{m['state']} c2a={m['c2a_type']:<12} cms={m['cms_type']:<10} plan={m['plan_name'][:60]}")
else:
    log("  No mismatches found.")

# ── Step 6: Pilot — 5 matched individual plans ────────────────────────────────
log("")
log(SEP)
log("STEP 6: Pilot — 5 matched individual plans (Ambetter/Fidelis)")
log(SEP)

individual_matched = [x for x in matched if x["market"] == "individual"]
log(f"  Total individual matched plans: {len(individual_matched)}")
log("")
log("  PILOT — first 5:")
for i, p in enumerate(individual_matched[:5], 1):
    log(f"\n  Plan {i}:")
    log(f"    plan_id          : {p['plan_id']}")
    log(f"    plan_name        : {p['plan_name']}")
    log(f"    brand            : {p['brand']}")
    log(f"    state            : {p['state']}")
    log(f"    hios_product_id  : {p['hios_product_id']}")
    log(f"    CMS join key     : HIOSProductId={p['hios_product_id']!r}")
    log(f"    cms_plan_type    : {p['cms_plan_type']}")
    log(f"    sbc_url          : {p['sbc_url'] or '(none)'}")
    log(f"    evidence_type    : {p['evidence_type']}")
    log(f"    confidence       : {p['confidence']}")
    log(f"    cms_rows_agreeing: {p['cms_row_count']}")

log("")
log("STOPPED for review. Awaiting approval before running all 1,570 plans.")
