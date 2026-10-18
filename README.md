# Provider Rates Data Pipeline — Network Classification

**Branch:** `agent-network-pipeline`  
**Repo:** [TeenTamatar/provider_rates_data_pipeline](https://github.com/TeenTamatar/provider_rates_data_pipeline)  
**Scope:** Centene Health Net California group plans — plan-level PPO identification + provider-level rate labelling

---

## Why this branch exists

CMS price-transparency rules require payers to publish machine-readable in-network rate files.
The challenge: those files do not label which rates belong to which network (PPO, HMO, EPO).
A provider rate file for a PPO plan is only useful if you know it **is** a PPO plan.

This branch solves that problem end-to-end for Centene's Health Net California group book:

1. **Parse** four Centene index files → discover 3,214 plans across 4 brands and 32 states
2. **Classify** each plan's network type using two independent evidence sources
3. **Label** every price row in the Health Net CA rate file by evidence tier
4. **Select** one authoritative rate per billing-code key

The output feeds downstream steps: address enrichment (C5) and the final provider-rate file (C6).

---

## Repository layout

```
c1/
  c1_parse_index.py          Parse index JSON → plan list + rate file URLs

c2/
  c2a_classify.py            Name-keyword classifier (pass 1)
  c2_network_evidence.py     SBC-document fetcher with robots.txt + cache
  cms_plan_attributes.py     CMS Plan Attributes PUF downloader + joiner
  c2_merge_network.py        Merge all evidence → final plan classification
  c2_closeout.py             Audit: agreement check, pilot display, group headline
  analyse_hios_ids.py        HIOS plan ID structure inspector (diagnostic)

c2a/
  c2a_classify.py            Rate-file HEAD-checker (separate from c2/c2a_classify.py)

c2b/
  c2b_agent.py               Stream 701 MB Health Net CA rate file → kept prices
  c2b_steps5to8.py           Label prices: parallel-contract test, resolve, tier
  c2b_network_analysis.py    Deep-dive: rate spreads, tier coverage, BH share
  c2b_disjoint_tiers.py      Disjoint TIN/NPI counts per evidence tier

.gitignore                   Excludes output/, data/, *.parquet, *.json.gz, .venv/
```

---

## How it works — step by step

### Step 1 — Parse index files (`c1/c1_parse_index.py`)

Centene publishes one JSON index per brand listing all their plans and the URL of each plan's
rate file. This script streams all four indexes (Ambetter, Fidelis, Health Net, QualChoice)
using `ijson` to avoid loading multi-hundred-MB files into memory.

**Extracts per plan:** plan name · plan ID · plan ID type · market type (group/individual) ·
state · rate file URL

**Key rules:**
- State inferred from rate-file URL suffix first, then HIOS state code, then name prefix
- Deduplication key: `(brand, plan_name_norm, plan_id, plan_id_type, market, rate_file_url)`
- QualChoice rate file returns HTTP 404 on all 80+ URL probes → recorded as unavailable

**Output:** `output/c1/c1_plans.csv` — 3,214 plans · 33 rate file URLs

```bash
python c1/c1_parse_index.py \
    --index <path-or-URL> <path-or-URL> <path-or-URL> <path-or-URL> \
    --out output/c1
```

---

### Step 2 — Name-keyword classification (`c2/c2a_classify.py`)

Scans each plan's name for network-type tokens. Rules are strict to avoid false positives:

| Token | Rule | Maps to |
|---|---|---|
| `PPO` | Anywhere in name (unless followed by a letter) | PPO |
| `HMO`, `EPO`, `POS` | Must be standalone (no adjacent letter) | Respective type |
| `EOA` | Health Net Elect Open Access — cited in code | HMO-based |
| `SAL` | Health Net Salud HMO y Más — cited in code | HMO-based |
| PPO + EPO | Together → PPO (Health Net CA confirmed pattern) | PPO |
| PPO + HMO | Conflict — never guess | unknown |
| No token | — | unknown |

**Output:** `output/c2a_network/c2a_plans.csv` (3,214 rows) + `c2a_unresolved.csv` (1,570 rows)

After this step: **720 PPO · 924 non-PPO · 1,570 unknown**

```bash
python c2/c2a_classify.py \
    --plans output/c1/c1_plans.csv \
    --out-dir output/c2a_network
```

---

### Step 3 — CMS Plan Attributes lookup (`c2/cms_plan_attributes.py`)

The 1,570 unknowns are mostly Ambetter individual plans whose names are metal-level only
(Bronze, Silver, Gold). CMS publishes a Plan Attributes Public Use File that includes a
`PlanType` column (PPO/HMO/EPO/POS) for every plan on the federal marketplace.

**Join logic:**
- Download `plan_attributes_PUF.csv` (30.6 MB) from `data.healthcare.gov` — **one network call**,
  then cached. SHA-256 manifest written to `data/MANIFEST.json`.
- Join our `hios_product_id` (10-char) → CMS `HIOSProductId` (10-char), year filter = 2026
- Accept `PlanType` **only if all CMS rows for that product agree** — any disagreement → unknown
- Group plans (AR/OR) → `unknown / no_public_document` (CMS PUF covers individual marketplace only)

**Result:** 469 additional plans resolved (all Ambetter individual)

```bash
python c2/cms_plan_attributes.py \
    --unresolved output/c2a_network/c2a_unresolved.csv \
    --all-plans  output/c2a_network/c2a_plans.csv \
    --cms-file   data/plan_attributes_py2026.csv \
    --manifest   data/MANIFEST.json
```

---

### Step 4 — Merge into final plan classification (`c2/c2_merge_network.py`)

Combines C2a name labels + CMS join results into one authoritative file with evidence columns:

| Column | Description |
|---|---|
| `network_type` | PPO / HMO / EPO / POS / EOA / SAL / HMO+SAL / unknown |
| `evidence_type` | `name_keyword` · `cms_plan_attributes` · `unknown` |
| `quote` | Exact text that triggered the label (e.g. `PlanType=PPO`) |
| `confidence` | `high` / `medium` / `low` / `""` |
| `sbc_url` | SBC PDF URL from CMS file (for future document fetch) |
| `cms_reason` | `matched` · `not_in_cms` · `no_public_document` |

**Safety guards:** exits non-zero if total ≠ 3,214 or CA group PPO ≠ 456.
Backs up any existing output file before overwriting.

**Final counts (verified 2026-10-18):**

| Scope | PPO | non-PPO | unknown | Total |
|---|---|---|---|---|
| All plans | 792 | 1,321 | 1,101 | 3,214 |
| **Group only (deliverable)** | **607** | **857** | **536** | **2,000** |

CA group PPO = 456 (unchanged from name-only pass).

```bash
python c2/c2_merge_network.py \
    --all-plans  output/c2a_network/c2a_plans.csv \
    --unresolved output/c2a_network/c2a_unresolved.csv \
    --cms-file   data/plan_attributes_py2026.csv \
    --out-dir    output/c2
```

---

### Step 5a — Stream Health Net CA rate file (`c2b/c2b_agent.py`)

Health Net CA publishes a 701 MB in-network JSON containing ~1.5 M price rows. This script
streams it with `ijson` and runs four internal stages:

1. **Parse** provider groups → TIN + NPIs + `business_name` + `entry_label` (ppo/direct_ffs/none)
2. **Parse** prices → billing code × modifier × POS × provider_group_ids × negotiated rate
3. **Normalize** — strip units, canonicalize billing code type
4. **Dedup** within each provider group — keep the lowest price when the same group has multiple
   rows for the same code+modifier+POS

**Output:** `c2b_step4_kept_prices.parquet` — **50,933 rows · 2,856 TINs · 3,694 NPIs**

```bash
python c2b/c2b_agent.py \
    --rate-file <Health-Net-CA-URL> \
    --out-dir output/c2b \
    --cache-dir output/c1/cache
```

---

### Step 5b — Label prices by evidence tier (`c2b/c2b_steps5to8.py`)

Processes the 50,933 kept prices through four stages:

**Step 5 — Parallel-contract test**
For each key `(TIN × NPI × billing_code × modifier × POS)`, checks if multiple
`provider_group_id` values share this key with the same or different rates:
- `single` — one group only
- `identical_copies` — multiple groups, same rate
- `differing_copies` — multiple groups, different rates (parallel contract conflict)

**Step 6 — Resolve conflicts**
Differing-copy keys are resolved:
- Rule 6a: if exactly one group is PPO-labelled → take that rate (fires 0 times)
- Rule 6b: multiple PPO rates → `ambiguous_multiple_ppo_rates`
- Rule 6c: no PPO-labelled group → `ambiguous_no_ppo_group`

**Step 7 — Assign network_label + evidence_type + confidence**

| network_label | evidence_type | confidence | How assigned |
|---|---|---|---|
| PPO | `direct_ffs` | high | Key appears in a group with `entry_label=direct_ffs` |
| PPO | `business_name_ppo` | high | Group `business_name` contains "PPO" |
| shared | `shared_contract` | medium | Identical rate across ≥2 groups (assumed on PPO contract) |
| ambiguous | `ambiguous` | low | Differing-copy conflict, no PPO group to distinguish |
| unknown | `single` | low | Single-group entry, no cross-group signal |

> **"shared" assumption:** identical rates across provider groups are assumed to appear in the
> PPO contract because group-plan pricing is typically portfolio-wide. This is an assumption —
> a shared rate could also be coincidental or part of a non-PPO contract. See `LOGIC.md`.

**Step 8 — TIN confidence score**
For each TIN, computes what % of its kept keys are high/medium confidence.
TINs with ≥80% → "confident PPO participant".

**Disjoint tier counts (each TIN at its highest tier, 2026-10-18):**

| Tier | TINs | NPIs | Keys |
|---|---|---|---|
| PPO | **365** | 400 | 525 |
| shared | 283 | 406 | ~31,205 rows / 10,922 keys |
| ambiguous | 204 | 319 | 1,382 |
| unknown | 2,004 | 2,569 | 14,737 |
| **Total** | **2,856** | **3,694** | **27,565** |

360 of 365 PPO-tier TINs are "pure PPO" (only PPO keys, no mixing).

```bash
python c2b/c2b_steps5to8.py \
    --kept    c2b_step4_kept_prices.parquet \
    --out-dir output/c2b_steps5to8
```

---

## Full clean-room run

```bash
# 0. Install dependencies
pip install ijson pandas pyarrow duckdb pypdf requests

# 1. Parse index files (pass URLs or local paths)
python c1/c1_parse_index.py \
    --index <ambetter-index-url> <fidelis-url> <qualchoice-url> <healthnet-url> \
    --out output/c1

# 2. Name-keyword classification
python c2/c2a_classify.py \
    --plans output/c1/c1_plans.csv --out-dir output/c2a_network

# 3. CMS Plan Attributes download + join (one network call)
python c2/cms_plan_attributes.py \
    --unresolved output/c2a_network/c2a_unresolved.csv \
    --all-plans  output/c2a_network/c2a_plans.csv \
    --cms-file   data/plan_attributes_py2026.csv \
    --manifest   data/MANIFEST.json

# 4. Merge into final plan-level network CSV
python c2/c2_merge_network.py \
    --all-plans  output/c2a_network/c2a_plans.csv \
    --unresolved output/c2a_network/c2a_unresolved.csv \
    --cms-file   data/plan_attributes_py2026.csv \
    --out-dir    output/c2

# 5a. Stream Health Net CA rate file (701 MB — cached after first run)
python c2b/c2b_agent.py \
    --rate-file <health-net-ca-rate-url> \
    --out-dir output/c2b --cache-dir output/c1/cache

# 5b. Label prices by evidence tier
python c2b/c2b_steps5to8.py \
    --kept    c2b_step4_kept_prices.parquet \
    --out-dir output/c2b_steps5to8
```

---

## Key design decisions

| Decision | Rationale |
|---|---|
| Evidence or `unknown` — never guess | Any invented network label would corrupt downstream cost-effectiveness analysis |
| Quote required for every label | Reproducibility: every label can be traced to an exact source string |
| CMS join: all rows must agree | If even one CMS row for a product disagrees on PlanType, the plan stays unknown |
| `shared` is an assumption | Identical cross-group rates suggest PPO membership but this is not proven |
| Cache everything | Re-running the full pipeline makes zero additional network calls after the first run |
| No hardcoded paths | All file paths are `--argument` flags; the pipeline is relocatable |

---

## Outputs (not committed — see `.gitignore`)

| File | Description |
|---|---|
| `output/c2/c2_plans_network.csv` | 3,214 rows, plan-level network classification |
| `output/c2/c2_summary.json` | Counts by network_type, group headline, evidence breakdown |
| `output/c2b_steps5to8/c2b_step7_labelled.parquet` | 50,933 rows, provider price labels → C4 input |
| `data/plan_attributes_py2026.csv` | CMS PUF (30.6 MB, provenance in `data/MANIFEST.json`) |

---

## Evidence rules summary

```
Priority  Label       Evidence type          Confidence
────────  ──────────  ─────────────────────  ──────────
1         PPO         direct_ffs             high
2         PPO         business_name_ppo      high
3         PPO/non-PPO name_keyword           high
4         PPO/non-PPO cms_plan_attributes    high
5         shared      shared_contract        medium  ← assumption
6         ambiguous   ambiguous              low
7         unknown     single / not_in_cms /  —
                      no_public_document
```
