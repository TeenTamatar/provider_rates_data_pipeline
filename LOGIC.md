# Logic File: Centene PPO Provider Rate Pipeline

This file records every decision the pipeline makes: the sources used, the rules applied, and how conflicts are resolved. It covers deliverable 2 of the problem statement:

- **(a)** how the PPO network was identified
- **(b)** how TIN → NPI → address (and specialty) were linked

It also records how each file is graded (C3, Section 3C), which feeds deliverable 3.

**Contents.** 1 Scope · 2 Assumptions · 3 (a) Identifying the PPO network (C1, C2, C2b) · 3C File quality score (C3) · 4 Rate selection (C4) · 5 (b) Mapping TIN → NPI → address and specialty (C5; the provider rate file C6 is 5.5) · 7 Cost-effectiveness (C7) · 8 Change log. Rule IDs (R1.x to R7.x) are used by the code, the reports and the video; assumption IDs (A1, A7 to A10) mark decisions that rest on a judgement, not on a source.

---

## 1. Scope

| Dimension | Problem statement | Our interpretation |
|---|---|---|
| Payer | Centene | All Centene brands in the index files (Ambetter, Health Net, QualChoice, Fidelis). Brands are filtered by state and market, not by name. |
| Market | "Commercial group, California" | **Commercial group (employer) plans only**, confirmed by the organizers on 2026-10-17. Individual plans (e.g. Ambetter) are out of scope. See [A1](#a1-commercial-group-means-employer-group-plans-only-confirmed). |
| Network | PPO | Identified in C2; see Section 3. |
| Provider type | Type 1 and Type 2 NPIs | Both kept. |

**Excluded:** government programs (Medicare / Medicare Advantage, Medicaid / Medi-Cal), even when a Centene brand administers them.

---

## 2. Assumptions

Each assumption has an ID so code, the summary file and the video can refer to it.

### A1. "Commercial group" means employer group plans only (confirmed)

| | |
|---|---|
| **Status** | **Confirmed by the organizers, 2026-10-17.** Replaces the earlier working assumption that individual plans were also in scope. |
| **Problem statement text** | Section 3, Scope: "Market: Commercial group, California" |
| **Interpretation** | Keep only plans with `plan_market_type = group`. Individual (marketplace) plans are out of scope. |
| **Effect on the pipeline** | **C1:** the in-scope plan file `c1_plans_ca.csv` keeps California **and** `group` rows (`--market group`, the default; `--market all` restores both). The full `c1_plans.csv` still keeps every plan. **C2:** classifies the 1,312 Health Net group plans. **C2b onward:** one rate file, `healthnet-ca`. |
| **What changed** | Ambetter CA (58 individual plan rows, 31 PPO) and its rate file (240 MB, 74,032 NPIs) left scope. In scope now: 1,312 plan rows, 1 rate file, 3,735 NPIs, 2,888 TINs, 3,780 TIN–NPI pairs. |

### A8. Plans named "PPO … EPO" belong to the PPO network

| | |
|---|---|
| **Status** | Adopted (team decision, 2026-10-17). Not confirmed in a Health Net document. |
| **Interpretation** | C2 classifies by **network**, not by benefit design. A name such as `CA L CUS PPO SACRAMENTO CITY USD- EPO` describes an EPO benefit design (no out-of-network coverage) built on Health Net's custom PPO network, so the plan counts as PPO. |
| **Why** | "CUS PPO" names the custom PPO product line and "EPO" the benefit design. Sacramento City USD (EIN 946002491) offers `CUS HMO`, `CUS PPO` and `CUS PPO … - EPO` side by side, which only makes sense if the EPO option differs from the PPO in out-of-network benefits, not in network. Insurers commonly build EPOs on their PPO network. |
| **Effect on the pipeline** | Step 1 keeps these plans with `network_note = PPO network, EPO benefit design`; step 2 no longer lists them as non-PPO. California: 3 plans (Ironworkers FWP EPO\$15 and EPO\$10, Sacramento City USD EPO). |
| **What changes if it's wrong** | Remove the PPO + EPO exception in `c2_step2_other_network_names.py`; the 3 plans move back to non-PPO. All three share the Health Net CA rate file, so prices are unaffected either way. |

### A9. In the shared rate file, unconflicted rates count as PPO; parallel contracts are dropped

| | |
|---|---|
| **Status** | Adopted (team decision, 2026-10-18). |
| **Interpretation** | Every price in the Health Net CA file applies to the plans that point to it, and the PPO network is the widest Health Net commercial network. So a rate is kept as PPO unless there is evidence against it: an excluded TIN entry (step 2), capitation (step 3), expiry (step 4), or a **parallel contract**, i.e. the same TIN–NPI priced for the same service at different rates in different groups (step 5). Parallel contracts are treated as ambiguous (possibly different networks) and dropped: the **strict** route. |
| **Why** | Only 102 TIN–NPI pairs carry a PPO marker, so keeping marked rates alone would leave almost nothing; the file gives no network label otherwise. For conflicts, the team preferred precision: everything kept has one unambiguous rate. Dropping costs 9 of 3,738 providers and 864 of 35,148 provider × service rates (2.5%). |
| **What changes if it's wrong** | If conflicts are PPO product variants (standard vs custom PPO), valid PPO rates are lost, mostly office visits. Resolving them is future work [FW1](docs/FUTURE_WORK.md#fw1-resolve-parallel-contracts-instead-of-dropping-them); switching R2.24 from "drop" to "keep the resolved copy" reverses it. If unconflicted rates include HMO-only contracts, some non-PPO rates remain; no evidence in the file separates them. |

### A7. Health Net "EOA" plans are not PPO plans

| | |
|---|---|
| **Status** | Confirmed and decided (2026-10-17). EOA is not a PPO plan. Its PCPs and physician groups come from the HMO network; only specialist visits may use the PPO network. Team decision: EOA plans are non-PPO, with no extra flag (the analysis works on the filtered PPO data only). |
| **What the source says** | Health Net's EOA employer flyer ([FLY057815EP01, 1/25](https://healthnet.com/content/dam/centene/healthnet/pdfs/broker/ca/general/hn-eoa-oe-flyer-2025.pdf)): EOA = **Elect Open Access**, issued by **Health Net of California, Inc.** Members choose a physician group and PCP **from the HMO network** ("HMO option"); through the "PPO option" they can "see specialists in the PPO Network for exams and evaluations without referrals". |
| **Interpretation** | An EOA plan is an HMO-based hybrid: an HMO plan with a PPO-network specialist benefit. It is not a PPO product, so the 127 California EOA plans are in the non-PPO sheet. The earlier guess that EOA was POS-like was wrong. |
| **Why** | (1) Health Net group plan IDs are employer EINs, so plans sharing an ID are one employer's menu. Of the 106 employers offering an EOA plan, **65 also offer both an HMO and a PPO** and 21 an HMO only. An employer would not normally offer two PPOs on one network, so EOA is a third, separate option. (2) EOA cost-sharing copies the employer's HMO designs, e.g. EIN `203028564` offers `CA L EC EOA 20/20%/2500` next to `CA L HMO 20/20%/2500`, and EIN `201538255` pairs `CA L SC HMO 20/20%/2500` with `CA L EC EOA 20/20%/2500`. (3) No EOA plan name also says PPO. |
| **Effect on the pipeline** | C2 step 2: EOA plans go to the non-PPO sheet with `classified_by = plan_code`, `code_status = sourced`. The code lives in `reference/network_codes.csv`, not in the code. |
| **What changes if it's wrong** | Change the EOA row in `reference/network_codes.csv` and re-run steps 2 and 3; the 127 plans move to a hybrid class or to PPO. |
| **Why it matters less than it looks** | All 1,312 Health Net California plans, EOA included, point to the same rate file. Whether EOA counts as PPO changes the plan list, not which prices exist; separating PPO prices happens inside the rate file. |

### A10. Ambetter-labelled entries in the Health Net file are not Health Net group PPO

| | |
|---|---|
| **Status** | Adopted (team decision, 2026-10-17). Candidate question for the organizers. |
| **Data** | The Health Net CA file has TIN entries whose business name is exactly `DIRECT NETWORK: AMBETTER PPO` (18 entries, 21 TIN–NPI pairs) or `DIRECT NETWORK: AMBETTER HMO` (17 entries, 19 pairs). The name carries no practice name, and none of these pairs appears in any other entry of the file. |
| **Interpretation** | These are contracts of Ambetter's (individual marketplace) direct network. Ambetter in California is "Ambetter from Health Net" (same issuer, `67138`), which explains why they appear in the Health Net file. "AMBETTER PPO" names an Ambetter PPO, not the Health Net group PPO. |
| **Effect on the pipeline** | C2b step 2: both labels mark the **entry** as excluded (`other_product` / `other_network`), not the whole provider group (R2.13). |
| **What changes if it's wrong** | Change the `ambetter_ppo` row in `reference/business_name_labels.csv` to `ppo` and re-run step 2: 21 pairs move back into the PPO set. |

---

## 3. (a) Identifying the PPO network

### 3.1 Parsing the index files (C1)

**Code:** `pipeline/c1_parse_index.py`. **Outputs:** `output/c1/` (`c1_plans.csv`, `c1_plans_ca.csv`, `c1_rate_files.csv`, `c1_duplicates_removed.csv`, `c1_summary.json`).

**Sources**

| Index file | `last_updated_on` | Structures | Plan rows | Rate files |
|---|---|---|---|---|
| `2026-09-28_ambetter_index.json` | 2026-09-28 | 28 | 1,120 | 28 |
| `2026-09-28_fidelis_index.json` | 2026-09-28 | 2 | 94 | 2 |
| `2026-09-28_qualchoice_index.json` | 2026-09-28 | 1 | 308 | 1 |
| `2026-09-30_healthnet_index.json` | 2026-09-30 | 2 | 1,693 | 2 |

All four use schema version 2.0.0 and are published by Centene Management Company LLC as a Third Party Administrator.

**Rules**

| # | Rule | Why |
|---|---|---|
| R1.1 | Index files are streamed one `reporting_structure` at a time (`ijson`), never loaded whole. | Index files from other payers can be gigabytes; the same code must scale. |
| R1.2 | One output row per plan × in-network rate file. A structure with several rate files gives several rows per plan. | The rate file is the unit later steps process; the plan is what links it to a network. |
| R1.3 | Every published field is kept as-is (issuer, sponsor, allowed-amounts URL, index date, source file, structure position). Derived fields are added next to them, never in place of them. | C2 and C3 need the raw values, and every derived value stays traceable. |
| R1.4 | **Brand** is taken from the index file name (`..._healthnet_index.json` → `healthnet`). | Each brand publishes its own index, and names inside overlap between brands (e.g. plans named "Ambetter from Fidelis Care" sit in the Fidelis index, not the Ambetter one). |
| R1.5 | **Plan ID type is checked against the ID's shape.** EIN = 9 digits. HIOS = 5 (issuer), 10 (issuer + state + product), 14 (plan) or 16 (plan variant) characters. The published type is kept; the inferred type and a mismatch flag are added. | The published type is unreliable (see results). Mislabelled IDs are also a file-quality signal for C3. |
| R1.6 | HIOS IDs are split into **issuer** (5), **state** (2) and **product** (first 10 characters). | Under ACA rules a HIOS product is a set of plans sold on one network type, so the product ID is an input to C2. |
| R1.7 | **State** comes from the first available source: (1) rate file URL suffix (`..._healthnet-ca_in-network...` → CA), (2) HIOS state code, (3) a leading state token in the plan name (`CA L PPO ...`), (4) the state of other rows with the same HIOS issuer ID, if they all agree. Each row records which source was used, and disagreements between sources are flagged. | The URL suffix applies to the whole file, which is the unit we process. HIOS issuer IDs are assigned per state, so (4) is safe whenever the issuer only appears in one state. Codes that are not US states (Fidelis `-es`/`-ex`, HIOS `EP`) are ignored. |
| R1.8 | **Deduplication key:** brand + plan name (upper-cased, whitespace collapsed) + plan ID + plan ID type + market type + rate file URL. The first occurrence is kept; dropped rows are written to `c1_duplicates_removed.csv` with the row they matched. | A plan ID alone is not unique (one ID covers up to 144 plan names), so the name must be part of the key. Case and spacing differences are not meaningful. |
| R1.9 | **Rate file table:** one row per unique rate file URL, with plan counts, ID counts, market types, state, and a HEAD check (HTTP status, size, last modified). Nothing is downloaded at this step. | "Many plans point to the same file": later steps work per file, once. The HEAD check catches dead links early and feeds C3. |
| R1.10 | No network filtering happens in C1. The full table `c1_plans.csv` keeps every state and market. | C2 should show exactly what it excluded and why; other states and markets stay available for re-runs. |
| R1.11 | C1 also writes an in-scope file, `c1_plans_ca.csv`: the rows of `c1_plans.csv` whose derived state (R1.7) is California **and** whose `plan_market_type` is `group` ([A1](#a1-commercial-group-means-employer-group-plans-only-confirmed)). Both are settings: `--state` (default `CA`) and `--market` (default `group`; `all` keeps every market). | Later steps work on the in-scope plans only (1,312 rows, 1 rate file). The full table stays for totals and for re-running on other states or markets. |

**Results (run 2026-10-17)**

| Metric | Value |
|---|---|
| Plan rows parsed | 3,215 |
| Duplicate rows removed | 1 (QualChoice "PreferredChoice $500 Deductible", same plan apart from case/spacing) |
| Unique plan rows | 3,214 |
| Unique plan IDs | 544 |
| Unique rate files | 33 |
| Plan rows by market | group 2,000 · individual 1,214 |
| State source used | rate file URL 2,813 (all Ambetter and Health Net rows, including all 1,370 California rows) · HIOS ID 173 (Fidelis 88, QualChoice 85) · HIOS issuer lookup 228 (QualChoice 222, Fidelis 6) · plan name 0 (cross-check only) · none 0. Sources 2–4 decided only New York and Arkansas rows, whose rate file names carry no state (`fidelis-es`, `fidelis-ex`, `qualchoice`). |
| State conflicts between sources | 0 |
| Plan ID type mismatches | 154 |
| Unreachable rate files | 1 (QualChoice, HTTP 404) |

**Observations carried forward**

- **California, all markets:** 2 rate files, 1,370 plan rows.
  - `healthnet-ca`: 1,312 group plan rows, 359 plan IDs, 702 MB. **In scope.**
  - `ambetter-ca`: 58 individual plan rows, 6 HIOS products, 240 MB. Out of scope (individual market, A1).
- **Health Net's ID labels are wrong for 154 California rows.** They are labelled `ein` but are HIOS product IDs (`67138CA0xx`, 9 products). The issuer prefix `67138` is shared with Ambetter California. → C2 signal, C3 quality metric.
- **QualChoice's IDs are HIOS issuer IDs only (5 digits).** They are less specific than a plan ID. → C3 quality metric.
- **QualChoice's rate file returns 404.** The index points to a file that is not published. → C3 quality metric (out of scope for California).
- **Fidelis `25303EP000` (Essential Plan)** is not a valid HIOS state code. The Essential Plan is New York's Basic Health Program, a government program. → Out of scope regardless; noted as an ID-format issue.

### 3.2 Filtering to California commercial
California is identified per R1.7; every California row got its state from the rate file URL. California has 1,370 plan rows across markets (Health Net 1,312 group, Ambetter 58 individual). Per [A1](#a1-commercial-group-means-employer-group-plans-only-confirmed) only `group` rows are kept, so `c1_plans_ca.csv` (R1.11) holds **1,312 Health Net group plan rows on 1 rate file** (`healthnet-ca`). The 58 Ambetter individual rows and the `ambetter-ca` file drop out.

### 3.3 Classifying network type (C2)

C2 works in steps. Each step sorts plans by one kind of evidence; at the end every plan gets a confidence value and a threshold decides which plans form the PPO network. The steps read the in-scope file `output/c1/c1_plans_ca.csv` (California group plans, R1.11). The same scripts run on all states by passing `--plans output/c1/c1_plans.csv`, so the rules are not California-specific.

**Code:** `pipeline/c2_step1_ppo_names.py`, `c2_step2_other_network_names.py`, `c2_step3_unlabelled_names.py`; plan codes in `reference/network_codes.csv`. **Outputs:** `output/c2/`.

**Rules**

| # | Rule | Why |
|---|---|---|
| R2.1 | **PPO by name (step 1):** a plan whose name contains "PPO", case-insensitive, anywhere. Recorded as `word` (stands alone) or `embedded` (inside a token, e.g. `CUSPPO`). | Spacing is inconsistent in plan names: `CA L CUSPPO 15/250/20% RAP` sits beside 164 `CA L CUS PPO …` plans, and QualChoice writes both `EnhancedPPO` and `Enhanced PPO` within one product. |
| R2.2 | **Other type by name (step 2):** a plan whose name contains HMO, EPO or POS. A letter may come before the type (`CUSHMO`) but not after it. | The "not after" rule stops false matches such as POS in `POST` (`CA L CUS PPO UC POST DOC`, a postdoctoral plan). |
| R2.3 | **Other type by plan code (step 2):** a plan whose name contains a brand-specific code listed as non-PPO in `reference/network_codes.csv`, as a whole word. Rows record `classified_by = plan_code` and the code's status. | Some brands mark the product with a code instead of a network word. Keeping codes in a table makes each one visible, sourced and reversible. Current entries, both sourced from Health Net documents: `EOA` = Elect Open Access, an HMO-based hybrid with PPO-network specialist access (assumption [A7](#a7-health-net-eoa-plans-are-not-ppo-plans)); `SAL` = Salud con Health Net, i.e. Salud HMO y Más, an HMO issued by Health Net of California, Inc. with care in Mexico through SIMNSA ([flyer FLY1566908EL01w](https://wordandbrown.com/getmedia/33d764b4-b489-43c5-badb-6e6313a1655c/hn-broker-salud-flyer-2025_1.pdf)). |
| R2.4 | **Unlabelled (step 3):** everything not matched by R2.1–R2.3, using the same matching functions. | The three sheets then partition all plans; nothing is dropped silently. |
| R2.5 | A name that says **PPO and EPO only** is a PPO-network plan with an EPO benefit design: it stays in the PPO sheet (`network_note`) and is not listed as non-PPO ([A8](#a8-plans-named-ppo--epo-belong-to-the-ppo-network)). A name that says PPO together with HMO or POS would still be a **conflict** in both sheets (none in California). | C2 classifies by network. Three California plans: `CA L CUS PPO CA IRONWORKERS FWP EPO$15`, `…FWP-EPO$10`, `CA L CUS PPO SACRAMENTO CITY USD- EPO`. |
| R2.6 | HIOS product consistency is a **confidence factor**, not a classification step. | Of 158 HIOS products, none mixes PPO and HMO names (supports A3), but only 3 products have unlabelled plans next to labelled ones, and none are in California. California's unlabelled plans have EIN IDs, so no product exists to borrow from. |
| R2.7 | For Health Net group plans the plan ID (EIN) identifies the **employer**, not the network; plans sharing an EIN are one employer's menu of options. | Used as evidence for A7: what an employer offers next to a plan shows what the plan is *not*. |

**Results (California group plans, 1,312 rows; run 2026-10-17 after the group-only scope change)**

| Sheet | Rows | Detail |
|---|---|---|
| PPO by name | 456 | All Health Net; includes the 3 PPO + EPO plans; 1 embedded match (`CUSPPO`) |
| Non-PPO by name or code | 856 | HMO 719, POS 6 (network word, 725 rows); EOA 127 and SAL 4 (plan code, 131 rows) |
| Both (conflicts) | 0 | The 3 PPO + EPO plans count as PPO (R2.5, A8) |
| Unlabelled | 0 | The 4 former cases (`CA L SAL Mex 5/0/1500` ×3, `CA L&T CUS SAL CEMENT MASONS CA H&W GF+`) are now matched by the `SAL` code |

### 3.4 Ruling out HMO, EPO and POS

| Excluded type | Evidence | Plans (California) |
|---|---|---|
| HMO | "HMO" in the plan name (R2.2) | 719 |
| POS | "POS" in the plan name (R2.2) | 6 |
| EPO | "EPO" in the plan name without "PPO" (R2.2). The 3 California names with both are PPO-network plans (A8). | 0 |
| EOA (Elect Open Access: HMO-based hybrid with PPO-network specialist access) | Plan code (R2.3), Health Net EOA flyer, employer-menu evidence (R2.7), assumption A7. Main network is the HMO network. | 127 |
| Salud (HMO) | Plan code `SAL` (R2.3), Health Net Salud flyer | 4 |

Plans with no network signal stay **unlabelled** until a later step adds evidence; they are not guessed. After adding the sourced `SAL` code, no California plan is unlabelled.

### 3.5 PPO prices inside shared rate files (C2b)

The one in-scope rate file (`healthnet-ca`) is used by PPO and non-PPO plans (456 PPO, 856 non-PPO), so the PPO network is separated at the level of provider groups and prices, not files.

**Step 1: provider group tables.** Code: `pipeline/c2b_step1_groups.py`. Outputs: `output/c2b/c2b_groups.parquet` (rate file × group × member × NPI), `c2b_group_summary.parquet` (one row per group), `c2b_step1_summary.json`.

| # | Rule | Why |
|---|---|---|
| R2.8 | The in-scope rate files are the unique files referenced by the in-scope plan table (`c1_plans_ca.csv`): only `healthnet-ca` under the group-only scope. | Files follow from plans; nothing is hardcoded. Re-running C1 with `--market all` would bring `ambetter-ca` back automatically. |
| R2.9 | `provider_references` are streamed into a normalized group table (one row per group member NPI). Prices are not read here and are never expanded to one row per NPI. | Keeps the pipeline scalable to large files (Ambetter would expand to 79 million NPI × price rows, F23); joins on `provider_group_id` keep everything small. |
| R2.10 | Each group gets a `member_signature`: a hash of its sorted (TIN, NPI) pairs. Groups with the same signature list exactly the same providers. | Needed to find parallel contracts in steps 4–5. |
| R2.11 | `network_name` is kept but not used to separate PPO prices. | Health Net leaves it empty on every group. (Out-of-scope Ambetter sets it to "Exchange CA" on every group, which names the marketplace network, not PPO vs HMO; F33.) |

| Result (in scope) | Health Net CA |
|---|---|
| Provider groups | 2,825 |
| Member NPI rows | 5,672 |
| Unique TINs / NPIs / TIN–NPI pairs | 2,888 / 3,735 / 3,780 |
| NPIs per group (median, max) | 1, 151 |
| Repeated member sets (groups involved) | 353 (1,207) |
| `network_name` | empty on all groups |
| Parse time | 3.7 s |

Every member has a TIN and NPI. These 3,780 TIN–NPI pairs are the full provider universe for C4–C6. For reference only, the out-of-scope Ambetter CA file has 3,141 groups, 74,032 NPIs and 89,577 pairs.

**Step 2: network markers in TIN business names.** Code: `pipeline/c2b_step2_name_labels.py`; patterns in `reference/business_name_labels.csv`. Outputs: `c2b_step2_name_patterns.csv` (pattern inventory), `c2b_step2_member_labels.csv` (one row per TIN entry), `c2b_step2_group_name_labels.csv` (one row per group), `c2b_step2_summary.json`.

| # | Rule | Why |
|---|---|---|
| R2.12 | Business-name patterns live in a table (`reference/business_name_labels.csv`), one regex per row with label, strength (`strong` or `hint`) and evidence; the first match wins, case-insensitive. Prefixes ending in ":" that no pattern matches are listed in the inventory. | Makes every marker visible, sourced and reversible, as for plan codes (R2.3). The inventory shows nothing is missed: 0 unmatched prefixes. |
| R2.13 | A marker labels the **TIN entry**, not the whole provider group. Excluded entries are dropped; a group is excluded only when every entry is excluded. | One group often lists `PPO: <County>`, `DIRECT NETWORK: AMBETTER HMO` and plain practice names side by side (e.g. group 2245), so a group-level label would drop or keep innocent entries. |
| R2.14 | Strong PPO markers: `PPO: <County>`, `HealthNet PPO (California)`. Strong exclusions: `DIRECT NETWORK: AMBETTER PPO` (other product, [A10](#a10-ambetter-labelled-entries-in-the-health-net-file-are-not-health-net-group-ppo)), `DIRECT NETWORK: AMBETTER HMO`, `SIMNSA` (other network). | These name a network or product directly. SIMNSA is the Baja California network behind Salud HMO (Health Net Salud flyer). |
| R2.15 | Hints, recorded but not used to decide: `Direct Network: <County>` (names no type), `HEALTH NET DIRECT FFS COMMERCIAL` (fee-for-service, commercial: both HMO and PPO are commercial), `IPA` (usually HMO delivery, but IPAs can hold PPO contracts). | Each is consistent with PPO and non-PPO alike; treating them as decisive would be a guess. Later steps may use them for confidence. |

| Result (Health Net CA) | TIN entries | TIN–NPI pairs | Groups |
|---|---|---|---|
| PPO markers (`ppo_county` 89, `healthnet_ppo` 2) | 91 | 102 | 69 |
| Excluded: Ambetter PPO / Ambetter HMO / SIMNSA | 18 / 17 / 2 | 21 / 19 / 2 | 13 / 15 / 2 |
| Hints: direct FFS commercial / IPA / Direct Network | 278 / 13 / 6 | 297 / 14 / 6 | 278 / 12 / 5 |
| No marker | 4,151 | 3,319 | – |

Group roll-up: `ppo_labelled` 57, `ppo_and_excluded` 12, `partly_excluded` 9, `excluded` 4 (5 pairs), `none` 2,743. 138 PPO-labelled pairs also sit in unlabelled groups (input to steps 5–6).

**Step 3: price table and capitation.** Code: `pipeline/c2b_step3_capitation.py`. Outputs: `c2b_prices.parquet` (one row per published price × provider group, 1,500,288 rows; 10.6 s), `c2b_step3_capitation_groups.csv`, `c2b_step3_summary.json`.

| # | Rule | Why |
|---|---|---|
| R2.16 | All prices of the in-scope files are streamed once into a normalized price table keyed by `provider_group_id`; later steps read it instead of the JSON. | One pass per file (scalability). Each negotiated rate references exactly one group in this file, so the table has one row per price. |
| R2.17 | Prices with `negotiation_arrangement = capitation` are flagged `exclude_capitation`, price by price. A group is excluded only if all its prices are capitation. | Capitation (a fixed payment per member per month) is the HMO payment model; PPO pays fee-for-service. The same groups also hold fee-for-service prices, which stay. |

Result: 46 capitation prices (all `fee schedule`, professional) on 33 codes and 19 groups; the rest (1,500,242) are fee-for-service. No group is capitation-only, so no group is excluded. None of the 19 groups carries a name marker.

**Step 4: contract-date check.** Code: `pipeline/c2b_step4_contract_dates.py`. Outputs: `c2b_step4_copy_cases.parquet` (one row per parallel-copy case), `c2b_step4_summary.json`.

| # | Rule | Why |
|---|---|---|
| R2.18 | A price whose `expiration_date` is before the run date is flagged `drop_expired`. | No longer payable. |
| R2.19 | The price key is code type + code + `additional_information` + arrangement + negotiated type + billing class + setting + place of service + modifier. | Health Net puts the HCPCS/CPT or HIPPS code billed under each revenue code in `additional_information` (RC `0250` + `J2405`); 1,128,728 of 1,496,307 RC prices carry one. Without it, different services look like conflicting prices for one service. |
| R2.20 | A **parallel-copy case** is one member signature (R2.10) × key priced by 2+ groups. Copies that differ in rate but not in date are true parallel contracts, resolved in steps 5–6. | Rules out the simplest explanation (an old and a new contract) before treating copies as separate networks. |

| Result | Value |
|---|---|
| Expired prices | 0: every price has `expiration_date` 9999-12-31 |
| Parallel-copy cases | 8,164 over 293 member signatures |
| Cases where the rates differ | 314 (3.8%) over 139 signatures; median max/min rate 1.23 |
| … of those, dates differ | 0, so dates explain none |
| Differing cases by price type | CPT professional 139 of 197 · HCPCS professional 11 of 12 · RC institutional 164 of 7,955 |
| Same group, same key, several rates | 328 keys |

So parallel contracts are rare for facility prices and common for professional prices. This corrects F27 (31% differ), which compared revenue-code prices without the sub-code.

**Step 5: parallel-contract test.** Code: `pipeline/c2b_step5_parallel_copies.py` (under 2 s). Outputs: `c2b_step5_price_keys.parquet` (group × key × rate with duplicate count), `c2b_step5_pair_key_copies.parquet` (one row per TIN–NPI × key), `c2b_step5_group_copy_stats.csv`, `c2b_step5_summary.json`.

| # | Rule | Why |
|---|---|---|
| R2.21 | Price rows are deduplicated to one row per group × key (R2.19) × rate before any test; the duplicate count is kept (`n_rows`, `n_items`). | 97.3% of the 1,500,288 rows are exact copies (F38): revenue code `0636` appears as 129 items, each repeating the same 721 drug prices. Duplicates would inflate every count. |
| R2.22 | The test runs per **TIN–NPI pair × key**, not only per identical provider set (step 4). Prices are joined to pairs only in this test table (35,592 rows). | A provider can sit in groups whose other members differ; per-pair comparison catches those copies too. |
| R2.23 | Each pair × key gets a status: `single` (one group, one rate), `within_group_variants` (one group, several rates; left to C4), `identical_copies` (2+ groups, one rate: one contract, safe to keep), `differing_copies` (2+ groups, different rates: parallel contracts, resolved in step 6). | Separates the cases that need a decision from the ones that do not. |

| Status | Pair × keys | TIN–NPI pairs |
|---|---|---|
| single | 26,472 | 3,747 |
| identical_copies | 8,180 | 370 |
| differing_copies | **877** | 318 (196 TINs) |
| within_group_variants | 63 | 41 |

| Differing copies by price type | Priced in 2+ groups | Differ |
|---|---|---|
| CPT professional | 1,029 | 670 (65%) |
| HCPCS professional | 73 | 43 |
| RC institutional | 7,955 | 164 (2%) |

Differing cases: median max/min rate 1.36, 90th percentile 4.23, max 10.2; up to 26 groups in one case; 140 cases have 3+ rates; 12 involve capitation. Most common codes: office visits 99214 (112) and 99213 (45), RC 0272 (64). Hints on the groups involved: none 861, IPA 15, direct FFS commercial 1. **No differing case touches a PPO-marked entry, and none of the 182 PPO-marked pair × keys is priced by any other group**, so step 6 cannot resolve copies by matching a PPO-labelled price.

**Export after step 4.** `pipeline/c2b_export_kept.py` applies the step 2–4 exclusions (excluded TIN entries, capitation, expired) and writes `c2b_step4_kept_prices.csv` / `.parquet`: 50,933 deduplicated rows (TIN–NPI × group × key × rate) standing for 1,511,243 raw rows; 3,738 pairs, 2,856 TINs, 768 codes. Nothing in it is yet marked PPO or non-PPO beyond the step 2 markers.

**Decision after step 5: strict route (2026-10-18).** See [A9](#a9-in-the-shared-rate-file-unconflicted-rates-count-as-ppo-parallel-contracts-are-dropped).

| # | Rule | Why |
|---|---|---|
| R2.24 | Every TIN–NPI × price key with `copy_status = differing_copies` is dropped as ambiguous. `single`, `identical_copies` and `within_group_variants` (left to C4) are kept. | Different rates for one provider and service may be different networks; the file cannot tell. Cost: 864 provider × service rates (2.5%) and 9 providers. Resolving instead of dropping is future work FW1. |
| R2.25 | A provider group is **not** assumed to belong to one network or plan. Evidence is used at the level of a single price (capitation) or a single TIN entry (business-name marker); group-level properties are hints at most. | The rate-file schema only says a price applies to every provider in the group it references. Groups mix `PPO: <County>`, Ambetter HMO and plain entries (F37), and capitation groups also hold fee-for-service prices (F36). |

**Step 6: final PPO rate table (closes C2b).** Code: `pipeline/c2b_step6_ppo_rates.py`. Outputs: `c2b_ppo_rates.parquet` / `.csv` (kept rows), `c2b_step6_dropped.csv` (dropped rows with `drop_reason`), `c2b_step6_summary.json`.

| # | Rule | Why |
|---|---|---|
| R2.26 | No numeric confidence and no threshold. Every kept plan and rate carries an `evidence` value naming the rule that kept it: plans `name_word`, `name_embedded`, `name_word_epo_design`, `plan_code_sourced` (`c2_plan_network.csv`); rates `ppo_marker` or `unconflicted_rate` (A9). | Every C2 rule is a yes/no rule on explicit evidence. A score per evidence type would be invented, and a threshold over invented scores restates which evidence types we accept. No California plan is borderline, so any threshold keeps the same 456 PPO plans. A measured confidence is needed only where evidence is indirect (other states; future work FW5). |
| R2.27 | Each deduplicated row (TIN–NPI × group × key × rate) is dropped for the first reason that applies: `excluded_entry` (R2.13–R2.14), `capitation` (R2.17), `expired` (R2.18), `parallel_contract` (R2.24); otherwise it is kept as PPO. | One table with one reason per dropped row makes every exclusion countable and reversible. |
| R2.28 | **Read each rate file once.** `pipeline/rate_scan.py` (run after C1) streams every rate file once, as two parallel jobs (provider references; prices), and writes the member, group and price tables to `output/scan/`. C2b steps 1 and 3 and all of C3 read those tables; nothing parses a rate file twice. | Parsing JSON is most of the cost (about 50 s per 700 MB). Health Net CA was parsed three times (C2b step 1, step 3, C3). Verified: every C2, C2b and C3 table is identical to the previous code's output. |
| R2.29 | **Larger payers.** The scan reads local `.json`, `.json.gz`, or streams straight from the URL (`--stream`, no download); writes Parquet in batches of 200,000 rows; bounds its NPI and TIN caches; fetches provider lists hosted elsewhere (`location`, schema v1). | Other payers publish compressed files of tens to hundreds of GB. Verified: a gzip copy of Health Net OR and Ambetter KS streamed from Centene's URL give identical tables and counters to the local scans. |
| R2.30 | **`network_name` first.** When a provider group's `network_name` states a type (PPO, HMO, EPO, POS; `reference/network_names.csv`), it labels the group's entries before any business-name marker: a group listed in the target network is a target entry, one listed only in others is `other_network`. Names that state no type are ignored. | It is the schema's own network field. Health Net leaves it empty and Ambetter's "Exchange Core National" states no type, so no current result changes; it matters for payers that fill it, and for files published under the 2027 rule (one file per network). |
| R2.31 | **Excluded copies are not conflicts.** In the parallel-contract test (step 5), copies from entries step 2 excluded (other network, other product) are set aside when the pair × key also has copies that could be in the target network. | A rate in another network is a different contract, not a conflict. Found with a synthetic file (one provider in a "... PPO" and a "... HMO" group); Health Net: 0 pair × keys affected. |
| R2.32 | **Any target network** (`--network`, default PPO) in C2 final and C2b steps 2, 5 and 6. Markers carry a network type (`business_name_labels.csv` `network_type`, `out_of_scope`); plan codes carry one (`network_codes.csv` `network_type`: SAL = HMO, EOA its own hybrid type). Outputs follow the target (`c2b_hmo_rates`); PPO keeps every current name. Unmarked unconflicted rates are kept for PPO (A9) and dropped for other networks unless `--unmarked keep`. | A9 keeps unmarked rates because PPO is the widest network, which does not hold for others. Health Net CA with HMO: 723 HMO plans; 7 rates kept (the SIMNSA entries), 48,045 unmarked rates dropped as `no_target_evidence`. PPO outputs unchanged. |
| R2.33 | List columns built by aggregation (`group_ids`, `entry_labels`, `group_labels`, `group_hints`) are sorted. | They were in arbitrary order, so two runs of the same code could differ; now every output is identical run to run. |

| Result | Rows | Raw price rows | TIN–NPI pairs | NPIs | TINs | Provider × service rates |
|---|---|---|---|---|---|---|
| All (before decisions) | 51,392 | 1,511,720 | 3,780 | 3,735 | 2,888 | 35,592 |
| Dropped: excluded entry | 91 | 109 | 42 | 42 | 32 | 90 |
| Dropped: capitation | 368 | 368 | 74 | 72 | 34 | 354 |
| Dropped: expired | 0 | 0 | 0 | 0 | 0 | 0 |
| Dropped: parallel contract | 2,706 | 13,007 | 317 | 317 | 195 | 864 |
| **Kept as PPO** | **48,227** | 1,498,236 | **3,729** | **3,685** | **2,849** | **34,284** |

Kept rows by evidence: `ppo_marker` 182, `unconflicted_rate` 48,045. By copy status: single 26,042, identical copies 22,059, within-group variants 126 (left to C4). By price type: RC institutional 35,218, CPT professional 10,916, HCPCS professional 2,093. 762 billing codes, 2,685 provider groups. 51 TIN–NPI pairs are lost in all (42 excluded entries, 9 with only parallel contracts); capitation drops prices, not whole providers.

### 3.5b C2 result

`pipeline/c2_final_plans.py` writes `output/c2/c2_plan_network.csv`: one row per plan with `network_class`, `network_type`, `evidence` and `rule`.

| Class | Plans | Evidence |
|---|---|---|
| PPO | 456 | name_word 452, name_embedded 1, name_word_epo_design 3 (A8) |
| Non-PPO | 856 | name_word 725 (HMO 719, POS 6), plan_code_sourced 131 (EOA 127, SAL 4) |
| Unlabelled | 0 | – |

All 456 PPO plans use one rate file, `healthnet-ca`, which is shared with the 856 non-PPO plans; the PPO rates inside it are `c2b_ppo_rates` (step 6).

### 3.6 Conflicts

| Conflict | Handling |
|---|---|
| A plan name says PPO and another type (HMO, POS) | Listed in both sheets as a conflict and left for a decision (R2.5). None in California. |
| A plan name says PPO and EPO only | PPO-network plan with an EPO benefit design (A8, R2.5). |
| A plan code says non-PPO but the name says nothing about the network | The code table decides, with its source (R2.3, A7). |
| A provider group lists PPO-marked and excluded entries together | The marker labels the **entry**, not the group (R2.13, R2.25). |
| The same provider and service priced at different rates by different groups | Parallel contract: ambiguous, dropped (A9, R2.24). Whatever remains with several rates is resolved in C4 (Section 4). |
| A price is in a group that also holds PPO-marked entries | Not enough: group membership alone is never evidence (R2.25). |

---

## 3C. File quality score (C3)

**Status:** built and run on all 4 index files and 33 rate files (2026-10-18). Scripts, in order: `pipeline/c3_reference.py`, `rate_scan.py` (shared scan, R2.28), `c3_metrics.py` (rate files, 3C.4, 3C.6), `c3_index_metrics.py` (index files and dataset scores, 3C.7–3C.8), `c3_diagnose.py` (problems ranked by severity, with fixes, 3C.9). `c3_report.py` writes deliverable 3 from these outputs: `deliverables/03_file_quality_metrics.md` (every metric, how it is calculated and why, and the score of every file) and `deliverables/03_file_quality_scores.xlsx` (the same definitions and every score).

### 3C.1 What is scored and against what

| Item | Decision |
|---|---|
| Datasets | Each Centene index file together with the rate files it links: Health Net (2 rate files), Ambetter (28), Fidelis (2), QualChoice (1, unreachable). The PPO and California filters do not apply to C3. |
| Unit | Every metric is computed per rate file, then rolled up per dataset, so one bad file is not hidden by the others. |
| Legal basis | 45 CFR §147.212 as in force on 1 Oct 2025. The new rule (91 FR 63748, 6 Oct 2026) keeps that version in force until 6 Mar 2027 (§147.212(c)(1)), so it governs our files. |
| Schema basis | The CMS schema at the version each file declares, read from [CMSgov/price-transparency-guide](https://github.com/CMSgov/price-transparency-guide) at that tag: v2.0.0 for 31 rate files and all 4 index files, v1.0.0 for Ambetter OH. The latest (2.2.1) is not required and is not scored. |
| Identifier rules | NPI: [CMS NPI check-digit specification](https://www.cms.gov/Regulations-and-Guidance/Administrative-Simplification/NationalProvIdentStand/Downloads/NPIcheckdigit.pdf). EIN prefixes: [IRS list of valid EIN prefixes](https://www.irs.gov/businesses/small-businesses-self-employed/how-eins-are-assigned-and-valid-ein-prefixes). |

### 3C.2 Scoring rules

| Rule | Rule text | Reason |
|---|---|---|
| R3.1 | Each metric gives numerator, denominator, value and a score from 0 to 1 (higher is better). "Share of bad" metrics score 1 − share. | One scale for every metric, and the raw counts stay visible. |
| R3.2 | **Gate:** M1 Reachable (HTTP 200 → 1, otherwise 0). File score = M1 × Σ(group weight × group score) ÷ Σ(weights of groups with at least one measured metric). | A file nobody can download has no usable data, whatever its index looks like. |
| R3.3 | A group score is the plain average of its measured metrics (additive, not multiplicative). | Multiplying lets one weak metric wipe out a group and penalizes groups for having more metrics. Averaging gives partial credit and is easy to explain. |
| R3.4 | A metric that cannot be measured for a file (e.g. no professional prices, no Medicare match) is left out and the weights are re-normalized. It is listed in the output with the reason. | Scoring it 0 would punish the file for our missing reference data; scoring it 1 would reward it. |
| R3.5 | Group weights: Timeliness 10%, Compliance 45%, Plausibility 25%, Usability 20%. | Compliance (what the rule and schema require) carries most weight; Plausibility next, because wrong values mislead negotiators; Usability and Timeliness follow. |
| R3.6 | Requirements and guidance are kept apart: Compliance holds only what the rule or schema enforces; CMS README guidance (M25–M27) sits in Usability, tagged `guide`. | A file is not marked non-compliant for breaking an unenforced convention. |
| R3.7 | Outliers use IQR fences (Q1 − 1.5·IQR, Q3 + 1.5·IQR) on log rates; extremes beyond 3·IQR are reported. | Rates scale multiplicatively: raw rates per code are right-skewed (Health Net CA median skewness 1.13) and log rates nearly symmetric (−0.16), which the fences assume. On a raw scale the lower fence often falls below \$0, so low outliers could never be flagged. On Health Net CA the 1.5·IQR fence flags 3.9% of prices, 3·IQR 0.8%, and a MAD z-score above 3.5 flags 2.5% (cross-check). |
| R3.8 | Freshness curves: score 1 within the 31-day update cycle, then `e^(−(d−31)/10)`. | The rule requires monthly updates. After that the score drops fast (a week late 0.50, a month late 0.05) but never reaches 0. The 31-day cycle becomes 92 when quarterly updates apply (1 Apr 2027). |
| R3.9 | Rates for one provider and service that differ between provider groups count against the file only when `network_name` does not tell the groups apart (M24). | One file can serve several networks and products, so different rates can all be correct; the defect is that the file does not say which applies. |
| R3.10 | M22 provider coverage is reported, not scored. | It measures how broad the network is, not whether the file is right: a narrow network published perfectly scores low, and the denominator includes every NPPES provider in the state. Measured values were 0.0003–0.37 (median 0.21), so scoring it only lowered every file by 1.6–2.5 points. |

### 3C.3 Identifier validity (M10, M11)

| Identifier | Valid | Not an error | Invalid |
|---|---|---|---|
| NPI | 10 digits, first digit 1 or 2, Luhn check digit with the constant 24 (for the `80840` prefix); also the 15-digit form `80840` + NPI (prefix stripped, then checked) | `0`: a contract made at TIN level only (CMS schema); counted separately | Any other length or a failed check digit |
| TIN (`ein`) | 9 digits as `NN-NNNNNNN` or `NNNNNNNNN`, first two digits on the IRS prefix list | 8 digits: padded with a leading zero, then checked, and counted as a formatting issue | Other lengths, extra digits (the schema's EIN pattern is unanchored, so `12-34567890` passes the schema but fails here), all zeros, prefix not on the IRS list |
| TIN (`npi`) | A valid NPI by the NPI rules | – | Otherwise |

Observed across the 32 downloadable rate files: 4,329,244 TIN entries, all `ein` and all `NN-NNNNNNN`; 51,480,972 ten-digit NPIs, 3,578 zeros, 2 nine-digit NPIs (Fidelis, invalid), no 15-digit forms. Health Net CA: all 3,734 real NPIs pass the check digit; 9 of 2,888 TINs have prefixes not on the IRS list (00, 07, 08, 09, 28, 29).

### 3C.4 Rate-file metrics

**Gate**

| # | Metric | Definition |
|---|---|---|
| M1 | Reachable | GET on the URL from the index returns HTTP 200 → 1, otherwise 0 |

**1. Timeliness (10%)**

| # | Metric | Definition | Basis |
|---|---|---|---|
| M2 | Freshness | `d` = run date − `last_updated_on` (days; HTTP `Last-Modified` if the field is missing). 1 if `d ≤ 31`, else `e^(−(d−31)/10)` | Rule (b)(4) |
| M3 | Index-to-file lag | `L` = index `last_updated_on` − file `last_updated_on` (days; ≤ 0 when the file is newer). Same curve as M2 | Quality |
| M4 | Contract currency | Prices with a valid `expiration_date` on or after the run date ÷ prices (share of `9999-12-31` reported) | Rule (b)(1)(i)(C)(2) |

**2. Compliance (45%)**

| # | Metric | Definition | Basis |
|---|---|---|---|
| M5 | Required fields | Objects with every required field present, non-empty and of the right type and format (dates `YYYY-MM-DD`, numbers numeric) ÷ objects checked (header, items, rate entries, prices, provider references, providers, TINs), per the declared schema version | Schema |
| M6 | Valid enumerations | Values in the allowed set ÷ values checked: `negotiation_arrangement`, `billing_code_type`, `negotiated_type`, `billing_class`, `setting`, `tin.type`, plan ID type and market | Schema |
| M7 | Conditional requirements | Objects meeting the condition ÷ objects it applies to: `service_code` on professional prices; `business_name` on `ein` TINs; `plan_sponsor_name` when the plan ID type is `ein` | Schema |
| M8 | Reference integrity | Provider reference IDs used in rate entries that resolve to a provider group ÷ IDs used (unused groups reported) | Schema |
| M9 | Valid billing codes | Items whose code fits its type and that have a description ÷ items. CPT: 5 digits, or 4 digits + F (Category II), T (Category III), U (PLA) or M (administrative MAAA). HCPCS: Level II letter + 4 digits on the CMS list (D codes are CDT, which CMS leaves out of its file, so they are accepted), or a CPT-form code. RC 4 digits; MS-DRG 3 digits; HIPPS 5 characters; NDC 11 digits | Rule (b)(1)(i)(B) |
| M10 | Valid NPIs | NPI entries valid by 3C.3 ÷ NPI entries, excluding `0` | Rule (b)(1)(i)(C) |
| M11 | Valid TINs | TIN entries valid by 3C.3 ÷ TIN entries | Rule (b)(1)(i)(C) |
| M12 | Valid place of service | Codes on the CMS place-of-service list, or `CSTM-00` ÷ codes | Rule (b)(1)(i)(C), Schema |
| M13 | Dollar rate types | Prices typed `negotiated`, `fee schedule`, `derived` or `per diem` ÷ prices | Rule (b)(1)(i)(C)(1): dollars, percentages only where no dollar amount can be set |

**3. Plausibility (25%)**

| # | Metric | Definition | Basis |
|---|---|---|---|
| M14 | \$0 / negative rates | 1 − prices with `negotiated_rate ≤ 0` ÷ prices | Schema (rate > 0), Quality |
| M15 | Outliers vs Medicare | Dollar professional prices with a Medicare fee (code + modifier, the file state's locality, facility or non-facility by place of service): `ln(rate ÷ Medicare)`, pooled over the file; 1 − share outside the R3.7 fences | Quality |
| M16 | Internal outliers | Per price key (code type + code + additional information + billing class + modifier + rate type) with at least 10 prices, on deduplicated prices: `ln(rate)`; 1 − share outside the R3.7 fences | Quality |
| M17 | Provider validity | Distinct NPIs (excluding `0`) found in NPPES and not deactivated ÷ distinct NPIs | Quality |
| M18 | Entity type vs billing class | 1 − institutional price entries whose provider lists only individual (Type 1) NPIs ÷ institutional price entries | Quality |

**4. Usability (20%)**

| # | Metric | Definition | Basis |
|---|---|---|---|
| M19 | Duplicate rows | 1 − (raw price rows − distinct group × price key × rate rows) ÷ raw price rows | Quality |
| M20 | Repeated items | 1 − codes (type + code + arrangement) in two or more items ÷ distinct codes | Quality |
| M21 | Code coverage | Σ weight of HiLabs codes priced ÷ Σ weight of all 583; a code's weight is the sum of its specialty weights in `Spec-Proc Weights` | Quality |
| M22 | Provider coverage (**reported, not scored**, R3.10) | Active NPIs in the file with an in-state practice address and a HiLabs-specialty taxonomy ÷ active NPPES providers in that state with those taxonomies. | Quality |
| M23 | Network named | Provider groups with at least one non-empty `network_name` ÷ groups (the schema allows `[]`) | Quality |
| M24 | Unexplained rate conflicts | 1 − provider × service keys priced at different rates in groups whose `network_name` does not tell them apart ÷ keys priced in two or more groups (R3.9) | Quality |
| M25 | File naming | Name matches `<YYYY-MM-DD>_<entity>_<plan>_in-network.json` → 1 / 0 | Guide |
| M26 | Bundle and capitation code lists | `bundle` items with `bundled_codes` and `capitation` items with `covered_services` ÷ such items | Guide |
| M27 | Percentage format | `percentage` prices given as whole percents (1 < value ≤ 100) ÷ percentage prices | Guide |

**Reported, not scored:** M22 provider coverage; bytes per distinct rate; extreme-outlier counts for M15 and M16; TIN-level contracts (`npi = 0`); unused provider groups; share of `9999-12-31` end dates; declared schema version; every unmeasured metric with its reason.

### 3C.5 Problem-statement terms

| Term | Metrics |
|---|---|
| Code coverage | M21 |
| Provider coverage | M17 (scored), M22 (reported) |
| \$0 rates | M14 |
| Outlier rates | M15, M16 |
| Rate types | M13, M27 |
| Duplicates | M19, M20 |
| Staleness | M2, M3, M4, deactivated NPIs in M17 |

### 3C.6 Results (run 2026-10-18)

Reference data: CMS PFS RVU26D (October 2026, conversion factor 33.4009, state-average GPCI), CMS Alpha-Numeric HCPCS October 2026, CMS place-of-service code set, NPPES October 2026 plus the 2026-10-12 deactivation report, HiLabs workbook. Scan of all 32 downloadable files: about 2 minutes on 18 workers; metrics: 40 seconds. Per-metric values, notes and scores: `output/c3/c3_rate_file_metrics.csv`; file scores: `c3_rate_file_scores.csv`; dataset roll-up: `c3_dataset_scores.csv`.

| Dataset | Rate files | Reachable | Timeliness | Compliance | Plausibility | Usability | File score (mean, min–max) |
|---|---|---|---|---|---|---|---|
| Ambetter | 28 | 28 | 1.000 | 0.999 | 0.983 | 0.73 | 0.948 (0.917–0.975) |
| Health Net | 2 | 2 | 1.000 | 0.928 | 0.968 | 0.43 | 0.851 (0.849–0.853) |
| Fidelis | 2 | 2 | 1.000 | 0.932 | 0.978 | 0.37 | 0.839 (0.836–0.841) |
| QualChoice | 1 | 0 | – | – | – | – | 0 (gate: HTTP 404) |

**What separates the files** (spread across the 32 reachable files): M23 network named (0 for Health Net and Fidelis, 1 for Ambetter), M19 duplicates (0.025–1.0; Health Net and Fidelis are 96–97% duplicate rows), M24 unexplained conflicts (0.16–0.92; Ambetter groups all carry the same network name, so differing rates for one provider cannot be told apart), M9 billing codes (0.34–1.0; Health Net and Fidelis revenue codes lost their leading zeros), M21 code coverage (0.52–0.91). Eleven checks (M2–M8, M10, M12, M14, M18) are at or near 1.0 for every file: these files meet the enforced schema, so compliance does little to separate them.

**Checks made while validating the numbers:**
- M24 was confirmed on Ambetter DE: one provider has five rates for CPT 99213 (\$100.19–\$148.40) across eight groups, all named "Exchange Core National".
- M24 is computed on provider membership signatures (providers in the same set of groups share every answer): the same counts as the direct join, about 10,000 times faster.
- Empty price-key parts are coalesced to `''`; with NULLs, equal keys never joined and M16 was unmeasured everywhere.
- M21 for Health Net CA (75.6% of HiLabs weight) matches the earlier independent count.
- M15 fences are relative to the file's own spread of rate ÷ Medicare (median ratio 1.20x in Ambetter DE, 1.34x Health Net CA, 2.42x Health Net OR). A file uniformly far from Medicare would still score well; M15 measures consistency, not level.

**Findings:** F46 (the Ambetter KS file's providers practise in Kentucky), F47 (TINs with prefixes the IRS never issued, on individual clinicians; consistent with SSNs, which CMS says must be replaced by the NPI), and M25 (file naming) is not measurable: the CMS naming standard covers only single-plan files, and every Centene rate file is listed in an index.

### 3C.7 Index-file metrics

Same frame as the rate files: gate, then four groups, each the plain average of its measured metrics (R3.3–R3.4). An index holds no prices, so Plausibility is replaced by **Consistency**: does the index agree with the files it links?

| Rule | Rule text | Reason |
|---|---|---|
| R3.11 | Index group weights: Timeliness 10%, Compliance 45%, Consistency 15%, Usability 30%. | Set by the team (2026-10-18). |
| R3.12 | I7 accepts the forms §147.212(b)(1)(i)(A) allows: the 14-character HIOS plan ID, the 5-digit HIOS issuer ID, or the EIN. A 10-character HIOS product ID is not an allowed form. Issuer-level IDs are accepted because we cannot tell whether a 14-character ID was available. | The rule names these three forms in order. Evidence that more specific IDs exist: CMS's 2026 Plan Attributes file lists 14-character plan IDs for 51 of Ambetter's 119 product IDs (it covers HealthCare.gov states only). |
| R3.13 | I11 state match: a linked rate file passes when the state where most of its providers practise equals the plans' state. Plans' state: the state code in the HIOS ID (valid US codes only), else the C1 state. Provider states: every state an NPI practises in, from the CMS Doctors and Clinicians file (2026-10-02) when the NPI is there, otherwise from NPPES primary and secondary practice locations. The top state by each source family is reported; a disagreement is flagged. | NPPES addresses are often stale (problem statement), so NPPES is used only where the Medicare file has no record. A state, unlike a street address, is robust: it is aggregated over thousands of providers. "Most providers" rather than "over half" passes border networks (Ambetter NE: 37% in Nebraska, still the largest state). |
| R3.14 | Dataset score = index score^0.3 × (mean rate-file score)^0.7, a weighted geometric mean; an unreachable rate file counts 0, and the index score already includes its gate (I1). | The index and the rate files are links in one chain: a plan's rate is right only if the index sends it to the right file and the file's data are good, so the two multiply and a zero in either gives 0. The exponents set relative influence (a 1% drop in the rate-file score costs 0.7%, in the index 0.3%), because the index only maps plans to files while the rate files hold every price. Exponents summing to 1 keep the 0–1 scale: an index and files both at 0.9 give 0.9. Replaced the weighted average 0.3 × index + 0.7 × files (2026-10-18), which gave QualChoice 0.279 for an index that points to no data. |

**Gate:** I1 Reachable: the index URL at Centene answers HTTP 200 or 206 (a one-byte range request).

| # | Metric | Group | Definition | Basis |
|---|---|---|---|---|
| I2 | Freshness | Timeliness | Same curve as M2 | Rule (b)(4) |
| I3 | Required fields | Compliance | Root, reporting structures, plans, file locations: share complete, averaged over object types | Schema (table of contents, declared version) |
| I4 | Valid enumerations | Compliance | `plan_id_type` ∈ {ein, hios}; `plan_market_type` ∈ {group, individual} | Schema |
| I5 | Conditional requirements | Compliance | `plan_sponsor_name` on EIN plans; each structure links an in-network or allowed-amount file | Schema |
| I6 | Plan ID matches its type | Compliance | EIN: 9 digits with an IRS prefix. HIOS: 5 digits, 10 characters (product) or 14 characters (plan, optional `-NN`), with a valid state code | Schema, Rule (b)(1)(i)(A) |
| I7 | Plan ID at the required level | Compliance | R3.12 | Rule (b)(1)(i)(A) |
| I8 | Out-of-network file linked | Compliance | Structures with an `allowed_amount_file` ÷ structures | Rule (b)(1)(ii) |
| I9 | Linked rate files reachable | Consistency | Distinct in-network links answering HTTP 200/206 ÷ links | Rule (b)(3) |
| I10 | Allowed-amount files reachable | Consistency | Same, for allowed-amount links | Rule (b)(3) |
| I11 | State match | Consistency | R3.13 | Quality |
| I12 | Plan ID consistency | Consistency | Plan IDs that always carry the same type, market and issuer ÷ plan IDs | Quality |
| I13 | Duplicate plan rows | Usability | 1 − exact duplicate plan rows (same plan fields and same linked files) ÷ rows | Quality |
| I14 | Unambiguous plan-to-file mapping | Usability | Plans listed in only one reporting structure ÷ plans | Quality |
| I15 | Secure links | Usability | HTTPS file locations ÷ locations | Quality |

### 3C.8 Index and dataset results (run 2026-10-18)

Outputs: `output/c3/c3_index_metrics.csv`, `c3_index_scores.csv`, `c3_dataset_scores.csv` (the rate-file roll-up alone is `c3_rate_file_rollup.csv`). Dataset score = index^0.3 × rate files^0.7 (R3.14).

| Dataset | Index score | What fails in the index | Rate files (reachable) | Mean rate-file score | Dataset score |
|---|---|---|---|---|---|
| Ambetter | 0.924 | I7 0/1,120 (all 10-character product IDs); I11 27/28 (KS) | 28 (28) | 0.948 | **0.941** |
| Health Net | 0.986 | I6 and I7 1,538/1,693 (154 HIOS IDs labelled `ein`, F5; 1 product ID) | 2 (2) | 0.851 | **0.890** |
| Fidelis | 0.920 | I7 0/94 (product IDs); I6 88/94 (`25303EP000`: "EP" is not a state, F10) | 2 (2) | 0.839 | **0.862** |
| QualChoice | 0.929 | I9 0/1 (rate file 404); I7 223/308 (85 product IDs) | 1 (0) | 0 | **0** |

Every index passes I1–I5, I8, I10, I12–I15: all fields present, no conflicting or duplicate plan rows, no plan in two structures, all links HTTPS, all allowed-amount files reachable.

**I11 detail.** Ambetter KS: 43 Kansas plans link to a file whose providers are mostly in Kentucky (KY 17,316, IN 1,536, OH 932 provider-state pairs). 15,634 of its 20,543 NPIs are in the Medicare file, and the Medicare file and NPPES agree on Kentucky, so stale NPPES addresses do not explain it (F46). Fidelis NY, Health Net CA and OR pass with both sources agreeing.


### 3C.9 Diagnosis: why each dataset scored what it did

`pipeline/c3_diagnose.py` turns the metric tables into a ranked list of problems per dataset with explanations and fixes. It is template-based and deterministic: no model, so the same inputs always give the same report. Outputs: `output/c3/c3_diagnosis.md` (report) and `c3_diagnosis.csv` (one row per dataset × problem).

| Rule | Rule text | Reason |
|---|---|---|
| R3.15 | Points lost per metric = group weight ÷ weights of measured groups × (1 − metric score) ÷ measured metrics in the group (the metric's exact cost to its file or index score). Carried to the dataset score by its first-order effect: index metric 0.3 × dataset ÷ index score × points; rate-file metric 0.7 × dataset ÷ mean file score × points ÷ number of files. Shown as dataset points (0–100). | Ranks problems by how much of the score they actually cost, from the same formulas that produce the score. |
| R3.16 | Severity: High ≥ 2.0 dataset points, Medium ≥ 0.5, Low below; under 0.01 points listed together as minor. Floors: an unreachable index or rate file (I1, M1, I9) is Critical, a state mismatch (I11) at least High. When the dataset score is 0, other problems are ranked by index points. | Points alone underrate defects that make a plan's rates wrong or missing but touch few files (Ambetter KS is 1 of 28 files: 0.04 points). |
| R3.17 | Each metric has a fixed template: the problem, why it matters, the fix for the publisher, the fix for our pipeline. Numbers come from the metric tables; evidence comes from fixed queries on the scan tables and index files (example codes, TINs, a provider's conflicting rates, plan IDs). | Explanations stay consistent and checkable. A model-written summary can be added later on top, with every number checked against these tables. |

**Results (run 2026-10-18)**

| Dataset | Score | Top problems (severity, dataset points) |
|---|---|---|
| Ambetter | 0.941 | I7 product-level plan IDs (High, 2.29); I11 KS linked to a Kentucky network (High by floor, 0.04); M24 unexplained rate conflicts (Medium, 1.60; e.g. NPI 1972503456 has 59 rates for CPT 99213, \$41.35–\$153.35, across 111 groups); M19 duplicate rows (Medium, 1.04) |
| Health Net | 0.890 | M23 no network names (High, 2.68); M19 duplicate rows (High, 2.45); M9 malformed codes (High, 2.36; `104`, `ZZZZZ`, RC `110`); M26 capitation without code lists (Medium, 1.22); M21 code coverage (Medium, 0.99) |
| Fidelis | 0.862 | M23 (High, 2.88); M19 (High, 2.79); M24 (High, 2.36); I7 product-level plan IDs (High, 2.11); M9 (High, 2.08) |
| QualChoice | 0 | I9 and M1: its only rate file is a 404 (Critical); I7 85 product IDs (Medium, 0.62 index points) |

While checking the evidence, M9 was found to reject valid codes: CPT administrative codes with an M suffix (`0003M`) and dental D codes labelled HCPCS (`D1206`). Both are now accepted (M9 row above). Only Ambetter's M9 moved (by about 0.0003 per file); the dataset scores are unchanged to four decimals. Ambetter's remaining invalid codes are MS-DRG codes that lost their leading zero (`11` for `011`).

---

## 4. Rate selection (C4)

When one TIN + NPI + code + additional information has several distinct rates, C4 keeps one. The rules run as a funnel, each only on the keys still unresolved: network label, modifier (whole service first), place of service (office first), majority contract (the rate carried by the most provider groups), then the lowest rate, flagged `conflict_unresolved`. Every rate not kept goes to `c4_alternatives.parquet` with the reason.

**Input.** Code: `pipeline/c4_select.py`; outputs `output/c4/`.

| Rule | Statement | Reason |
|---|---|---|
| R4.1 | C4 reads the C2 final rate table `output/c2b/c2b_ppo_rates.parquet` (C2b step 6) by default, not the hand-exported `c2b_step4_kept_prices.parquet` snapshot. | C4 must run on what the pipeline produces. The snapshot was taken after step 4 and still held the 2,706 parallel-contract rows that R2.24 drops (50,933 rows vs 48,227); otherwise the columns are the same. |
| R4.2 | Parallel contracts are handled in two places: C2b step 6 drops them (R2.24), and C4 rule 4 (majority contract: the rate carried by the most provider groups) stays as a second check. | Decision 2026-10-18. Keeps C4 correct if R2.24 is relaxed (FW1) or C4 is pointed at an earlier table. On the step 6 output, rule 4 resolves 24 of 26,951 keys. |

Run on `c2b_ppo_rates` (2026-10-18): 48,227 rows → 26,951 keys; no conflict 26,322, modifier 279, place of service 138, majority contract 24, unresolved (lowest rate kept) 188 (0.7%).

---

## 5. (b) Mapping TIN → NPI → address

### 5.1 Sources

**Code:** `pipeline/run_c5.py`, `pipeline/c5/` (work packages WP0 to WP9); settings in `config/c5.yaml`. **Outputs:** `output/c5/`.

| Source | Adds | Weakness | Family |
|---|---|---|---|
| PTD provider references (C2b step 6) | The TIN ↔ NPI pairs to be located | No address | – |
| NPPES (primary, secondary locations, endpoints) | Self-reported practice addresses, NPI type, taxonomy, last update date | Roughly 80% of addresses stale; self-reported | `nppes_self` |
| CMS NPI Registry API (live) | The same NPPES record with its current date | Same content as NPPES, so not independent | `nppes_self` |
| CMS Doctors and Clinicians (2026-10-02) | Medicare practice addresses for individual NPIs, refreshed monthly | Medicare-enrolled clinicians only; no organization NPIs | `medicare` |
| California DCA licensee files | Licence address for boards that publish bulk files (medical, nursing, behavioral sciences, psychology, podiatry) | Licence address may be a mailing address or P.O. box; needs a manual download from the DCA Box share | `state_license` |
| CDPH facility register (2026-10-16) | Licensed California facility address per NPI | Needs a download; the C5 test set uses the same register as truth, so a run that reads it cannot be scored against that test set | `state_facility` |
| Allow-listed web pages (payer directory, organization sites) | Addresses read from provider pages | Only domains in the allow-list, only with probe URLs a person supplied; search-based discovery is off | `payer_directory`, `org_website` |
| NUCC taxonomy | Taxonomy code → specialty | None significant | – |

DCA, CDPH and web sources are optional: a run without them uses NPPES and Medicare only.

### 5.2 Linking rules

| # | Rule | Why |
|---|---|---|
| R5.1 | The targets are the TIN–NPI pairs in `c2b_ppo_rates` (the PPO population). Entity type comes from NPPES; deactivated NPIs are flagged, not dropped. | C5 locates exactly the providers C2 kept. |
| R5.2 | A **claim** is one address for one NPI from one source with that source's date. Claims from the same family are not independent evidence (R5.8). | Agreement only means something between sources that did not copy each other. |
| R5.3 | Addresses are normalized to USPS form and geocoded once with the Census batch geocoder; results are cached on disk. Equal normalized addresses become one **site**. P.O. boxes are flagged and never become sites; a city-only address gives a city-level claim only. | Sites make "the same address" comparable across sources. The cache makes a second run free and identical. |
| R5.4 | A **TIN site set** holds the sites supported by an organization NPI of the TIN, sites listed by at least 2 of the TIN's individual NPIs (consensus), and sites of a Medicare group that covers at least half of the TIN's NPIs. | The TIN is the practice; its validated sites show where its providers can be expected. |
| R5.5 | **Candidates** per pair: the NPI's own sites, always; the TIN's sites only when the TIN is small (at most 10 NPIs) or the NPI has no site of its own. | A large TIN has many sites; offering all of them to every NPI would guess. |
| R5.6 | **Score** = sum of weighted features, transparent and additive: independent families 0.40 (capped at the number needed for verified), fit with the TIN site set 0.25, freshness 0.20 (linear over 24 months), geocode match 0.05, county matches the TIN's county label 0.05, in California 0.05. | Each weight can be read and tuned; the audit sample (WP8) is the place to calibrate. |
| R5.7 | **Stale check:** a self-reported claim is stale when a fresher claim from another family points to a different site; a candidate is stale when it is over 100 km from every validated site of its TIN. Stale claims are written to `c5_stale_claims.parquet` with the contradicting claim. | NPPES addresses are often old; a fresher independent source outranks them. |
| R5.8 | **Independence:** NPPES primary, secondary, endpoint and the live registry are one family (`nppes_self`); Medicare, DCA, CDPH, payer directory and organization sites are each their own; web aggregators never count as independent of `nppes_self` because they copy NPPES. | Families are set in `config/c5.yaml`; changing them requires updating this file. |
| R5.9 | **Web and model evidence is last and bounded:** only allow-listed domains that passed a probe; deterministic extraction first (structured data, `<address>`), then batched Gemini calls under call and spend caps, with a quote check and name proximity; a domain that yields nothing is stopped. Off by default (`estimate_only`). | Keeps the output reproducible and every address traceable to a quote. |

### 5.3 What counts as a "verified" address

| Label | Condition |
|---|---|
| **verified** (strict) | At least 2 independent source families agree on the site and the newest evidence is at most 24 months old |
| **probable** | Supported but short of verified: one fresh source with a TIN-site match, or two families that are not fresh |
| **unverified** | One weak source, stale evidence only, or no candidate |
| **verified / probable / unverified (loose)** | Written as `label_loose`: one fresh family plus a TIN fit of at least 0.25 counts as verified. Reported next to the strict label, never instead of it. |

The strict label is the one used downstream (C6, C7).

### 5.4 Conflicts

| Conflict | Handling |
|---|---|
| Sources give different addresses for one NPI | Candidates are scored (R5.6); stale claims removed first (R5.7); the highest score wins and the others stay in `c5_candidates.parquet` and, ranked, in C6's address alternatives. |
| A state source disagrees with the winning address | The 50 largest-TIN cases are written to `c5_source_disagreements.csv` for review; the winner is not changed by hand. |
| Only one family has a claim | The pair cannot be `verified`; it is `probable` or `unverified` (R5.3 above). |
| No claim at all | The pair is `unverified` with no address and is queued in `c5_leftovers.csv`, ordered by TIN size. |
| An NPI is deactivated | Flagged in C5; removed from the C6 file (R6.6). |

**Result (final run 2026-10-18).** 3,729 TIN–NPI pairs (3,685 NPIs, 2,849 TINs). Claims: 13,059 from `nppes_self` and 464 from `medicare`; DCA, CDPH and web sources were not present, so only two families can agree. 9,137 sites, 92.7% matched by the Census batch geocoder. Labels per pair: **5 verified**, 1,045 probable, 2,679 unverified; per NPI: 5 verified (0.1%), 1,026 probable, 2,654 unverified. Under the loose profile 26.9% of NPIs are verified. Verification is rare because Medicare covers only 464 claims, so almost every pair has a single family; adding the DCA and CDPH files (5.1) is the lever.

### 5.5 Specialty assignment and the provider rate file (C6)
**Code:** `pipeline/c6_provider_rate_file.py`. **Outputs:** `output/c6/`.

| # | Rule | Why |
|---|---|---|
| R6.1 | Taxonomies come from NPPES (entity type, up to 15 codes), the primary one (switch = Y) first. | NPPES is the only source of taxonomy for our NPIs. |
| R6.2 | **Individuals (entity type 1):** the first taxonomy, primary first, found in the HiLabs crosswalk gives the specialty (code and description). Marked `hilabs_crosswalk`, or `..._secondary_taxonomy` when the primary was not in the crosswalk. | The crosswalk is HiLabs' own specialty list (F15: individuals only). |
| R6.3 | **Organizations (type 2), and individuals with no crosswalk match:** NUCC *Classification* of the primary taxonomy (`nucc_classification`). | F15: the crosswalk has no organization rows; NUCC covers every taxonomy. |
| R6.4 | One address per TIN–NPI: C5's chosen address and its label. Other address candidates (rank > 1) go to `c6_address_alternatives.parquet`; unchosen prices to `c6_rate_alternatives.parquet`. | Alternatives must not multiply rows (F23). |
| R6.5 | One row per C4 key (TIN × NPI × code × sub-code × class × setting × place of service × modifier). The main rate is C4's chosen rate. | The C4 selection rule is the single source of the price. |
| R6.6 | **Deactivated NPIs are removed** from the main file (7 NPIs, 48 rows, `c6_dropped_deactivated.csv`). | C3 diagnosis; a deactivated NPI cannot bill. |
| R6.7 | `quality_flags` (`;`-joined): `unresolved_rate_conflict` (C4 fell back to lowest rate), `no_address`, `address_unverified`, `non_dollar_type`, `specialty_unknown`, `invalid_npi` (the published NPI is `0`; 5 rows), `npi_not_in_nppes`. Rows are flagged, not dropped. | Users filter by flag; nothing is silently lost. |
| R6.8 | Excel view = rows on a HiLabs benchmark code (`Spec-Proc Weights`); the full file is Parquet and CSV. | Fits Excel's 1,048,576-row limit and shows the codes HiLabs weights. |

**Result (final run 2026-10-18):** 26,903 rows, 3,722 TIN–NPI pairs, 3,678 NPIs, 2,846 TINs, 762 codes; 6,322 rows on HiLabs codes. Address labels per pair: 1,045 probable, 2,667 unverified, **5 verified**; 118 rows without an address. Specialty: 619 NPIs from the crosswalk (+3 via a secondary taxonomy), 3,054 from NUCC, 0 unknown.

---

## 7. Cost-effectiveness (C7)

**Code:** `pipeline/c7_geocode.py`, `pipeline/c7_cost_effectiveness.py`. **Reference:** `reference/peer_specialty.csv`. **Outputs:** `output/c7/`.

### 7.1 What "similar" means
A price is compared only with prices for the **same service** (code, sub-code, billing class, setting, place of service, modifier) from providers of the **same entity type** (individual / organization), the same **peer specialty** and the same **region**. Rates are never used to build the groups, so the comparison is not circular.

| # | Rule | Why |
|---|---|---|
| R7.1 | Every unique C5 practice address is geocoded once with the Census batch geocoder (`geographies`), which returns coordinates and county FIPS in the same call. No match: mean coordinates of matched addresses with the same ZIP5, then ZIP3. The method is kept per row (`census_batch` 2,740, `zip5_mean` 116, `zip3_mean` 14, none 4 of 2,874 addresses). | One call gives both; no separate county step. The C5 cache was built offline (all unmatched), so C7 does its own run. |
| R7.2 | California addresses are clustered with HDBSCAN (haversine, leaf selection, min cluster 40, min samples 10). Unclustered points join the nearest cluster centre within 75 km, otherwise `CA-remote`. Non-CA addresses are `out_of_state`; no coordinates is `unknown`. A region is named after its commonest city. | Counties are very uneven (Los Angeles vs rural); density clusters give metro areas. 19 regions result. Regions label and report providers; the peer search itself uses distance (R7.4). |
| R7.3 | `reference/peer_specialty.csv` maps each of the 97 C6 specialty labels to a `peer_specialty` (69) and a coarser `peer_family` (16). Differently spelled labels for one specialty are merged ("Marriage & Family Therapist" and "Marriage and Family Therapist"). | HiLabs crosswalk names and NUCC classifications name the same specialty differently. A reviewed table is easier to defend than distances between embeddings. |
| R7.4 | **Peers are found by a widening search.** For each price, peers are prices from **other TINs**. The search widens until it holds **at least 5 providers (NPIs) from at least 3 TINs** (`--min-npi`, `--min-tin`). Inside one service level the provider scope widens: same peer specialty within 25, 50, 100, 200 km, then statewide, then peer family within 100 km, family statewide, then everyone statewide. Only if that fails does the service level relax, in this order: S0 exact key (code, sub-code, billing class, setting, place of service, modifier, same entity type); S1 ignore descriptive modifiers (R7.9); S2 any place of service and setting; S3 any entity type; S4 any sub-code (code, billing class and price modifier must still match); G1 same **code family**; G2 same **code section** (R7.10). The level, scope and basis reached are stored per price (`peer_service_level`, `peer_scope`, `peer_basis`). | Service comparability comes first, then similarity of the provider. Excluding the provider's own TIN stops NPIs that share one contract counting as many peers and keeps the provider out of its own median. |
| R7.9 | Modifiers that change the price (22, 26, 50-56, 62, 66, 80-82, AS, TC, RR, NU, UE, NR) must always match. Descriptive ones (95 telehealth, 90 reference lab, QW CLIA waived, JZ, 98 and others) are ignored from level S1. | 26 and TC are parts of a service and RR/NU/UE are rent / new / used equipment, so mixing them compares different things. The descriptive ones do not change what is bought. |
| R7.10 | **Code families.** Codes with too few providers are grouped: CPT by AMA sub-section (office E&M, hospital E&M, chest imaging, chemistry labs, ... 46 families in 7 sections), HCPCS by letter and first digit (`E0`, `J1`), revenue codes by first 3 digits. Dollar rates of different codes cannot be compared, so G1 and G2 compare **rate as a share of the Medicare fee** (R7.5) and only for prices that have a Medicare fee. Their weight is 0.5 (family) and 0.35 (section) times the scope weight. | A code with no peers can still be judged against similar services if both are put on one scale. Medicare is the one scale available for every professional code. |
| R7.11 | **Regional average, last resort.** A price with no peers at any level takes the weighted geometric mean ratio of compared prices in its region (same peer family and billing class, else the region, else the state), is flagged `imputed` (`peer_basis` = `regional_average`) and gets 0.2 of its normal weight. A provider whose only prices are imputed is labelled `regional_average_only`, never typical, lower or higher; imputed prices do not count toward `n_codes` or the 3-price minimum. | The price stays visible and the provider is not dropped, but a number with no evidence behind it is not allowed to produce a verdict. The search also gained an `all_100km` scope: everyone within 100 km before everyone statewide. |
| R7.5 | Per price: `ratio` = rate / peer median, clipped to 0.1-10; above P75 and below P25 flags. Professional CPT/HCPCS prices also get `pct_medicare` = rate / CA 2026 Medicare fee (non-facility for place of service 11, otherwise facility, matching modifier). | A ratio to the median is scale free. Medicare gives an outside anchor. |
| R7.6 | Row weight = HiLabs specialty-procedure weight (for individuals with a HiLabs specialty), else HiLabs code weight, else a floor; divided by the median weight and clipped to 0.1-5; **multiplied by the peer weight: scope specialty 1.0, family 0.75, everyone 0.5, times service level S0/S1 1.0, S2 0.75, S3 0.5. | High-volume codes should drive the score; clipping stops one code from deciding it. The wider the search, the less the comparison counts. |
| R7.7 | Provider **cost index** = weighted geometric mean of the ratios, shrunk toward 1: `exp(ln(raw) * n/(n+5))` with n compared prices. Computed per TIN-NPI and per TIN. | Geometric mean so 2x and 0.5x cancel. A provider priced on one code should not rank at an extreme. |
| R7.8 | Label from the shrunk index: `lower_cost` (<= 0.85), `higher_cost` (>= 1.15), `typical`, or `insufficient_peers` (fewer than 3 compared prices). No score threshold beyond these cut-offs; the evidence columns (n prices, peer level, share above P75) travel with the label. | Same approach as R2.26: explicit evidence, no hidden threshold. |

### 7.2 Result (run 2026-10-18)
- **Prices:** 26,903; compared 24,899 (92.5%). Basis: same-code dollar rate 23,978, share of Medicare 921. Service level reached: S0 20,666, S1 427, S2 1,361, S3 145, S4 1,379, G1 599, G2 322. Scope reached: specialty within 25 km 6,317, 50 km 4,302, 100 km 3,213, 200 km 2,933, specialty statewide 3,685, family 1,288, everyone 3,161. **2,004 prices have no peers and take the regional average (R7.11)** (before the widening search 4,816; after it 3,361): labs, DME, drugs and revenue codes that have no Medicare physician fee and fewer than 5 providers for the code.
- **Providers:** 3,722 TIN-NPI pairs scored. Labels: typical 745, lower cost 274, higher cost 268, insufficient peers 2,313, regional average only 122. **Most providers are priced on one code (median 1)**, so `insufficient_peers` comes from fewer than 3 compared prices (`--min-codes`).
- **Checks:** median ratio is 1.0 at every scope; split-half Spearman 0.55; Spearman of cost index vs median % of Medicare 0.69 over 473 professional providers (0.34 with the first ladder; part of the rise is because G1/G2 prices are scored on % of Medicare).
- **Next reference files** to cover what is left: the CMS Clinical Laboratory Fee Schedule (labs), DMEPOS fee schedule (E and K codes) and ASP drug pricing (J codes).
- **Specialty table check:** `c7_specialty_suggest.py` embeds the 97 labels with `BAAI/bge-small-en-v1.5`. At cosine >= 0.85 it finds 3 pairs the table keeps apart (Primary Care vs Primary Care (Pediatric) 0.91, vs Primary Care - Midlevel 0.87, Neurology vs Psychiatry & Neurology 0.85); all three separations are deliberate (children, midlevel, neurology). None of the table's merges falls below cosine 0.60. The table is unchanged; the model only checks it.
- **Not done:** practice size and service mix as grouping features (optional extension).

---

## 8. Change log

| Date | Change |
|---|---|
| 2026-10-17 | File created. Scope recorded; assumption A1 added (commercial = group + individual). |
| 2026-10-17 | C1 built: rules R1.1–R1.10 and results added to Section 3.1. |
| 2026-10-17 | C2 steps 1–3: rules R2.1–R2.7, assumption A7 (EOA not PPO), Sections 3.3–3.4. |
| 2026-10-17 | R1.11: C1 writes a California-only plan file; C2 steps now read it. Section 3.2 filled. |
| 2026-10-17 | Decision: EOA stays non-PPO, flagged `uses_ppo_network` (R2.3a, A7 closed). |
| 2026-10-17 | C2b step 1: provider group tables built (R2.8–R2.11, section 3.5). |
| 2026-10-17 | Decision: drop the `uses_ppo_network` flag (former R2.3a); EOA is simply non-PPO. Work continues on the filtered PPO data only. |
| 2026-10-17 | Decision: plans named "PPO … EPO" count as PPO network (A8, R2.5). California: PPO 487, non-PPO 883, conflicts 0, unlabelled 0. |
| 2026-10-17 | EOA and SAL sourced from Health Net documents. A7 corrected: EOA is an HMO-based hybrid that uses the PPO network for specialists. SAL added as non-PPO; no California plan is unlabelled. |
| 2026-10-17 | **Organizer decision: commercial group plans only.** A1 confirmed as group-only; C1 `--market group` (R1.10, R1.11). In scope: 1,312 Health Net group plan rows, 1 rate file. C2: PPO 456, non-PPO 856 (HMO 719, EOA 127, POS 6, SAL 4), unlabelled 0. C2b step 1: 2,825 groups, 3,735 NPIs, 2,888 TINs, 3,780 pairs. Ambetter CA out of scope. |
| 2026-10-17 | C2b steps 2–4: business-name markers per TIN entry (R2.12–R2.15, A10), price table and capitation flag (R2.16–R2.17), contract-date check with the revenue-code sub-code in the price key (R2.18–R2.20). Corrects F27: 3.8% of parallel copies differ, not 31%. |
| 2026-10-18 | C2b step 5: prices deduplicated (97.3% exact copies, F38); parallel-contract test per TIN–NPI × key (R2.21–R2.23): 877 differing pair × keys over 318 pairs, mostly professional prices; PPO-marked entries are never priced by another group. |
| 2026-10-18 | Decision: strict route for parallel contracts (A9, R2.24): differing copies dropped, unconflicted rates kept as PPO. R2.25: a provider group is not assumed to be one network. Resolving conflicts moved to `docs/FUTURE_WORK.md` (FW1). |
| 2026-10-18 | C2 closed. C2b step 6: final PPO rate table `c2b_ppo_rates` (48,227 rows, 3,729 pairs, 34,284 provider × service rates) with drop reasons (R2.27). Confidence and threshold replaced by evidence columns (R2.26); `c2_plan_network.csv` gives every plan its class and evidence. |
| 2026-10-18 | C5 test set: 707 PPO TIN–NPI pairs with addresses from CDPH (406 facilities, held out from C5) and CMS Doctors and Clinicians (301 clinicians); 107 hard cases where NPPES is wrong. NPPES-only baseline 95.8% / 70.1%. `handoff/test_set_C5/`. |
| 2026-10-18 | C3 rate-file metric set final (Section 3C): gate M1 plus Timeliness 10%, Compliance 45%, Plausibility 25%, Usability 20%; 27 metrics M1–M27; scoring rules R3.1–R3.9; identifier rules from the CMS NPI specification and the IRS EIN prefix list. No C3 code yet. |
| 2026-10-18 | C3 rate-file metrics built and run on all 33 files (3C.6): Ambetter 0.948, Health Net 0.851, Fidelis 0.839, QualChoice 0. M22 made reported only (R3.10). |
| 2026-10-18 | C3 index metrics I1–I15 and dataset scores built (3C.7–3C.8, R3.11–R3.14): index weights Timeliness 10%, Compliance 45%, Consistency 15%, Usability 30%; I11 provider states from the CMS Doctors and Clinicians file first, NPPES as fallback. Dataset scores: Ambetter 0.941, Health Net 0.892, Fidelis 0.863, QualChoice 0.279. |
| 2026-10-18 | Dataset score changed to a weighted geometric mean, index^0.3 × rate files^0.7 (R3.14): Ambetter 0.941, Health Net 0.890, Fidelis 0.862, QualChoice 0. |
| 2026-10-18 | C3 diagnosis report (3C.9, R3.15–R3.17): template-based ranking of problems by severity with fixes; M9 now accepts CPT M codes and HCPCS D codes. |
| 2026-10-18 | Deliverable 3 written by `pipeline/c3_report.py`: `deliverables/03_file_quality_metrics.md` and `03_file_quality_scores.xlsx`, generated from the C3 outputs. |
| 2026-10-18 | Scalability (R2.28–R2.33): shared scan `rate_scan.py` replaces `c3_scan.py` and the C2b parsers (each rate file read once, two parallel passes; 59 s for all 32 files instead of about 2 minutes plus the C2b re-parse); `.json.gz`, URL streaming, batched Parquet, bounded caches, remote provider lists; `network_name` used first; excluded copies set aside in step 5; `--network` target with `--unmarked`; sorted list columns. Every C2, C2b and C3 table identical to before for PPO. |
| 2026-10-18 | C4 merged on `dev` with C3 and C5. C4 now reads `c2b_ppo_rates` (R4.1) instead of the step 4 snapshot; parallel contracts handled in both C2b step 6 and C4 rule 4 (R4.2). Generated C4 outputs untracked and ignored. |
| 2026-10-18 | Full C1–C5 run on `dev` in a clean venv from the merged `requirements.txt`: every step exits 0; C1–C3 outputs identical to the previous run (only timestamps, run times and paths differ); C4 26,951 keys; C5 24 tests pass and the pipeline completes offline (`--estimate --dry-run`). C5 config paths fixed to point inside the repo. |
| 2026-10-18 | `dev` tidied for push: C4 moved to `pipeline/c4_select.py` writing `output/c4/`; C5 reads its target TIN–NPI pairs from `c2b_ppo_rates` (same 3,729 pairs) instead of `handoff/data/rate_file_providers.csv`; one combined README; duplicate root index JSONs removed; `main.ipynb` ignored. |
| 2026-10-18 | C6 built: `pipeline/c6_provider_rate_file.py` joins C4 chosen rates, C5 addresses and specialty into `output/c6/c6_provider_rates.parquet` / `.csv` plus an Excel view and side tables (rules R6.1–R6.8, section 5.5). |
| 2026-10-18 | C7 built: `c7_geocode.py` (Census geocode, ZIP fallback) and `c7_cost_effectiveness.py` (regions by HDBSCAN, peer specialty table, peer ladder, cost index and labels), rules R7.1-R7.8, section 7. |
| 2026-10-18 | C7: `c7_specialty_suggest.py` checks `peer_specialty.csv` with a small embedding model (bge-small); no change to the table. |
| 2026-10-18 | C7 peer search changed from a fixed ladder to a widening search (R7.4): other TINs only, at least 5 peers from 3 TINs, distance radii 25-200 km, then statewide, family, everyone, then relaxed service level. Prices without peers 4,816 to 3,361; Medicare correlation 0.34 to 0.58. |
| 2026-10-18 | C7: code families (R7.10) and price-changing modifiers (R7.9) added to the peer search; prices without peers 3,361 to 2,004; Medicare correlation 0.70. |
| 2026-10-18 | C7: regional-average fallback for prices with no peers (R7.11) and an `all_100km` scope; label `regional_average_only`. |
| 2026-10-18 | Final run of the whole pipeline with one command (`run_pipeline.py`, 28 steps). Fixes found on the way: C5's Census batch parser read the response wrongly (0% matched; now 92.7%); C3 needs network for the reachability gate, so it was re-run after a DNS outage; `rate_scan.py --workers` lowered for a 16 GB machine; the CMS DAC file is read from `Data/`. Headline numbers: `output/RUN_SUMMARY.md`. README rewritten as documentation; LOGIC.md cleaned (sections 5.1 to 5.4 written, change log moved to the end) and rendered to `LOGIC.pdf` by `pipeline/make_logic_pdf.py`. |
| 2026-10-18 | Final deliverables collected in `deliverables/` (rate file, logic file, quality metrics, cost-effectiveness, video script, run summary) with an index README. |
