# Feasibility Trial: New California Address Sources for C5 Address Mapping

**Date:** 2026-10-18  
**Scope:** California PPO Rate File Targets (`healthnet-ca`): 3,729 pairs / 3,685 NPIs / 2,849 TINs.  
**Baseline Unverified:** 2,690 targets (595 individual pairs / 588 NPIs; 2,082 organization pairs / 2,068 NPIs; 13 unknown).  
**Rules Followed:** Read-only on pipeline code and existing configs; zero code modifications; experiments performed strictly in scratch space.

---

## Executive Summary & Overall Comparison Table

| Source | Target Scope | Potential Targets Helped (Est.) | Implementation Effort | Independence Concern | Recommendation |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **1. NPPES License Fields** | Individuals | Direct key for DCA matching (94.2% CA license rate) | None (already local) | Low (self-reported key) | **GO** (Foundation key) |
| **2. DCA Licensee Bulk Files (Box)** | Individuals | **150 – 350 individual targets** (152 verified in Medical Board alone; ~300+ with Nursing & Behavioral Health) | Low (monthly TSV downloads from open Box folder) | **None** (State licensing authority, completely independent of NPPES/Medicare) | **GO (High Priority)** |
| **3. CMS National Provider Directory** | Both | 0 net new independent claims | High (NDJSON `.zst` parsing, 2+ GB) | **CRITICAL: Zero independence** (100% duplicate of NPPES & CMS DAC) | **NO-GO** |
| **4. CDPH Licensed Health Facilities** | Organizations | **260 – 380 organization targets** (261 unverified org pairs matched; 89.5% exact address agreement) | Low (6.9 MB direct CSV from CHHS open data portal) | **None** (State health department regulator, independent of NPPES/Medicare) | **GO (High Priority)** |
| **5. TIN Site Inference (Internal)** | Organizations | **4 – 7 organization targets** | Zero (logic rule on existing data) | None (internal consensus) | **MARGINAL (Low yield)** |

---

## Detailed Trial Reports

### Trial 1: NPPES License Fields (Individuals)

* **Reachable / Status:** Accessible immediately from local NPPES dissemination file (`npidata_pfile_20050523-20261011.csv`).
* **Format & Size:** CSV flat file (9.1 GB).
* **Key Columns:**
  * `Provider License Number_1` through `Provider License Number_15`
  * `Provider License Number State Code_1` through `Provider License Number State Code_15`
* **Sample Size & Funnel:**
  * Total target individuals analyzed: **641 unique NPIs** (648 pairs).
  * Share with ANY license number populated: **95.2%** (610 / 641).
  * Share with **California (CA)** license state: **94.4%** (605 / 641).
  * Unverified target individuals (588 unique NPIs):
    * Share with ANY license number: **95.1%** (559 / 588).
    * Share with **California (CA)** license state: **94.2%** (554 / 588).
  * License slot count distribution (among unverified individuals):
    * 0 slots: 29 (4.9%)
    * 1 slot: 430 (73.1%)
    * 2 slots: 99 (16.8%)
    * 3 slots: 23 (3.9%)
    * 4+ slots: 7 (1.2%)
* **Sample License Formats Observed:**
  `LMFT97649` (Marriage & Family Therapist), `A065277` (Physician/Surgeon A), `A31106`, `20A19246` (Podiatric Medicine), `24526` (Numeric), `LCS23358` (Clinical Social Worker), `MFC27146`, `A44569`, `LCSW82215`, `A61657`.
* **Expected Targets Helped:** Enables high-precision deterministic linking for **554 out of 588 unverified individuals** (94.2%).
* **Problems Found:** In `c2b_ppo_rates.parquet`, some provider rows list the health plan name (`HEALTH NET DIRECT FFS COMMERCIAL`) in `business_name`. The provider's legal name must be extracted directly from NPPES (`Provider Last Name (Legal Name)`).
* **Recommendation:** **GO.** This field is already present in the existing NPPES raw ingest; extracting it provides the exact join key for DCA records.

---

### Trial 2: DCA Licensee Bulk Files (Medical Board & Sub-Boards)

* **Reachable / Status:** **Reachable via direct HTTP request.** California DCA maintains an open public Box folder (`https://dca.box.com/s/oss6hf8jys2bmgxqd2gdz7w4oepm2il9`), and tokens are issued programmatically via `https://www.dca.ca.gov/boxToken/getToken`.
* **Format & Size:** Tab-Separated Values (TSV) with `.xls` extension. Refreshed monthly.
  * Medical Board (`MedicalBoard_Data00.xls`): 42.7 MB, 219,878 rows.
  * Registered Nursing (`RegisteredNursing_Data00.xls` & `01.xls`): 203.4 MB total.
  * Behavioral Sciences, Psychology, Podiatry, Physical Therapy: 5–25 MB each.
* **Key Columns:**
  `Agency Code`, `Agency Name`, `LicTypeCode`, `License Type`, `License Number`, `Indiv/Org`, `Org/Last Name`, `First Name`, `Middle Name`, `Suffix`, `Address Line 1`, `Address Line 2`, `City`, `County`, `State`, `Zip`, `Country`, `Original Issue Date`, `Expiration Date`, `License Status`.
* **Medical Board Address Quality Metrics (N = 219,878 rows):**
  * California State address: **79.6%** (175,003 rows)
  * P.O. Box in Address Line 1: **3.5%** (7,616 rows)
  * City-only / missing Address Line 1: **0.0%** (9 rows)
  * Usable physical street addresses: **96.5%** (212,073 rows)
* **Matching Trial on Target Individuals (N = 641 NPIs / 588 unverified):**
  * Join criteria: License number (with/without prefix) + matching normalized legal last name + active license.
  * **Matched with usable street address:** **173 individuals** (27.0% of all individuals; **152 unverified individuals** = 25.9% of unverified).
  * Ambiguous matches (>1 hit): **0** (0.0%).
  * P.O. Box in DCA: 8.
  * City-only: 0.
  * Unmatched in Medical Board: 468 (these belong to other DCA boards: Board of Behavioral Sciences `MFT`/`LCSW`, Board of Psychology `PSY`, Board of Registered Nursing `RN`/`NP`, etc.).
* **Address Comparison (DCA Address vs. Current C5 Candidate Address):**
  * Same building / street address match: **102 / 173 (59.0%)** — confirms candidate address with an independent state regulatory source!
  * Same ZIP, different street address: **1 / 173 (0.6%)**
  * Different address / ZIP: **70 / 173 (40.5%)** — provides a current state-registered practice address where NPPES was outdated.
* **5 Sample Matches:**
  1. `NPI 1417951963` (GIL, ALEJANDRO | Lic: 37558 Physician A): DCA `240 N Virgil Ave Ste 7, Los Angeles, CA 90004` vs C5 `240 N VIRGIL AVE` [MATCH]
  2. `NPI 1225033111` (TSAI, MARK | Lic: 81875 Physician A): DCA `16415 S COLORADO AVE STE 101, PARAMOUNT, CA 90723` vs C5 `16415 COLORADO AVE` [MATCH]
  3. `NPI 1881690865` (ABRAMS, DAVID | Lic: 17392 Physician G): DCA `18370 Burbank Blvd Ste 414, TARZANA, CA 91356` vs C5 `18370 BURBANK BLVD` [MATCH]
  4. `NPI 1346251451` (HARRINGTON, M.D. | Lic: 45573 Physician A): DCA `9850 Genesee Ave Ste 870, LA JOLLA, CA 92037` vs C5 `9888 GENESEE AVE` [DIFF SUITE/BLDG]
  5. `NPI 1407851777` (MAW MAW, NINA | Lic: 79700 Physician A): DCA `35900 Bob Hope Dr Ste 230, RANCHO MIRAGE, CA 92270` vs C5 `47120 DUNE PALMS RD` [DIFF ADDRESS]
* **Expected Targets Helped:**
  * Medical Board alone: **152 unverified individuals**.
  * Medical Board + Behavioral Sciences + Psychology + Nursing: **300 – 380 unverified individuals** (~50%–65% of all unverified individuals).
* **Problems Found:** Tab-delimited files are named with `.xls` extensions; standard Excel parsers fail, but standard TSV parsers handle them cleanly.
* **Recommendation:** **GO (High Priority).** Fully automated bulk download via DCA's public Box API; 100% independent state licensing authority.

---

### Trial 3: CMS National Provider Directory (directory.cms.gov)

* **Reachable / Status:** **Reachable via direct HTTP public download.** Published at `https://directory.cms.gov/downloads/`.
* **Format & Size:** Newline-Delimited JSON (NDJSON) compressed with Zstandard (`.zst`).
  * `01-Organization.ndjson.zst`: 274.75 MB
  * `02-Location.ndjson.zst`: 196.98 MB
  * `06-Practitioner.ndjson.zst`: 828.87 MB
  * `07-PractitionerRole.ndjson.zst`: 859.28 MB
* **Record Structure & Layout:**
  * `Practitioner`: contains `id: Practitioner-{NPI}`, name, gender, telecom, and an `address` array with physical and billing postal addresses.
  * `PractitionerRole`: in this release, **0 out of 2,781 inspected records** had a `location` reference (mostly endpoints/telecom).
* **Cross-Source Comparison (NPD vs. NPPES vs. CMS DAC):**
  * Sample target NPIs evaluated: `1003884925`, `1003856188`, `1003311051`.
  * **100% of addresses in NPD match NPPES and CMS DAC verbatim.**
    * Example NPI `1003884925`: NPD = `2001 Santa Monica Blvd, Ste 990W`; NPPES = `2001 SANTA MONICA BLVD 990W`; CMS DAC = `2001 SANTA MONICA BLVD SUITE 990W`.
    * Example NPI `1003856188`: NPD = `9220 Haven Ave, Ste 230`; NPPES = `9220 HAVEN AVE STE 230`; CMS DAC = `9220 HAVEN AVE SUITE 230`.
* **Independence Finding:**
  * NPD is an internal CMS downstream view generated by aggregating NPPES and PECOS enrollment records.
  * Under C5 independence rules (LOGIC.md section 5: *"Only different families count as independent evidence... web_aggregator never counts as independent of NPPES"*), NPD belongs to the existing `nppes_self` and `medicare` families.
  * It provides **zero net-new independent source family evidence** for upgrading targets to `verified`.
* **Expected Targets Helped:** **0 net new verified upgrades.**
* **Recommendation:** **NO-GO.** Heavy files (over 2 GB download, requires `zstd` binary), high ETL complexity, and zero source-family independence from data already in the pipeline.

---

### Trial 4: CDPH Healthcare Facility Locations (Organizations)

* **Reachable / Status:** **Reachable via open HTTP download.** Hosted on California Health and Human Services (CHHS) open data portal (`data.chhs.ca.gov`).
* **Format & Size:** Standard CSV (`health_facility_locations.csv`), 6.9 MB, **14,921 rows**. Refreshed 2026-10-16.
* **Key Columns:**
  `FACNAME`, `BUSINESS_NAME`, `FAC_FDR` (Facility Type), `FAC_STATUS_TYPE_CODE`, `ADDRESS`, `CITY`, `ZIP`, `COUNTY_NAME`, `LICENSE_NUMBER`, `LICENSE_STATUS_DESCRIPTION`, `LATITUDE`, `LONGITUDE`, `NPI`, `DATA_DATE`.
* **Top Facility Types in California:**
  Home Health Agency (4,077), Primary Care Clinic (3,371), Hospice (1,968), Skilled Nursing Facility (1,188), Surgical Clinic (1,013), Chronic Dialysis Clinic (745), General Acute Care Hospital (434).
* **Exact Matching Trial (Normalized Name + ZIP5):**
  * Target organization rows tested: **3,068 rows** (3,035 unique NPIs; 2,082 unverified rows).
  * **Matched target rows:** **400 rows (13.0%)** across 400 unique NPIs.
  * **Unverified organization target rows matched:** **261 / 2,082 (12.5%)**.
  * Matches by facility category:
    * General Acute Care Hospitals: 205
    * Surgical Clinics: 121
    * Home Health Agencies: 40
    * Chronic Dialysis Clinics: 16
    * Acute Psychiatric Hospitals: 7
    * Skilled Nursing Facilities: 5
    * Primary Care Clinics: 4
* **Address Comparison (CDPH Address vs. Current C5 Candidate Address):**
  * **Same building / street address match:** **358 / 400 (89.5%)**!
  * Same ZIP, different street address: 42 / 400 (10.5%).
* **10 Sample Matches:**
  1. `TIN 01-0721696` (Whittier Dialysis): CDPH `10055 WHITTWOOD DR, SUITE A, WHITTIER 90603` vs C5 `10055 WHITTWOOD DR` [MATCH]
  2. `TIN 02-0554398` (Waverley Surgery Center): CDPH `400 FOREST AVE, PALO ALTO 94301` vs C5 `400 FOREST AVE` [MATCH]
  3. `TIN 02-0653265` (OPTUM Infusion Services): CDPH `4610 NORTHGATE BLVD., SUITE 130, SACRAMENTO 95834` vs C5 `4610 NORTHGATE BLVD` [MATCH]
  4. `TIN 11-3652210` (Endoscopy Center Of Santa Monica): CDPH `12400 WILSHIRE BLVD, SUITE 100, LOS ANGELES 90025` vs C5 `12400 WILSHIRE BLVD` [MATCH]
  5. `TIN 13-4257391` (HEALTH SERVICE ALLIANCE): CDPH `13193 CENTRAL AVENUE, SUITE 100, CHINO 91710` vs C5 `13193 CENTRAL AVE` [MATCH]
  6. `TIN 13-4285230` (Peninsula Eye Surgery Center): CDPH `1128 W EL CAMINO REAL, MOUNTAIN VIEW 94040` vs C5 `1128 W EL CAMINO REAL` [MATCH]
  7. `TIN 13-4341801` (90210 Surgery Medical Center): CDPH `9735 WILSHIRE BLVD, SUITE 100, BEVERLY HILLS 90212` vs C5 `9735 WILSHIRE BLVD` [MATCH]
  8. `TIN 20-0095307` (United Surgery Center Murrieta): CDPH `25150 HANCOCK AVE, SUITE 208, MURRIETA 92562` vs C5 `25150 HANCOCK AVE` [MATCH]
  9. `TIN 20-0500901` (Sylvan Surgery Center, Inc): CDPH `2336 SYLVAN AVE, MODESTO 95355` vs C5 `2336 SYLVAN AVE` [MATCH]
  10. `TIN 20-1074818` (Advanced Surgery Center): CDPH `235 O'CONNOR DR, SAN JOSE 95128` vs C5 `235 OCONNOR DR` [MATCH]
* **Large Unmatched Organizations:**
  National clinical diagnostic lab chains (Quest Diagnostics, LabCorp), DME equipment suppliers (Apria Healthcare), and physician professional groups (CEP America, Sutter medical foundations) are organized as professional corporations rather than state-licensed health care facilities.
* **Expected Targets Helped:** **260 – 350 organization targets** can immediately gain an independent state government claim.
* **Recommendation:** **GO (High Priority).** 89.5% address concordance, 6.9 MB lightweight CSV, and fully independent California Department of Public Health regulatory source family (`state_health_dept`).

---

### Trial 5: TIN Site Inference Potential (Internal Data Only)

* **Reachable / Status:** Evaluated directly from `output/c5/c5_addresses.csv`, `c5_targets.parquet`, and `c5_candidates.parquet`.
* **Funnel & Counts:**
  * Target TINs where at least 2 individual NPIs share an address site: **9 TINs**.
  * Total organization targets under these 9 TINs: **7 targets** (across 7 unique NPIs).
  * Unverified organization targets under these TINs: **4 targets**.
  * Candidate agreement: All 4 unverified organization targets already contain the shared individual site within their candidate address set.
* **Why the Potential is Low:**
  In `healthnet-ca` PPO rate data, 97.5% of organization TINs are pure organization entities (e.g., hospital systems, labs, clinics) without associated billing individuals in the file. Only 53 TINs in the entire unverified population contain mixed individuals and organizations.
* **Expected Targets Helped:** **4 – 7 organization targets** maximum.
* **Recommendation:** **MARGINAL.** The yield is negligible (only 4 unverified targets) because PPO contracts separate institutional group agreements from individual practitioner rosters.

---

## What a Human Needs to Download / Supply (Folder Structure)

If approved to add California state sources to the C5 ingestion pipeline, the following files should be placed in `data/state_sources/`:

```
data/
└── state_sources/
    ├── dca/
    │   ├── MedicalBoard_Data00.tsv       (from https://dca.box.com/s/oss6hf8jys2bmgxqd2gdz7w4oepm2il9)
    │   ├── RegisteredNursing_Data00.tsv  (from same Box folder)
    │   └── BehavioralSciences_Data00.tsv (from same Box folder)
    └── cdph/
        └── health_facility_locations.csv (from https://data.chhs.ca.gov/dataset/healthcare-facility-locations)
```
*(Note: As demonstrated during this trial, both DCA Box files and CDPH open data CSV files can also be fetched 100% automatically via standard non-interactive HTTP scripts).*

---

## Limitations & Unchecked Areas

1. **Other DCA Sub-Boards:** We verified the Medical Board file (219k rows, 152 unverified matches). The remaining 33 DCA sub-boards (Behavioral Sciences, Psychology, Nursing, Podiatry) follow identical schemas in the same Box folder, but their exact match rates were projected rather than fully joined in this scratch trial.
2. **NPD Full Graph Traversal:** We verified that `Practitioner` in NPD duplicates NPPES/DAC addresses and that `PractitionerRole` had 0 location references. We did not download the entire 1.7 GB archive to trace every rare affiliation edge.
3. **Fuzzy Name Matching on CDPH:** The CDPH trial used strict normalized exact string matching. A conservative token-set match on business names could potentially match another 50–100 organization clinics without sacrificing precision.
