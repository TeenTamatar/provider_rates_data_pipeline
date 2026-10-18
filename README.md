# Centene PPO provider rates pipeline

HiLabs hackathon: build a provider-level PPO rate file for Centene California (NPI × TIN × billing code × address × specialty × rate) from Centene's price transparency files.

**Team guides start in [`handoff/`](handoff/README.md).** Rules and their reasons: [`LOGIC.md`](LOGIC.md). Findings F1–F32: [`handoff/FINDINGS.md`](handoff/FINDINGS.md). (These live in the team's working copy and are not on this branch.)

| Stage | Code | Output |
|---|---|---|
| C1 parse index files | `pipeline/c1_*.py` | `output/c1/` |
| C2 find the PPO network, C2b PPO prices | `pipeline/c2_*.py`, `pipeline/c2b_*.py`, `pipeline/rate_scan.py` | `output/c2/`, `output/c2b/`, `output/scan/` |
| C3 file quality metrics | `pipeline/c3_*.py` | `output/c3/`, `deliverables/` |
| C4 one rate per provider key | `pipeline/c4_select.py` | `output/c4/` |
| C5 TIN/NPI → practice address | `pipeline/run_c5.py`, `pipeline/c5/` | `output/c5/` |
| C6 provider rate file | `pipeline/c6_provider_rate_file.py` | `output/c6/` |
| C7 cost-effectiveness | `pipeline/c7_*.py` | `output/c7/` |

## Setup

Python 3.12.

```bash
pip install -r requirements.txt
```

Large inputs are not in git. They go under `Data/` in the repo root (the folder is git-ignored); see [Data sources](#data-sources) below.

C5 also needs a `.env` with `GEMINI_API_KEY` (copy `.env.example`); see [C5 quick start](#quick-start).

## Run everything (one command)

Put the two organizer files in `Data/` (see [Data sources](#data-sources)), then:

```bash
python run_pipeline.py
```

This downloads every public input that is missing, then runs C1 → C7 in order and stops at the first failing step. Useful options:

| Option | Effect |
|---|---|
| `--list` | Print the 27 steps in run order |
| `--from c4` / `--to c3_report` | Start or stop at a step (full name or prefix) |
| `--only c6 c7_geocode` | Run only these steps |
| `--skip-download` | Skip the download steps (inputs already in `Data/`) |
| `--offline` | No paid or remote calls: C5 runs `--estimate --dry-run` (no Gemini, no Census), C7 uses cached geocodes |
| `--run-date 2026-10-18` | Run date for the scan and C3 (default: today) |

A failed step prints the command to resume from it. The final output is in `output/c7/`.

## Data sources

Provided by the organizers (not publicly downloadable; copy into `Data/` yourself):

| Local path | What it holds | Needed by |
|---|---|---|
| `Data/2026-09-30_healthnet_index.json`, `Data/2026-09-28_qualchoice_index.json`, `Data/2026-09-28_fidelis_index.json`, `Data/2026-09-28_ambetter_index.json` | Centene price transparency index files (plans → rate file links) | C1, C3 |
| `Data/Rate Benchmarking Individual Weights_Hackathon.xlsx` | HiLabs taxonomy → specialty crosswalk, specialty weights, codes per specialty | C3, C4, C6, C7 |

Downloaded automatically by `run_pipeline.py` (or by hand from these links):

| Local path | Size | Source | Needed by |
|---|---|---|---|
| `Data/NPPES_Data_Dissemination_October_2026_V2/` (`npidata_pfile_*`, `pl_pfile_*`, `endpoint_pfile_*`) | 1.1 GB zip, 11 GB unzipped | [NPPES October 2026 V2 zip](https://download.cms.gov/nppes/NPPES_Data_Dissemination_October_2026_V2.zip) ([NPPES downloads page](https://download.cms.gov/nppes/NPI_Files.html)) | C3, C5, C6 |
| `Data/NPPES Deactivated NPI Report 20261012.xlsx` | 5.4 MB | [NPPES deactivated NPI report zip](https://download.cms.gov/nppes/NPPES_Deactivated_NPI_Report_101226_V2.zip) | C3, C5 |
| `Data/DAC_NationalDownloadableFile.csv` | ~900 MB | [CMS Doctors and Clinicians national file (dataset mj5m-pzi6)](https://data.cms.gov/provider-data/dataset/mj5m-pzi6) | C3, C5 |
| `Data/nucc_taxonomy_261.csv` | 520 KB | [NUCC provider taxonomy v26.1 CSV](https://nucc.org/images/stories/CSV/nucc_taxonomy_261.csv) ([NUCC CSV page](https://www.nucc.org/index.php/code-sets-mainmenu-41/provider-taxonomy-mainmenu-40/csv-mainmenu-57)) | C5, C6 |
| `Data/cms_ref/rvu26d/` | zip | [Medicare PFS RVU26D](https://www.cms.gov/files/zip/rvu26d-updated-08-26-2026.zip) ([PFS relative value files](https://www.cms.gov/medicare/payment/fee-schedules/physician/pfs-relative-value-files)) | C3, C7 |
| `Data/cms_ref/hcpcs_2026_10/` | zip | [HCPCS October 2026 alpha-numeric file](https://www.cms.gov/files/zip/october-2026-alpha-numeric-hcpcs-file.zip) ([HCPCS quarterly update](https://www.cms.gov/medicare/coding-billing/healthcare-common-procedure-system/quarterly-update)) | C3 |
| `Data/rate_files/*.json` | 700 MB (Health Net CA) and more | Links listed by C1 in `output/c1/c1_rate_files.csv`, e.g. [Health Net CA in-network file](https://www.centene.com/content/dam/centene/Centene%20Corporate/json/DOCUMENT/2026-09-30_centene-management-company-llc_healthnet-ca_in-network.json) ([Centene price transparency](https://www.centene.com/price-transparency-files.html)); downloaded by `pipeline/download_rate_files.py` | C2b, C3 |

Optional C5 state sources (used when present): [California DCA licensee bulk files](https://www.dca.ca.gov/consumers/public_info/index.shtml) into `data/state_sources/dca/`, and [CDPH healthcare facility locations](https://data.chhs.ca.gov/dataset/healthcare-facility-locations) into `data/state_sources/cdph/health_facility_locations.csv`.

Free services called at run time: [US Census Geocoder](https://geocoding.geo.census.gov/geocoder/Geocoding_Services_API.html) (C5, C7) and Gemini (C5, needs `GEMINI_API_KEY`; skipped with `--offline`).

## Run step by step

`run_pipeline.py` runs exactly these commands. All paths default to locations relative to the repo root; every script prints `--help`. Run the steps in this order.

```bash
python pipeline/c1_parse_index.py
```

```bash
python pipeline/c2_step1_ppo_names.py
```

```bash
python pipeline/c2_step2_other_network_names.py
```

```bash
python pipeline/c2_step3_unlabelled_names.py
```

```bash
python pipeline/c3_reference.py
```

```bash
python pipeline/rate_scan.py --run-date 2026-10-18
```

```bash
python pipeline/c2b_step1_groups.py
```

```bash
python pipeline/c2b_step2_name_labels.py
```

```bash
python pipeline/c2b_step3_capitation.py
```

```bash
python pipeline/c2b_step4_contract_dates.py
```

```bash
python pipeline/c2b_step5_parallel_copies.py
```

```bash
python pipeline/c2b_export_kept.py
```

```bash
python pipeline/c2b_step6_ppo_rates.py
```

```bash
python pipeline/c2_final_plans.py
```

C3 (file quality, all index and rate files; needs every rate file in `Data/rate_files/`, see `pipeline/download_rate_files.py`, and the CMS reference files unzipped into `Data/cms_ref/rvu26d/` and `Data/cms_ref/hcpcs_2026_10/` from https://www.cms.gov/files/zip/rvu26d-updated-08-26-2026.zip and https://www.cms.gov/files/zip/october-2026-alpha-numeric-hcpcs-file.zip):

```bash
python pipeline/c3_metrics.py --run-date 2026-10-18
```

```bash
python pipeline/c3_index_metrics.py --run-date 2026-10-18
```

```bash
python pipeline/c3_diagnose.py
```

```bash
python pipeline/c3_report.py
```

C4 (reads the C2b step 6 output):

```bash
python pipeline/c4_select.py
```

C5 (reads the C2b step 6 output; offline run shown, full options in [C5](#c5-tinnpi--practice-address-pipeline)):

```bash
python pipeline/run_c5.py --estimate --dry-run
```

| Step | Reads | Writes |
|---|---|---|
| C1 parse index files | `Data/*_index.json` | `output/c1/` (`c1_plans.csv`, `c1_plans_ca.csv`, `c1_rate_files.csv`, …) |
| C2 step 1: PPO in the name | `output/c1/c1_plans_ca.csv` | `output/c2/c2_step1_ppo_name_plans.csv` |
| C2 step 2: other network word or plan code | `output/c1/c1_plans_ca.csv`, `reference/network_codes.csv` | `output/c2/c2_step2_other_network_name_plans.csv` |
| C2 step 3: no network signal | same | `output/c2/c2_step3_unlabelled_name_plans.csv` |
| C3 reference tables | NPPES, deactivation report, HiLabs workbook, `Data/cms_ref/` (PFS RVU26D, HCPCS), CMS Doctors and Clinicians | `output/c3/ref/*.parquet` |
| Shared scan (each rate file read once; C2b and C3 read it) | `output/c1/c1_rate_files.csv`, `Data/rate_files/` (`.json` or `.json.gz`) or the URLs with `--stream` | `output/scan/<file>_{members,groups,prices,refs}.parquet`, `<file>_stats.json` |
| C2b step 1: provider group tables | `output/c1/c1_plans_ca.csv`, shared scan | `output/c2b/c2b_groups.parquet`, `c2b_group_summary.parquet` |
| C2b step 2: business-name markers | step 1, `reference/business_name_labels.csv` | `output/c2b/c2b_step2_member_labels.csv`, `c2b_step2_group_name_labels.csv`, `c2b_step2_name_patterns.csv` |
| C2b step 3: price table, capitation | `output/c1/c1_plans_ca.csv`, shared scan | `output/c2b/c2b_prices.parquet`, `c2b_step3_capitation_groups.csv` |
| C2b step 4: contract dates, parallel copies | steps 1 and 3 (`--run-date`, default today) | `output/c2b/c2b_step4_copy_cases.parquet` |
| C2b step 5: parallel-contract test per TIN–NPI | steps 1–3 | `output/c2b/c2b_step5_price_keys.parquet`, `c2b_step5_pair_key_copies.parquet`, `c2b_step5_group_copy_stats.csv` |
| Export: data remaining after step 4 | steps 1–5 | `output/c2b/c2b_step4_kept_prices.csv` / `.parquet` |
| C2b step 6: final PPO rates (closes C2b) | steps 1–5 | `output/c2b/c2b_ppo_rates.parquet` / `.csv`, `c2b_step6_dropped.csv` |
| C2 final: plan class and evidence | C2 step sheets | `output/c2/c2_plan_network.csv` |
| C3 rate-file metrics M1–M27 | scan, reference tables | `output/c3/c3_rate_file_metrics.csv`, `c3_rate_file_scores.csv`, `c3_rate_file_rollup.csv` |
| C3 index metrics I1–I15, dataset scores | `Data/*_index.json`, rate-file scores, scan, reference tables | `output/c3/c3_index_metrics.csv`, `c3_index_scores.csv`, `c3_dataset_scores.csv` |
| C3 diagnosis: problems ranked by severity, with fixes | all C3 outputs | `output/c3/c3_diagnosis.md`, `c3_diagnosis.csv` |
| Deliverable 3: file quality metrics | all C3 outputs | `deliverables/03_file_quality_metrics.md`, `03_file_quality_scores.xlsx` |
| C4 rate selection: one rate per TIN × NPI × code × additional information | `output/c2b/c2b_ppo_rates.parquet` | `output/c4/c4_chosen.parquet`, `c4_alternatives.parquet`, `c4_dropped_nonpositive.parquet`, `c4_key_audit.csv`, `c4_summary.json` |
| C5 address mapping (WP0–WP9) | `output/c2b/c2b_ppo_rates.parquet`, NPPES, CMS DAC | `output/c5/` (see [Outputs](#outputs-outputc5)) |

C2 steps run on all states with `--plans output/c1/c1_plans.csv`.

Run `python pipeline/c3_reference.py` (reference tables; its HCPCS list is used by the scan) and then `python pipeline/rate_scan.py` after C1 and before C2b: it reads every rate file once and C2b steps 1 and 3 and C3 use its tables. Other target networks: `--network HMO` on `c2_final_plans.py` and C2b steps 2, 5 and 6, with `--out`/`--dir output/c2b_hmo` (PPO is the default and keeps every current file name).

## C4: rate selection

`pipeline/c4_select.py` keeps one rate per key (TIN × NPI × billing code type × billing code × additional information). Rules run as a funnel, each only on keys that still have more than one distinct rate: network label, modifier (whole service first), place of service (office first), majority contract (rate carried by the most provider groups), then lowest rate (flagged `conflict_unresolved`). Every row not chosen goes to `c4_alternatives.parquet` with a reason. Parallel contracts are already dropped by C2b step 6; the majority-contract rule stays as a second check.

## Layout

| Path | Contents |
|---|---|
| `pipeline/` | Pipeline steps (C1–C4 scripts, `run_c5.py`) |
| `pipeline/c5/` | C5 work packages WP0–WP9 |
| `config/c5.yaml` | C5 thresholds, weights and paths |
| `prompts/` | C5 Gemini extraction prompts |
| `tests/` | C5 unit tests (`pytest`) |
| `reference/network_codes.csv` | Brand-specific plan codes (Health Net EOA, SAL) with sources |
| `reference/business_name_labels.csv` | Network markers in TIN business names (C2b step 2) |
| `experiments/state_sources_trial/` | C5 trial of state licensing and facility sources |
| `download_c5_data.py` | Downloads C5's NPPES and CMS inputs |

Kept in the team's working copy, not on this branch:

| Path | Contents |
|---|---|
| `LOGIC.md` | Logic deliverable: scope, assumptions, rules with reasons, results |
| `handoff/` | Team guides, data sources, findings, shared data snapshots |
| `handoff/test_set_C5/` | C5 test set: 707 TIN–NPI pairs with independent truth addresses, scorer, NPPES baseline (see `RESULTS.md`) |
| `docs/diagrams/` | Plan diagrams (SVG) |
| `docs/FUTURE_WORK.md` | Deferred work (e.g. resolving parallel contracts) with what is known and how to pick it up |
| `experiments/network_inference/` | Test: inferring network type when plan names don't say it (see `RESULTS.md`) |
| `experiments/centene_pd/` | Centene FHIR directory coverage test (blocked, HTTP 403) |
| `.github/workflows/` | Manually triggered coverage-test workflow |

C6 (provider rate file; needs the C4 and C5 outputs in `output/c4/` and `output/c5/`, plus NPPES and the HiLabs workbook in `Data/`):

```bash
python pipeline/c6_provider_rate_file.py
```

Writes `output/c6/c6_provider_rates.parquet` and `.csv` (full file), `c6_provider_rates_excel_view.xlsx` (HiLabs codes only), side tables for alternative rates and addresses, and `c6_summary.json`.

C7 (cost-effectiveness; needs the C5 addresses, the C6 file and the C3 reference tables; the geocoder calls the free Census batch service):

```bash
python pipeline/c7_geocode.py
```

```bash
python pipeline/c7_cost_effectiveness.py
```

Writes `output/c7/c7_geocode.parquet`, `c7_address_geo.parquet`, `c7_price_ratios.parquet` (each price with its peer group, level and ratios), `c7_npi_cost.parquet` / `.csv` and `c7_tin_cost.parquet` / `.csv` (cost index and label), and `c7_summary.json`. `reference/peer_specialty.csv` maps specialties to peer groups.

Final file (one row per price with TIN, NPI, specialty, address, billing code, rate, and the provider's cost-effectiveness score and label):

```bash
python pipeline/c7_final_output.py
```

Writes `output/c7/c7_final.csv` and `.xlsx`.

Optional check of the specialty table with a small embedding model (downloads `BAAI/bge-small-en-v1.5` on first use):

```bash
python pipeline/c7_specialty_suggest.py
```

## C5: TIN/NPI → Practice Address Pipeline

**Goal.** Produce one verified/probable/unverified practice address per TIN–NPI pair from the Centene California rate files, using NPPES and CMS DAC as evidence sources, validated via the US Census Geocoder.

### Quick start

```bash
# 1. Create and activate the venv
python3 -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Copy and fill in environment variables
cp .env.example .env
# Edit .env and set your GEMINI_API_KEY (from Google AI Studio):
# GEMINI_API_KEY=your_key_here

# 4. Configure web fetch allowlist and probe URLs in config/c5.yaml
# Add permitted payer/web domains and 3-5 real provider result URLs to probe:
# web_fetch_allowlist:
#   - healthnet.com
#   - search.dca.ca.gov
# probe_urls:
#   healthnet.com:
#     - "https://www.healthnet.com/provider-sample-1"
#     - "https://www.healthnet.com/provider-sample-2"

# 5. Run domain probe only (verifies gates, writes c5_domain_probe.csv)
python pipeline/run_c5.py --probe-only

# 6. Run in estimate mode (Tiers 0 & 1 only, estimates Gemini calls needed, zero model spend)
python pipeline/run_c5.py --estimate

# 7. Run full C5 Address Mapping pipeline
python pipeline/run_c5.py

# 8. Offline replay (idempotent run; hits disk cache with zero network/API calls)
python pipeline/run_c5.py --dry-run

# 9. Run unit tests
pytest tests/test_c5.py -v
```

All parameters, weights, and file paths are fully configurable in `config/c5.yaml`.
A snapshot of the configuration (with its SHA-256 hash) is stored with every run.

### Adding Probe URLs, Templates, and Domains

#### 1. Adding Allowlisted Domains
In `config/c5.yaml`, add the domain under `web_fetch_allowlist` and its family (`payer` or `web`) under `web_fetch_domain_family`:
```yaml
web_fetch_allowlist:
  - healthnet.com
  - search.dca.ca.gov
web_fetch_domain_family:
  healthnet.com: payer
  search.dca.ca.gov: web
```

#### 2. Supplying Probe URLs (Gate Requirement)
Every allowlisted domain must be probed before live lookups can occur. Supply 3 to 5 real provider search result URLs copied from a browser under `probe_urls`:
```yaml
probe_urls:
  healthnet.com:
    - "https://www.healthnet.com/portal/provider/searchResult.do?..."
```
If `probe_urls` is empty, the domain is marked `unprobed`, lookups halt cleanly, and human instructions with the priority TIN list are written to `output/c5/c5_leftovers.csv`.

#### 3. Adding URL Templates
Supply URL templates with placeholders (`{npi}`, `{tin}`, `{name_slug}`, `{city_slug}`, `{zip5}`):
```yaml
url_templates:
  dca_license: "https://search.dca.ca.gov/details/{npi}"
```
Missing placeholders skip template generation without errors. Search-based discovery is strictly prohibited (`discovery_search_enabled: false`).

### Pipeline steps

| Step | Code | Description |
|------|------|-------------|
| WP0 | `wp0_readiness.py` | Environment check, file verification, source manifest (`c5_sources_manifest.json`), 5-NPI probe |
| WP1 | `wp1_targets.py` | Load target TIN-NPI population reconciled against C2b step 6 (`c2b_ppo_rates`), resolve entity types, deactivation check |
| WP2 | `wp2_claims.py` | Extract evidence claims (NPPES primary + secondary + endpoints = `nppes_self`; CMS DAC = `medicare`; CA DCA = `state_license`; CDPH = `state_facility`) |
| WP3 | `wp3_sites.py` | USPS address normalization, Census batch geocoding, persistent disk cache |
| WP4 | `wp4_tin_sites.py` | Build validated TIN site sets (org NPI support, provider consensus, Medicare group overlap) |
| WP5 | `wp5_candidates.py` | Candidate generation and 6-factor weighted scoring |
| WP6 | `wp6_choose.py` | Stale & conflict checks, winner selection, verified/probable/unverified labelling (strict & loose profiles) |
| WP7 | `wp7_leftovers.py` | Domain probe gate + Tiered extraction: Tier 0 (deterministic confirm), Tier 1 (JSON-LD, microdata, `<address>`), Tier 2 (batched Gemini 2.5 Flash with quote-check, name proximity, spend caps) |
| WP8 | `wp8_validation.py` | Stratified 200-pair audit calibration sample with state-source strata |
| WP9 | `wp9_summary.py` | Write `c5_summary.json` with state source contributions, before/after comparison, and run statistics |

## State Data Sources (DCA & CDPH)

To expand verification without unconstrained scraping, C5 incorporates two official California state bulk sources:
- **California DCA Licensee Bulk Files (Individuals):** Downloaded from the DCA public Box share into `data/state_sources/dca/`. Joined on normalized license numbers and verified legal last names.
- **CDPH Healthcare Facility Locations (Organizations):** Downloaded from CHHS Open Data into `data/state_sources/cdph/health_facility_locations.csv`. Matched via NPI, Name + ZIP, and Name + City.

For manual-drop download instructions, see `output/c5/c5_source_instructions.md`.

### Labelling rules

| Label | Condition |
|-------|-----------|
| **verified** (strict) | ≥ 2 independent source families agree on the site AND newest evidence date ≤ 24 months |
| **verified** (loose) | ≥ 1 fresh independent family agree on site AND TIN site fit score $s_{\text{tin\_fit}} \ge 0.25$ (`label_loose`) |
| **probable** | Supported but short of verified (e.g. 1 fresh source + TIN site set match, or 2 families but not fresh) |
| **unverified** | Single weak source, stale-only, or no candidate found in bulk sources |

**Source Family Independence Rules (R5.1, R5.8):**
- NPPES primary, secondary (`pl`), and endpoints (`ep`) all belong to family `nppes_self`.
- CMS DAC belongs to family `medicare`.
- California DCA licensee bulk files belong to family `state_license` (independent).
- CDPH healthcare facility locations belong to family `state_facility` (independent).
- Web fetcher families: `payer_directory` (independent), `org_website` (independent), `web_aggregator` (**never independent** of `nppes_self` since aggregators derive provider records from NPPES).

### Outputs (`output/c5/`)

| File | Contents |
|------|----------|
| `c5_addresses.csv` | One row per TIN–NPI (3,780 rows): best address + strict `label` + loose `label_loose` + score + evidence |
| `c5_candidates.parquet` | Scored candidates per TIN–NPI (8,155 rows) |
| `c5_tin_sites.parquet` | Validated TIN site sets (5,440 relationships across 2,888 TINs) |
| `c5_sites.parquet` | Normalized physical address sites with coordinates |
| `c5_leftovers.csv` | Prioritized queue of unverified pairs ordered by TIN size descending |
| `c5_source_contribution.csv` | Individual vs Organization verification metrics for DCA and CDPH |
| `c5_source_disagreements.csv` | Top-50 TIN discrepancies between state claims and winning addresses |
| `c5_source_instructions.md` | Download links and verification steps for state sources |
| `c5_lookup_log.csv` | Audit log of every web fetch attempt (status, response size, refusal reason) |
| `c5_llm_log.csv` | Audit log of every Gemini API call (prompt tokens, duration, status, reason code) |
| `c5_calibration_sample.csv` | 200-pair stratified sample for manual audit (includes state-supported strata) |
| `c5_sources_manifest.json` | Provenance for every input file (path, file size, SHA-256) |
| `c5_config_snapshot.yaml` | Exact configuration used for the run with SHA-256 hash |
| `c5_summary.json` | Key verification counts, shares, before/after comparison, and metadata |

### Data sources required

Place these under `Data/` in the repo root:

| File | Size | Source |
|------|------|--------|
| `NPPES_Data_Dissemination_October_2026_V2/npidata_pfile_20050523-20261011.csv` | 11 GB | Provided by organizers |
| `NPPES_Data_Dissemination_October_2026_V2/pl_pfile_20050523-20261011.csv` | 117 MB | Provided by organizers |
| `NPPES_Data_Dissemination_October_2026_V2/endpoint_pfile_20050523-20261011.csv` | 121 MB | Provided by organizers |
| `NPPES Deactivated NPI Report 20261012.xlsx` | 5.4 MB | Provided by organizers |
| `DAC_NationalDownloadableFile.csv` | ~500 MB | [data.cms.gov dataset mj5m-pzi6](https://data.cms.gov/provider-data/dataset/mj5m-pzi6) |
| `data/state_sources/dca/*.tsv` | ~250 MB | California DCA Public Box Folder (Medical Board, Nursing, etc.) |
| `data/state_sources/cdph/health_facility_locations.csv` | 6.9 MB | CHHS Open Data Portal |

Target TIN–NPI pairs are read from the C2b step 6 output `output/c2b/c2b_ppo_rates.parquet` (`paths.targets` in `config/c5.yaml`).

### Module layout

| File | Purpose |
|------|---------|
| `pipeline/run_c5.py` | Top-level CLI entrypoint (`--config`, `--dry-run`) |
| `pipeline/c5/run_all.py` | Master pipeline orchestrator connecting WP0–WP9 |
| `pipeline/c5/common.py` | Shared configuration, schemas, hash snapshot, and ID formatters |
| `pipeline/c5/wp0_readiness.py` | WP0: Setup validation, source manifest generation, 5-NPI probe |
| `pipeline/c5/wp1_targets.py` | WP1: Load Health Net CA targets, entity types, deactivation flags |
| `pipeline/c5/wp2_claims.py` | WP2: Load evidence claims (NPPES primary/pl/ep, CMS DAC, PO box flags, state sources) |
| `pipeline/c5/wp2_state_sources.py` | WP2: State sources ingestion (DCA bulk files for individuals, CDPH facilities for orgs) |
| `pipeline/c5/wp3_sites.py` | WP3: USPS address normalization, Census batch geocoding, disk cache |
| `pipeline/c5/wp4_tin_sites.py` | WP4: TIN site set construction (org NPI, consensus, Medicare group) |
| `pipeline/c5/wp5_candidates.py` | WP5: Candidate generation and additive feature scoring |
| `pipeline/c5/wp6_choose.py` | WP6: Stale & conflict checks, winner selection, strict/loose labelling, disagreement logging |
| `pipeline/c5/wp7_leftovers.py` | WP7: Unverified leftover queue prioritized by TIN size, quote check |
| `pipeline/c5/wp8_validation.py` | WP8: Stratified calibration sample generation and precision metrics |
| `pipeline/c5/wp9_summary.py` | WP9: Comprehensive execution summary, state contribution metrics, before/after diff |

### Conventions (per project LOGIC.md)

- Stream large files; never load the full NPPES CSV into memory (DuckDB scans in ~14 s).
- Keep identifiers as text: NPI (10 digits, zero-padded), TIN with dash (`94-1156581`).
- Write large outputs as Parquet; CSV only for small tables.
- Every script accepts `--help` with path arguments defaulting relative to the repo root.
- Each step writes `c5_summary.json` with key counts.
