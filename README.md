# Centene PPO provider rates pipeline

HiLabs hackathon: build a provider-level PPO rate file for Centene California (NPI × TIN × billing code × address × specialty × rate) from Centene's price transparency files.

**Team guides start in [`handoff/`](handoff/README.md).** Rules and their reasons: [`LOGIC.md`](LOGIC.md). Findings F1–F32: [`handoff/FINDINGS.md`](handoff/FINDINGS.md).

## Setup

Python 3.12.

```bash
pip install -r requirements.txt
```

Large inputs are not in git. Put them under `Data/` in your checkout (paths and download links: [`handoff/DATA_SOURCES.md`](handoff/DATA_SOURCES.md)).

## Run (steps built so far)

All paths default to locations relative to the repo root; every script prints `--help`.

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

| Step | Reads | Writes |
|---|---|---|
| C1 parse index files | `Data/*_index.json` | `output/c1/` (`c1_plans.csv`, `c1_plans_ca.csv`, `c1_rate_files.csv`, …) |
| C2 step 1: PPO in the name | `output/c1/c1_plans_ca.csv` | `output/c2/c2_step1_ppo_name_plans.csv` |
| C2 step 2: other network word or plan code | `output/c1/c1_plans_ca.csv`, `reference/network_codes.csv` | `output/c2/c2_step2_other_network_name_plans.csv` |
| C2 step 3: no network signal | same | `output/c2/c2_step3_unlabelled_name_plans.csv` |
| C2b step 1: provider group tables | `output/c1/c1_plans_ca.csv`, shared scan | `output/c2b/c2b_groups.parquet`, `c2b_group_summary.parquet` |
| C2b step 2: business-name markers | step 1, `reference/business_name_labels.csv` | `output/c2b/c2b_step2_member_labels.csv`, `c2b_step2_group_name_labels.csv`, `c2b_step2_name_patterns.csv` |
| C2b step 3: price table, capitation | `output/c1/c1_plans_ca.csv`, shared scan | `output/c2b/c2b_prices.parquet`, `c2b_step3_capitation_groups.csv` |
| C2b step 4: contract dates, parallel copies | steps 1 and 3 (`--run-date`, default today) | `output/c2b/c2b_step4_copy_cases.parquet` |
| Export: data remaining after step 4 | steps 1–5 | `output/c2b/c2b_step4_kept_prices.csv` / `.parquet` |
| C2b step 6: final PPO rates (closes C2b) | steps 1–5 | `output/c2b/c2b_ppo_rates.parquet` / `.csv`, `c2b_step6_dropped.csv` |
| C2 final: plan class and evidence | C2 step sheets | `output/c2/c2_plan_network.csv` |
| C2b step 5: parallel-contract test per TIN–NPI | steps 1–3 | `output/c2b/c2b_step5_price_keys.parquet`, `c2b_step5_pair_key_copies.parquet`, `c2b_step5_group_copy_stats.csv` |
| C3 reference tables | NPPES, deactivation report, HiLabs workbook, `Data/cms_ref/` (PFS RVU26D, HCPCS), CMS Doctors and Clinicians | `output/c3/ref/*.parquet` |
| Shared scan (each rate file read once; C2b and C3 read it) | `output/c1/c1_rate_files.csv`, `Data/rate_files/` (`.json` or `.json.gz`) or the URLs with `--stream` | `output/scan/<file>_{members,groups,prices,refs}.parquet`, `<file>_stats.json` |
| C3 rate-file metrics M1–M27 | scan, reference tables | `output/c3/c3_rate_file_metrics.csv`, `c3_rate_file_scores.csv`, `c3_rate_file_rollup.csv` |
| C3 index metrics I1–I15, dataset scores | `Data/*_index.json`, rate-file scores, scan, reference tables | `output/c3/c3_index_metrics.csv`, `c3_index_scores.csv`, `c3_dataset_scores.csv` |
| C3 diagnosis: problems ranked by severity, with fixes | all C3 outputs | `output/c3/c3_diagnosis.md`, `c3_diagnosis.csv` |
| Deliverable 3: file quality metrics | all C3 outputs | `deliverables/03_file_quality_metrics.md`, `03_file_quality_scores.xlsx` |

C2 steps run on all states with `--plans output/c1/c1_plans.csv`.

Run `python pipeline/c3_reference.py` (reference tables; its HCPCS list is used by the scan) and then `python pipeline/rate_scan.py` after C1 and before C2b: it reads every rate file once and C2b steps 1 and 3 and C3 use its tables. Other target networks: `--network HMO` on `c2_final_plans.py` and C2b steps 2, 5 and 6, with `--out`/`--dir output/c2b_hmo` (PPO is the default and keeps every current file name).

## Layout

| Path | Contents |
|---|---|
| `pipeline/` | Pipeline steps |
| `reference/network_codes.csv` | Brand-specific plan codes (Health Net EOA, SAL) with sources |
| `LOGIC.md` | Logic deliverable: scope, assumptions, rules with reasons, results |
| `handoff/` | Team guides, data sources, findings, shared data snapshots |
| `reference/business_name_labels.csv` | Network markers in TIN business names (C2b step 2) |
| `docs/diagrams/` | Plan diagrams (SVG) |
| `docs/FUTURE_WORK.md` | Deferred work (e.g. resolving parallel contracts) with what is known and how to pick it up |
| `experiments/network_inference/` | Test: inferring network type when plan names don't say it (see `RESULTS.md`) |
| `handoff/test_set_C5/` | C5 test set: 707 TIN–NPI pairs with independent truth addresses, scorer, NPPES baseline (see `RESULTS.md`) |
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

Optional check of the specialty table with a small embedding model (downloads `BAAI/bge-small-en-v1.5` on first use):

```bash
python pipeline/c7_specialty_suggest.py
```
