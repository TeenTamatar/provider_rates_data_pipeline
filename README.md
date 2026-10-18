# Centene PPO provider rates pipeline

HiLabs hackathon. The pipeline reads Centene's price transparency files and builds a provider-level PPO rate file for California commercial group plans: one row per **NPI × TIN × billing code × practice address × specialty × rate**, with a quality score for every source file and a cost-effectiveness score for every provider.

This document explains what each stage does and how. The rules behind every decision, with their reasons, are in [`LOGIC.md`](LOGIC.md) (also as `LOGIC.pdf`). The numbers of the latest run are in `output/RUN_SUMMARY.md`.

## Contents

1. [Run it](#1-run-it)
2. [Data sources](#2-data-sources)
3. [How the pipeline fits together](#3-how-the-pipeline-fits-together)
4. [C1: parse the index files](#4-c1-parse-the-index-files)
5. [C2: find the PPO network](#5-c2-find-the-ppo-network)
6. [C3: file quality scores](#6-c3-file-quality-scores)
7. [C4: one rate per provider and service](#7-c4-one-rate-per-provider-and-service)
8. [C5: practice address per TIN–NPI](#8-c5-practice-address-per-tinnpi)
9. [C6: the provider rate file](#9-c6-the-provider-rate-file)
10. [C7: cost-effectiveness](#10-c7-cost-effectiveness)
11. [Repository layout](#11-repository-layout)
12. [Conventions and tests](#12-conventions-and-tests)

---

## 1. Run it

Python 3.12.

```bash
pip install -r requirements.txt
```

Copy the organizer files into `Data/` (see [Data sources](#2-data-sources)), then run everything with one command:

```bash
python run_pipeline.py
```

It downloads every public input that is missing, then runs C1 to C7 in order and stops at the first failing step. A failure prints the command that resumes from that step. The final file is `output/c7/c7_final.csv`; the headline numbers are written to `output/RUN_SUMMARY.md` and `output/run_summary.json`.

| Option | Effect |
|---|---|
| `--list` | Print the 28 steps in run order |
| `--from c4` / `--to c3_report` | Start or stop at a step (full name or prefix) |
| `--only c6 c7_geocode` | Run only these steps |
| `--skip-download` | Skip the download steps (inputs already in `Data/`) |
| `--offline` | C5 runs `--estimate --dry-run` (no web or Gemini calls); C7 uses cached geocodes |
| `--scan-workers 4` | Parallel jobs for the rate file scan (default: CPUs − 2; lower it if memory is short) |
| `--run-date 2026-10-18` | Run date for the scan and C3 (default: today) |

C5's model step is off by default (`estimate_only: true` in `config/c5.yaml`), so no key is needed. To turn it on, set `estimate_only: false` and put `GEMINI_API_KEY` in `.env` (copy `.env.example`).

## 2. Data sources

`Data/` is git-ignored. Two sets of files:

**Provided by the organizers** (copy into `Data/` yourself)

| Local path | What it holds | Used by |
|---|---|---|
| `Data/2026-09-30_healthnet_index.json`, `2026-09-28_qualchoice_index.json`, `2026-09-28_fidelis_index.json`, `2026-09-28_ambetter_index.json` | Centene index files: plans and the rate files they link | C1, C3 |
| `Data/Rate Benchmarking Individual Weights_Hackathon.xlsx` | HiLabs taxonomy → specialty crosswalk, specialty weights, 583 benchmark codes | C3, C4, C6, C7 |

**Downloaded by `run_pipeline.py`** (or by hand from these links)

| Local path | Size | Source | Used by |
|---|---|---|---|
| `Data/NPPES_Data_Dissemination_October_2026_V2/` (`npidata_pfile_*`, `pl_pfile_*`, `endpoint_pfile_*`) | 1.1 GB zip, 11 GB unzipped | [NPPES October 2026 V2](https://download.cms.gov/nppes/NPPES_Data_Dissemination_October_2026_V2.zip) ([downloads page](https://download.cms.gov/nppes/NPI_Files.html)) | C3, C5, C6 |
| `Data/NPPES Deactivated NPI Report 20261012.xlsx` | 5.4 MB | [Deactivated NPI report](https://download.cms.gov/nppes/NPPES_Deactivated_NPI_Report_101226_V2.zip) | C3, C5, C6 |
| `Data/DAC_NationalDownloadableFile.csv` | ~900 MB | [CMS Doctors and Clinicians, dataset mj5m-pzi6](https://data.cms.gov/provider-data/dataset/mj5m-pzi6) | C3, C5 |
| `Data/nucc_taxonomy_261.csv` | 520 KB | [NUCC taxonomy v26.1](https://nucc.org/images/stories/CSV/nucc_taxonomy_261.csv) ([page](https://www.nucc.org/index.php/code-sets-mainmenu-41/provider-taxonomy-mainmenu-40/csv-mainmenu-57)) | C5, C6 |
| `Data/cms_ref/rvu26d/` | zip | [Medicare PFS RVU26D](https://www.cms.gov/files/zip/rvu26d-updated-08-26-2026.zip) ([PFS files](https://www.cms.gov/medicare/payment/fee-schedules/physician/pfs-relative-value-files)) | C3, C7 |
| `Data/cms_ref/hcpcs_2026_10/` | zip | [HCPCS October 2026](https://www.cms.gov/files/zip/october-2026-alpha-numeric-hcpcs-file.zip) ([quarterly update](https://www.cms.gov/medicare/coding-billing/healthcare-common-procedure-system/quarterly-update)) | C3 |
| `Data/rate_files/*.json` | 700 MB for Health Net CA; 32 files in all | Links in `output/c1/c1_rate_files.csv`, e.g. [Health Net CA](https://www.centene.com/content/dam/centene/Centene%20Corporate/json/DOCUMENT/2026-09-30_centene-management-company-llc_healthnet-ca_in-network.json) ([Centene price transparency](https://www.centene.com/price-transparency-files.html)); fetched by `pipeline/download_rate_files.py` | C2b, C3 |

Optional C5 sources, used when present: [California DCA licensee bulk files](https://www.dca.ca.gov/consumers/public_info/index.shtml) in `data/state_sources/dca/` and [CDPH healthcare facility locations](https://data.chhs.ca.gov/dataset/healthcare-facility-locations) in `data/state_sources/cdph/health_facility_locations.csv`.

Services called at run time: the [US Census Geocoder](https://geocoding.geo.census.gov/geocoder/Geocoding_Services_API.html) (C5, C7), and Gemini (C5, only with `GEMINI_API_KEY`).

## 3. How the pipeline fits together

```
index files ──► C1 plans table ──► C2 PPO plans (names, codes)
                    │                    │
rate files ──► shared scan ──► C2b PPO prices per provider ──► C4 one rate per key ──┐
                    │                                                                ├─► C6 provider rate file ─► C7 cost score
                    └──► C3 quality scores (index + rate files)     C5 address ──────┘
```

- **The index files** list plans and point to rate files. C1 turns them into a table.
- **A rate file** is shared by many plans, so the PPO network cannot be picked by choosing files. C2 decides which plans are PPO; C2b decides which prices inside the shared file are PPO prices.
- **The shared scan** (`rate_scan.py`) reads each rate file once and stores its tables in `output/scan/`. C2b and C3 read those tables; nothing parses a rate file twice.
- **C3** scores every index file and rate file for quality; it needs no PPO filter.
- **C4, C5, C6, C7** work on the PPO provider population that C2b leaves: C4 picks one rate per service, C5 finds each provider's address, C6 joins them with the specialty, C7 scores the cost.

Each stage writes to its own folder under `output/`.

## 4. C1: parse the index files

`pipeline/c1_parse_index.py` → `output/c1/`

1. Each index file is streamed one reporting structure at a time (`ijson`), so a multi-gigabyte index would not need more memory.
2. A structure lists plans and the rate files they use. The script writes **one row per plan × in-network rate file**, keeping every published field and adding derived ones next to them.
3. **Brand** comes from the index file name. **State** comes from the first available source: the rate file URL suffix, the HIOS state code, a state token in the plan name, or other plans of the same HIOS issuer. The source used is recorded.
4. The **plan ID type** is checked against the ID's shape (EIN = 9 digits; HIOS = 5, 10, 14 or 16 characters). The published type is kept, the inferred type and a mismatch flag are added.
5. Duplicate rows (same brand, name, ID, market and rate file) are removed and listed in `c1_duplicates_removed.csv`.
6. A rate file table has one row per unique URL with a HEAD check (status, size, last modified). Nothing is downloaded.
7. `c1_plans.csv` keeps every plan. `c1_plans_ca.csv` keeps the in-scope rows: California and market `group` (commercial group, confirmed by the organizers).

## 5. C2: find the PPO network

C2 has two parts: classify plans, then separate PPO prices inside the shared rate file.

### 5.1 Plan classification (`c2_step1` to `c2_step3`, `c2_final_plans.py`)

Each step sorts the in-scope plans by one kind of evidence; every rule is yes/no on explicit evidence.

1. **Step 1, PPO by name.** A plan name containing "PPO" anywhere, case-insensitive (so `CUSPPO` counts).
2. **Step 2, another network by name or code.** HMO, EPO or POS in the name (a letter may come before the word, not after, so `POST` does not match), or a brand plan code listed as non-PPO in `reference/network_codes.csv`: `EOA` (Elect Open Access, an HMO-based hybrid) and `SAL` (Salud HMO). The code table gives each code, its source and its network type, so every code can be changed.
3. **Step 3, no signal.** Everything the first two steps did not match, so the three sheets partition all plans.
4. **Final.** `c2_final_plans.py` writes `c2_plan_network.csv`: each plan with its class, the evidence that decided it and the rule. A name with PPO and EPO (but no HMO/POS) is a PPO-network plan with an EPO benefit design, so it stays PPO.

### 5.2 PPO prices inside the shared rate file (C2b)

All 456 PPO plans and the non-PPO plans point to one file, so the network is separated at the level of provider groups and single prices. Input is the shared scan.

| Step | Script | What it does |
|---|---|---|
| Scan | `rate_scan.py` | Streams every rate file once into Parquet tables: provider members, provider groups, prices, references. Reads `.json`, `.json.gz` or a URL. |
| 1 | `c2b_step1_groups.py` | Builds the provider group tables (one row per group member NPI) and a **member signature** per group: a hash of its sorted TIN–NPI pairs. Groups with equal signatures list exactly the same providers. |
| 2 | `c2b_step2_name_labels.py` | Reads network markers in TIN business names from `reference/business_name_labels.csv` (one regex per row): strong PPO markers (`PPO: <County>`), strong exclusions (Ambetter PPO/HMO entries, SIMNSA), hints that decide nothing. A marker labels the **TIN entry**, not its whole group. If a group's `network_name` states a type, that is used first. |
| 3 | `c2b_step3_capitation.py` | Builds the price table (one row per published price × group) and flags capitation prices, the HMO payment model. |
| 4 | `c2b_step4_contract_dates.py` | Flags expired prices and counts parallel copies: the same member signature and service priced by two or more groups. The price key includes the code, sub-code (`additional_information`), arrangement, rate type, class, setting, place of service and modifier. |
| 5 | `c2b_step5_parallel_copies.py` | Deduplicates prices (most rows are exact copies), then tests every **TIN–NPI × service** for parallel contracts: `single`, `identical_copies`, `within_group_variants`, `differing_copies`. |
| Export | `c2b_export_kept.py` | Writes the data left after steps 2 to 4. |
| 6 | `c2b_step6_ppo_rates.py` | Final table. Each row is dropped for the first reason that applies: excluded entry, capitation, expired, or parallel contract (the same provider and service at different rates in different groups, which may be different networks, so it is ambiguous). Everything else is kept as PPO, labelled `ppo_marker` or `unconflicted_rate`. Dropped rows go to `c2b_step6_dropped.csv` with their reason. |

The result is `output/c2b/c2b_ppo_rates.parquet`. Rate-file keys, networks and targets are options: `--network HMO` runs the same steps for another network.

## 6. C3: file quality scores

Every index file and every rate file it links is scored from 0 to 1. Code: `c3_reference.py`, `c3_metrics.py`, `c3_index_metrics.py`, `c3_diagnose.py`, `c3_report.py`.

1. **Reference tables** (`c3_reference.py`): NPPES (valid NPIs), deactivated NPIs, HiLabs codes and weights, the Medicare fee schedule, valid HCPCS and place-of-service codes, and the states where each NPI practises.
2. **Rate-file metrics M1 to M27** are computed per file from the shared scan. Each metric gives a numerator, a denominator, a value and a score from 0 to 1.
   - **Gate M1:** the file URL answers HTTP 200. An unreachable file scores 0.
   - **Timeliness (10%):** days since update, lag from the index, contracts not expired.
   - **Compliance (45%):** required fields, valid enumerations, conditional fields, reference integrity, valid billing codes, NPIs (check digit) and TINs (IRS prefix), place of service, rate types.
   - **Plausibility (25%):** zero or negative rates, outliers against Medicare and within the file (IQR fences on log rates), NPIs found and active in NPPES, entity type against billing class.
   - **Usability (20%):** duplicate rows, repeated items, share of HiLabs code weight covered, network named, unexplained rate conflicts, and the CMS guidance items. Provider coverage (M22) is reported, not scored.
   - **File score** = M1 × the weighted average of the group scores. A group score is the plain average of its measured metrics; a metric that cannot be measured is left out and the weights are re-normalized.
3. **Index metrics I1 to I15** use the same frame with Consistency (does the index agree with the files it links) in place of Plausibility, weights 10/45/15/30.
4. **Dataset score** = index score^0.3 × (mean rate-file score)^0.7: the index and the files are links of one chain, so a zero in either gives zero.
5. **Diagnosis** (`c3_diagnose.py`): turns the metric tables into a ranked list of problems per dataset with severity, why it matters and the fix, using fixed templates so the same inputs always give the same text.
6. **Report** (`c3_report.py`): `deliverables/03_file_quality_metrics.md` and `03_file_quality_scores.xlsx` define every metric and list every score.

## 7. C4: one rate per provider and service

`pipeline/c4_select.py` reads `output/c2b/c2b_ppo_rates.parquet` and keeps one rate per key (TIN × NPI × billing code type × code × additional information). Only keys with more than one distinct rate need a decision. The rules run in order and each sees only the keys still unresolved:

1. network label (a PPO-marked rate beats unmarked),
2. modifier (the whole service, no modifier, first),
3. place of service (office first),
4. majority contract (the rate carried by the most provider groups),
5. lowest rate, flagged `conflict_unresolved`.

Output in `output/c4/`: `c4_chosen.parquet` (the kept rate), `c4_alternatives.parquet` (every other rate with the reason it lost), `c4_dropped_nonpositive.parquet`, `c4_key_audit.csv`, `c4_summary.json`.

## 8. C5: practice address per TIN–NPI

`pipeline/run_c5.py` runs work packages WP0 to WP9 in `pipeline/c5/`. It produces one practice address per TIN–NPI pair, labelled `verified`, `probable` or `unverified`. Settings, weights and paths are in `config/c5.yaml`; a snapshot with its SHA-256 hash is stored with every run.

| Step | What it does |
|---|---|
| WP0 `wp0_readiness.py` | Checks the inputs, writes the source manifest (path, size, SHA-256), probes five NPIs. |
| WP1 `wp1_targets.py` | Loads the TIN–NPI pairs from `c2b_ppo_rates`, resolves individual/organization from NPPES, flags deactivated NPIs. |
| WP2 `wp2_claims.py`, `wp2_npi_registry.py`, `wp2_state_sources.py` | Collects **claims**: an address for an NPI from a source, with the source's date. NPPES primary, secondary and endpoint addresses; CMS Doctors and Clinicians (Medicare); California DCA licence files (individuals) and CDPH facility locations (organizations) when present. Each source belongs to a **family**; only different families count as independent evidence (NPPES primary/secondary/endpoint are one family, since all are self-reported). |
| WP3 `wp3_sites.py` | Normalizes every address (USPS form), geocodes with the Census batch geocoder, and caches results on disk. Equal normalized addresses become one **site**. |
| WP4 `wp4_tin_sites.py` | Builds each TIN's validated site set: sites shared by two or more of its NPIs, organization NPI sites, and Medicare group overlap. |
| WP5 `wp5_candidates.py` | Makes candidates for each pair: the NPI's own sites, plus the TIN's sites when the TIN is small or the NPI has none. Scores each with an additive, transparent score: independent families (0.40), fit with the TIN's site set (0.25), freshness (0.20), geocode match, county, in-state (0.05 each). |
| WP6 `wp6_choose.py` | Marks stale claims (a fresher claim from another family points elsewhere, or the site is over 100 km from every TIN site), picks the best candidate per pair, and labels it. **Verified:** two independent families agree and the newest evidence is at most 24 months old. **Probable:** supported but short of verified. **Unverified:** one weak or stale source. A looser profile (`label_loose`) is also written. |
| WP7 `wp7_leftovers.py` | For pairs still unverified, an optional tiered extraction from allow-listed web pages: structured data first, then a batched Gemini call with a quote check and name proximity, under spend caps. The Gemini part runs only with `estimate_only: false`, probe URLs and `GEMINI_API_KEY`; otherwise WP7 only writes a prioritized leftover queue. |
| WP8 `wp8_validation.py` | Draws a stratified 200-pair sample for manual audit. |
| WP9 `wp9_summary.py` | Writes `c5_summary.json` with label counts, source contributions and run statistics. |

Main outputs in `output/c5/`: `c5_addresses.csv` (one row per pair: address, label, loose label, score, evidence), `c5_candidates.parquet`, `c5_claims.parquet`, `c5_sites.parquet`, `c5_leftovers.csv`, `c5_summary.json`. Run options: `--estimate` (no model calls), `--dry-run` (disk cache only), `--probe-only`.

## 9. C6: the provider rate file

`pipeline/c6_provider_rate_file.py` joins three things into the main deliverable:

- the **rate** from C4 (one row per C4 key),
- the **address** from C5 (one address per TIN–NPI; other candidates go to a side table),
- the **specialty** from NPPES taxonomies: for individuals, the first taxonomy (primary first) found in the HiLabs crosswalk; for organizations and individuals with no match, the NUCC classification of the primary taxonomy.

Deactivated NPIs are removed (listed in `c6_dropped_deactivated.csv`). Other problems are flagged in `quality_flags`, not dropped: `unresolved_rate_conflict`, `no_address`, `address_unverified`, `non_dollar_type`, `specialty_unknown`, `invalid_npi`, `npi_not_in_nppes`.

Outputs in `output/c6/`: `c6_provider_rates.parquet` and `.csv` (full file), `c6_provider_rates_excel_view.xlsx` (rows on HiLabs benchmark codes, fits Excel), side tables for alternative rates and addresses, `c6_summary.json`.

## 10. C7: cost-effectiveness

The C6 file gets a cost score per provider: is the provider cheaper or dearer than comparable providers for the same services?

1. **Geocode** (`c7_geocode.py`): each unique practice address is geocoded once with the Census batch geocoder; unmatched addresses take the mean coordinates of their ZIP5, then ZIP3.
2. **Regions** (`c7_cost_effectiveness.py`): California addresses are clustered by density (HDBSCAN) into metro regions for reporting.
3. **Peer specialties**: `reference/peer_specialty.csv` maps the specialty labels to a peer specialty and a broader family, so differently spelled names for one specialty are merged.
4. **Peers for each price**: other TINs only. A widening search runs until it holds at least 5 providers from at least 3 TINs: first the same peer specialty within 25, 50, 100, 200 km, then statewide, then the specialty family, then everyone. If that fails, the **service** is relaxed step by step (ignore descriptive modifiers, any place of service, any entity type, any sub-code, then the same code family or section, compared as a share of the Medicare fee). Modifiers that change the price (26, TC, equipment) always match. The level reached is stored per price.
5. **Ratio**: each price divided by its peers' median, with flags above P75 and below P25; professional prices also get a percentage of the Medicare fee.
6. **Provider cost index**: the weighted geometric mean of the ratios (weights from HiLabs code and specialty weights, scaled by how close the peers are), shrunk toward 1 for providers with few prices.
7. **Label**: `lower_cost` (index at most 0.85), `higher_cost` (at least 1.15), `typical`, `insufficient_peers` (fewer than 3 compared prices), or `regional_average_only` (every price had no peers and used the regional average).
8. **Final file** (`c7_final_output.py`): `output/c7/c7_final.csv` and `.xlsx`, one row per price with TIN, NPI, specialty, address, code, rate, and the provider's cost index and label ("score per price").

`c7_specialty_suggest.py` is an optional check of the specialty table with a small embedding model; it does not change the table.

## 11. Repository layout

| Path | Contents |
|---|---|
| `run_pipeline.py` | One-command runner for the whole pipeline |
| `download_c5_data.py` | Downloads the public inputs into `Data/` |
| `pipeline/` | Stage scripts (`c1_*`, `c2_*`, `c2b_*`, `c3_*`, `c4_select.py`, `run_c5.py`, `c6_*`, `c7_*`, `rate_scan.py`), `run_summary.py` (headline metrics), `make_logic_pdf.py` (LOGIC.md to PDF) |
| `pipeline/c5/` | C5 work packages WP0 to WP9 |
| `config/c5.yaml` | C5 thresholds, weights, paths |
| `prompts/` | C5 Gemini extraction prompts |
| `reference/` | Editable tables: plan codes, business-name markers, network names, peer specialties |
| `tests/` | C5 unit tests |
| `output/` | Everything the pipeline writes (git-ignored), one folder per stage; `RUN_SUMMARY.md` has the headline numbers |
| `deliverables/` | Final deliverables: provider rate file (01), logic file (02), file quality metrics (03), cost-effectiveness (04), video script (05), run summary; index in `deliverables/README.md` |
| `LOGIC.md`, `LOGIC.pdf` | Scope, assumptions and every rule with its reason |
| `handoff/`, `docs/`, `experiments/` | Team guides and findings, plan diagrams and future work, side experiments |

## 12. Conventions and tests

- Large files are streamed or scanned with DuckDB; nothing loads the full NPPES CSV or a whole rate file into memory.
- Identifiers stay text: NPI as 10 digits, TIN with a dash (`94-1156581`).
- Large outputs are Parquet; CSV only for small tables.
- Every script accepts `--help`; paths default relative to the repository root.
- Reference tables hold the decisions that might change (plan codes, name markers, peer specialties) so a change needs a table edit, not a code edit.
- Outputs are deterministic: list columns are sorted, so two runs of the same code give identical files.

```bash
pytest tests/test_c5.py -v
```
