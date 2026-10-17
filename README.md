# C5: TIN/NPI → Practice Address Pipeline

**Goal.** Produce one verified/probable/unverified practice address per TIN–NPI pair from the Centene California rate files, using NPPES and CMS DAC as evidence sources, validated via the US Census Geocoder.

## Quick start

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

# 4. Configure web fetch allowlist in config/c5.yaml (optional)
# Under web_fetch_allowlist, add permitted payer or provider domains:
# web_fetch_allowlist:
#   - "healthnet.com"
#   - "sutterhealth.org"

# 5. Run the C5 Address Mapping pipeline end-to-end
python pipeline/run_c5.py

# Optional: Run dry-run (skips network calls)
python pipeline/run_c5.py --dry-run

# Run unit tests
pytest tests/test_c5.py -v
```

All parameters, weights, and file paths are fully configurable in `config/c5.yaml`.
A snapshot of the configuration (with its SHA-256 hash) is stored with every run.

## Pipeline steps

| Step | Code | Description |
|------|------|-------------|
| WP0 | `wp0_readiness.py` | Environment check, file verification, source manifest (`c5_sources_manifest.json`), 5-NPI probe |
| WP1 | `wp1_targets.py` | Load target TIN-NPI population (`rate_file_providers.csv`), resolve entity types, deactivation check |
| WP2 | `wp2_claims.py` | Extract evidence claims (NPPES primary + secondary + endpoints = `nppes_self`; CMS DAC = `medicare`) |
| WP3 | `wp3_sites.py` | USPS address normalization, Census batch geocoding, persistent disk cache |
| WP4 | `wp4_tin_sites.py` | Build validated TIN site sets (org NPI support, provider consensus, Medicare group overlap) |
| WP5 | `wp5_candidates.py` | Candidate generation and 6-factor weighted scoring |
| WP6 | `wp6_choose.py` | Stale & conflict checks, winner selection, verified/probable/unverified labelling |
| WP7 | `wp7_leftovers.py` | Capped leftover-lookup: allowlisted static web fetcher (robots.txt compliance) + Google Gemini structured extraction with verbatim quote-check |
| WP8 | `wp8_validation.py` | Stratified 200-pair audit calibration sample |
| WP9 | `wp9_summary.py` | Write `c5_summary.json` with before/after comparison and run statistics |

## Labelling rules

| Label | Condition |
|-------|-----------|
| **verified** | ≥ 2 independent source families agree on the site AND newest evidence date ≤ 24 months |
| **probable** | Supported but short of verified (e.g. 1 fresh source + TIN site set match, or 2 families but not fresh) |
| **unverified** | Single weak source, stale-only, or no candidate found in bulk sources |

**Source Family Independence Rules (R5.1, R5.8):**
- NPPES primary, secondary (`pl`), and endpoints (`ep`) all belong to family `nppes_self`.
- CMS DAC belongs to family `medicare`.
- Web fetcher families: `payer_directory` (independent), `org_website` (independent), `web_aggregator` (**never independent** of `nppes_self` since aggregators derive provider records from NPPES).

## Outputs (`output/c5/`)

| File | Contents |
|------|----------|
| `c5_addresses.csv` | One row per TIN–NPI (3,780 rows): best address + label + score + evidence |
| `c5_candidates.parquet` | Scored candidates per TIN–NPI (8,155 rows) |
| `c5_tin_sites.parquet` | Validated TIN site sets (5,440 relationships across 2,888 TINs) |
| `c5_sites.parquet` | Normalized physical address sites with coordinates |
| `c5_leftovers.csv` | Prioritized queue of unverified pairs ordered by TIN size descending |
| `c5_lookup_log.csv` | Audit log of every web fetch attempt (status, response size, refusal reason) |
| `c5_llm_log.csv` | Audit log of every Gemini API call (prompt tokens, duration, status, reason code) |
| `c5_calibration_sample.csv` | 200-pair stratified sample for manual audit |
| `c5_sources_manifest.json` | Provenance for every input file (path, file size, SHA-256) |
| `c5_config_snapshot.yaml` | Exact configuration used for the run with SHA-256 hash |
| `c5_summary.json` | Key verification counts, shares, before/after comparison, and metadata |

## Data sources required

Place these under `../Data/` (relative to `provider_rates_data_pipeline/`):

| File | Size | Source |
|------|------|--------|
| `NPPES_Data_Dissemination_October_2026_V2/npidata_pfile_20050523-20261011.csv` | 11 GB | Provided by organizers |
| `NPPES_Data_Dissemination_October_2026_V2/pl_pfile_20050523-20261011.csv` | 117 MB | Provided by organizers |
| `NPPES_Data_Dissemination_October_2026_V2/endpoint_pfile_20050523-20261011.csv` | 121 MB | Provided by organizers |
| `NPPES Deactivated NPI Report 20261012.xlsx` | 5.4 MB | Provided by organizers |
| `DAC_NationalDownloadableFile.csv` | ~500 MB | [data.cms.gov dataset mj5m-pzi6](https://data.cms.gov/provider-data/dataset/mj5m-pzi6) |

Target list is read from `../hilabs-provider-rates/handoff/data/rate_file_providers.csv`.

## Module layout

| File | Purpose |
|------|---------|
| `pipeline/run_c5.py` | Top-level CLI entrypoint (`--config`, `--dry-run`) |
| `pipeline/c5/run_all.py` | Master pipeline orchestrator connecting WP0–WP9 |
| `pipeline/c5/common.py` | Shared configuration, schemas, hash snapshot, and ID formatters |
| `pipeline/c5/wp0_readiness.py` | WP0: Setup validation, source manifest generation, 5-NPI probe |
| `pipeline/c5/wp1_targets.py` | WP1: Load Health Net CA targets, entity types, deactivation flags |
| `pipeline/c5/wp2_claims.py` | WP2: Load evidence claims (NPPES primary/pl/ep, CMS DAC, PO box flags) |
| `pipeline/c5/wp3_sites.py` | WP3: USPS address normalization, Census batch geocoding, disk cache |
| `pipeline/c5/wp4_tin_sites.py` | WP4: TIN site set construction (org NPI, consensus, Medicare group) |
| `pipeline/c5/wp5_candidates.py` | WP5: Candidate generation and additive feature scoring |
| `pipeline/c5/wp6_choose.py` | WP6: Stale & conflict checks, winner selection, verified/probable labelling |
| `pipeline/c5/wp7_leftovers.py` | WP7: Unverified leftover queue prioritized by TIN size, quote check |
| `pipeline/c5/wp8_validation.py` | WP8: Stratified calibration sample generation and precision metrics |
| `pipeline/c5/wp9_summary.py` | WP9: Comprehensive execution summary and verification stats |

## Conventions (per project LOGIC.md)

- Stream large files; never load the full NPPES CSV into memory (DuckDB scans in ~14 s).
- Keep identifiers as text: NPI (10 digits, zero-padded), TIN with dash (`94-1156581`).
- Write large outputs as Parquet; CSV only for small tables.
- Every script accepts `--help` with path arguments defaulting relative to the repo root.
- Each step writes `c5_summary.json` with key counts.
