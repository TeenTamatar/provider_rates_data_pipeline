# LOGIC.md — Pipeline rules, counts, and decisions

Every rule added to the pipeline is documented here with: what it does, why,
and the counts it produces. Updated after each step.

---

## C1: Parse index files

**Script:** `c1/c1_parse_index.py`
**Command:** `python c1/c1_parse_index.py --out output/c1 --index <4 URLs>`

### Rules

| ID | Rule | Why |
|---|---|---|
| R-C1-1 | Stream-parse index JSON with `ijson` | Index files are up to 576 KB; streaming avoids loading the whole file into memory |
| R-C1-2 | Infer plan_id_type from shape (EIN=9 digits, HIOS=5+state+3, etc.) | 154 CA Health Net rows are labelled `ein` by the payer but are actually `hios_product`; the declared label is kept, the inferred label recorded separately |
| R-C1-3 | Derive state from rate_file_url suffix first, then HIOS state code, then plan name prefix | URL suffix is the most reliable signal; HIOS state is second; plan name prefix is a fallback |
| R-C1-4 | Fill state from HIOS issuer ID when every other row with the same issuer resolves to one state | Issuers are assigned per state; if unambiguous, the state is safe to impute |
| R-C1-5 | Deduplication key: (brand, plan_name_normalised, plan_id, plan_id_type, plan_market_type, rate_file_url) | Removes 1 duplicate row |

### Counts (run 2026-10-18)

| Metric | Value |
|---|---|
| Index files parsed | 4 |
| Raw plan rows | 3,215 |
| Duplicates removed | 1 |
| Unique plan rows | 3,214 |
| Unique rate files | 33 |
| State conflicts | 0 |
| QualChoice rate file | 404 — file absent from Centene CDN; recovery exhausted 80+ URL probes |

---

## C2a: Network type classification (name-keyword pass)

**Script:** `c2/c2a_classify.py`
**Command:** `python c2/c2a_classify.py --plans output/c1/c1_plans.csv --out-dir output/c2a_network`

### Rules

| ID | Rule | Why |
|---|---|---|
| R-C2a-1 | `PPO` anywhere in name counts (e.g. CUSPPO) unless followed by a letter | PPO is always a stand-alone brand signal |
| R-C2a-2 | `HMO`, `EPO`, `POS` must be standalone tokens (no letter on either side) | Prevents POST → POS, PRIMO → none |
| R-C2a-3 | Payer codes (EOA, SAL) mapped from `PLAN_CODES` dict; **source required** per entry | EOA = Health Net Elect Open Access (HMO-based); SAL = Health Net Salud HMO y Mas. Source: Health Net plan documents (cited in PLAN_CODES) |
| R-C2a-4 | PPO + EPO together → PPO (EPO design on PPO network, assumption A8) | Confirmed pattern in Health Net CA plans |
| R-C2a-5 | PPO + HMO or POS together → `unknown` (conflict, not guessed) | No evidence rule; stops incorrect guessing |
| R-C2a-6 | No token found → `unknown` | Never guess. Evidence or unknown |

### Counts — all plans (run 2026-10-18)

| network_type | plans | % |
|---|---|---|
| unknown | 1,570 | 48.8% |
| PPO | 720 | 22.4% |
| HMO | 623 | 19.4% |
| HMO+SAL | 163 | 5.1% |
| EOA | 127 | 4.0% |
| POS | 7 | 0.2% |
| SAL | 4 | 0.1% |

### Counts — CA group only (acceptance check vs section 4)

| Metric | Expected | Got | Pass? |
|---|---|---|---|
| PPO (strict) | 456 | 456 | ✅ |
| non-PPO known | 856 | 856 | ✅ |
| unknown | 0 | 0 | ✅ |
| conflicts | 0 | 0 | ✅ |

### Scope: group plans only (organiser confirmed commercial group)

| State | Group plans | Individual plans |
|---|---|---|
| CA | 1,312 | 58 |
| AR | 307 | 75 |
| OR | 381 | 0 |
| All others | 0 | 1,156 |
| **Total** | **2,000** | **1,214** |

> Only CA, AR and OR have group plans. All other states are individual-only.
> The 720 strict-PPO and 2,290 inclusive counts include individual plans.
> **Group-only strict PPO: 607. Group-only unknown: 536.**
> `c2_plans_network.csv` is not a finished classification — 536 group plans
> are still `unknown` pending Task D (evidence agent). See per-state profiles.

### Why 1,570 plans are unknown

All Ambetter-brand plans (individual marketplace) use metal-level names only
(Silver, Gold, Bronze, etc.) with no PPO/HMO token. QualChoice plans use
employer-specific names with no network token. Task D (c2_network_evidence.py)
will resolve these once SBC URL patterns are provided by the user.

---

## C2: Network evidence (Task D) — stub

**Script:** `c2/c2_network_evidence.py`
**Status:** Built; not yet run on real documents.

### Evidence priority

1. CMS Plan Attributes file (HIOS plan ID join) — no network fetch needed
2. SBC PDF document — fetched with robots.txt check + 1s per-host delay
3. Model fallback — stub only; requires user to supply a model

### Safeguards in place

- `robots.txt` checked before every URL fetch
- 1.0 s per-host delay between requests
- Plan-year check: documents not for 2026 are rejected
- Quote from document text is required; guessing is rejected
- Full disk cache: re-run makes zero network calls

**Waiting for:** SBC URL pattern per payer (user to supply).

---

## C2b steps 5-8: parallel-contract test, resolve copies, label prices

**Script:** `c2b/c2b_steps5to8.py`
**Input:** `c2b_step4_kept_prices.parquet` (50,933 rows, Health Net CA group)

### Key: two independent copy concepts

| Column | Scope | Meaning |
|---|---|---|
| `copy_status` (stored, step 4) | Within one `provider_group_id` | Same group, same code, multiple rows |
| `parallel_contract_status` (derived, step 5) | Across all `provider_group_id`s | Different groups, same code, different rates |

Step 5 derives `parallel_contract_status` independently and does NOT overwrite `copy_status`.

### Step 5 — Cross-group parallel-contract test

| Status | Keys | Rows |
|---|---|---|
| identical_copies | 10,923 | 31,207 |
| single | 15,260 | 15,260 |
| differing_copies | **1,382** | **4,466** |

### Step 6 — Resolve parallel copies (Rules 6a/6b/6c)

| Rule | Verdict | Keys | Rows |
|---|---|---|---|
| 6a: unique PPO-labelled copy | matched_ppo_copy | 0 | 0 |
| 6b: multiple PPO rates | ambiguous_multiple_ppo_rates | 5 | 10 |
| 6c: no PPO-labelled group | ambiguous_no_ppo_group | 1,377 | 4,456 |

All 1,382 differing-copy keys are ambiguous. The 182 `entry_label=ppo` rows appear only in single/identical-copy keys, not in differing-copy keys.

### Step 7 — Price labels

| evidence_type | network_label | confidence | rows | keys |
|---|---|---|---|---|
| shared_contract | shared | medium | 31,205 | 10,922 |
| single | unknown | low | 14,737 | 14,737 |
| ambiguous | ambiguous | low | 4,466 | 0* |
| direct_ffs | PPO | high | 343 | 342 |
| business_name_ppo | PPO | high | 182 | 182 |

*Ambiguous rows carry `negotiated_rate = 0.0`; C4 drops them as non-positive.

**Strict PPO keys: 524. Inclusive (PPO + shared) keys: 11,446.**

### Step 8 — Plan confidence (threshold 80%)

- 502 / 3,332 TINs meet the threshold.
- Median pct_confident = 0.0% — most TINs have only `single` (low confidence) keys with no cross-group comparison.
- Rationale: 80% means the dominant contract is identified; remaining 20% is within expected TiC data noise.

---

## C4: Rate selection — v1 (step-4) vs v2 (step-7)

**Script:** `c4_select.py` (unchanged; input differs)

### v1 vs v2 comparison

| Metric | v1 (step-4) | v2 (step-7) | Difference |
|---|---|---|---|
| input_rows | 50,933 | 50,933 | 0 |
| dropped_not_positive | 0 | 4,466 | +4,466 (ambiguous rows, rate=0) |
| candidate_rows | 50,933 | 46,467 | −4,466 |
| chosen_keys | **27,565** | **26,183** | −1,382 |
| unique_TINs | 2,856 | 2,833 | −23 |
| unique_NPIs | 3,694 | 3,665 | −29 |
| conflicts at start | 1,382 | **0** | −1,382 |
| share_no_conflict | 95.0% | **100%** | +5% |

The 1,382 key difference = exactly the differing-copy keys resolved upstream in step 5-6. C4 rules S1/S4/S5 no longer fire in v2 because all conflicts are already resolved.

### Funnel: 1,500,288 -> 50,933 -> 26,183

| Stage | Rows | Note |
|---|---|---|
| Raw price rows (full table) | 1,500,288 | C2b steps 1-4; table not on disk |
| After step-4 dedup | **50,933** | `c2b_step4_kept_prices.parquet` |
| After step-7 ambiguous exclusion | **46,467** | 4,466 ambiguous rows have rate=0 |
| Chosen keys (C4 v2) | **26,183** | `output/c4_v2/c4_chosen.parquet` |

### NPI/TIN gap

| Source | NPIs | TINs |
|---|---|---|
| C2b steps 1-4 summary (full table) | 3,735 | 2,888 |
| C4 v1 chosen | 3,694 | 2,856 |
| C4 v2 chosen | 3,665 | 2,833 |

Gap 3,735 -> 3,694: cannot explain without full price table or C2b step 1-4 source. Not stated; not guessing.
Gap 3,694 -> 3,665: providers whose only keys were ambiguous differing-copy rows, now excluded.

### Billing code breakdown (v2 chosen)

| Type | Keys | % |
|---|---|---|
| RC (revenue code) | 15,216 | 58.1% |
| CPT | 9,021 | 34.5% |
| HCPCS | 1,946 | 7.4% |

> **Q3 decision (2026-10-18):** Keep RC rows in full output, flagged by `billing_class`. Excel view shows CPT/HCPCS on HiLabs codes only. RC filter is a one-line change in C6.

> **C3 note:** Must be computed on the full 1,500,288-row table. Table not on disk; confirm location before starting C3.

---

## Decisions log

| Date | Question | Decision |
|---|---|---|
| 2026-10-18 | Q3: Facility RC prices in scope? | Keep in full output, flagged. Excel = HiLabs CPT/HCPCS codes only |
| 2026-10-18 | Q9: Verified address definition | ≥2 independent source families agree, one recent. Applies to C5 |
| 2026-10-18 | Q10: Scrape provider directories? | **No.** Bulk files only (NPPES, CMS D&C, geocoder) |
| 2026-10-18 | Q14: Excel row limit | HiLabs-code rows only (~8,615 for Health Net CA). Guard above 1,000,000 rows → split/CSV |
| 2026-10-18 | SBC URL pattern per payer | User will supply. Do not invent hosts |

---

## Open items (stop and wait)

- **Q3 HiLabs code check**: waiting for `data/hilabs_codes.csv` from user
- **Task D**: waiting for SBC URL patterns from user
- **QCNN and AR/OR unknown fragments**: listed in state profiles; no meaning assigned; awaiting user source
- **Oregon rate extraction**: decision deferred until after California
- **Arkansas**: recorded — 307 group plans, rate file 404, no further work
- **C3**: full 1,500,288-row price table not on disk; confirm location before starting
- **C5, C6, C7**: not started
