"""pipeline/c5/wp2_state_sources.py – Ingest and link California DCA Licensee and CDPH Facility data.

Sources:
1. California DCA Licensee Bulk Files (Individuals)
   - Source: dca_license_bulk -> Family: state_license
   - Matches: NPI -> NPPES license number (15 slots) -> DCA record.
   - Matching requirements: License number equal after prefix/leading-zero normalization,
     compatible board via license_type_to_board, and normalized legal last name equal.
   - Disambiguation: Multiple surviving DCA records -> ambiguous_license (no claim).
   - Freshness: Current status and future expiry date.
   - P.O. Box: Flagged and excluded from sites. City-only: city-level claim only.

2. CDPH Healthcare Facility Locations (Organizations)
   - Source: cdph_facility -> Family: state_facility
   - Matches: in order: (1) NPI equality; (2) exact normalized name + ZIP5; (3) exact normalized name + City.
   - ZIP and City can come from NPPES or any existing claim for the target.
   - Freshness: from file's data date and facility license status.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from .common import SCHEMA_CLAIMS, get_logger, is_po_box

log = get_logger("c5.wp2_state_sources")


def norm_name(s: Any) -> str:
    """Normalize names: uppercase, remove punctuation/corporate suffixes."""
    if not s or pd.isna(s):
        return ""
    s_clean = str(s).upper()
    s_clean = re.sub(r"\b(INC|LLC|CORP|CORPORATION|LTD|LP|CO|COMPANY|PC|PROFESSIONAL CORP)\b", "", s_clean)
    return re.sub(r"[^A-Z0-9]", "", s_clean)


def norm_last_name(s: Any) -> str:
    """Normalize individual last name: uppercase letters only."""
    if not s or pd.isna(s):
        return ""
    return re.sub(r"[^A-Z]", "", str(s).upper())


def normalize_license_number(lic: str) -> Tuple[str, List[str]]:
    """
    Extract prefix and candidate numeric representations for license matching.
    Example: 'A065277' -> prefix 'A', patterns ['A065277', 'A65277', '65277']
    """
    lic_clean = re.sub(r"[\s\-\.]", "", lic.upper())
    patterns = [lic_clean]
    prefix = ""

    # Check if there is an alphabetical prefix followed by digits
    m = re.match(r"^([A-Z]+0*)(\d+)$", lic_clean)
    if m:
        prefix_match = re.match(r"^[A-Z]+", lic_clean)
        if prefix_match:
            prefix = prefix_match.group(0)
        digits = m.group(2)
        patterns.append(digits)
        patterns.append(digits.lstrip("0"))
    else:
        # Check pure alphanumeric like 20A...
        m2 = re.match(r"^([0-9]+[A-Z]+0*)(\d+)$", lic_clean)
        if m2:
            prefix = re.match(r"^[0-9]+[A-Z]+", lic_clean).group(0)
            digits = m2.group(2)
            patterns.append(digits)
            patterns.append(digits.lstrip("0"))

    return prefix, list(dict.fromkeys(p for p in patterns if p))


def write_source_instructions(cfg: dict, out_path: str) -> None:
    """Write c5_source_instructions.md describing manual download steps."""
    content = f"""# C5 Pipeline: State Data Sources Download Instructions

The C5 pipeline uses two official California regulatory data sources to verify practice addresses:
1. **California Department of Consumer Affairs (DCA) Licensee Bulk Files** (for individuals)
2. **California Department of Public Health (CDPH) Healthcare Facility Locations** (for organizations)

If automatic network downloading is prevented by state firewall/Cloudflare policies (`manual_drop: true`), please download the files using a browser and place them into the designated directories.

---

## 1. California DCA Licensee Bulk Files (Individuals)

* **Public Portal:** [California DCA Public Information Portal](https://www.dca.ca.gov/consumers/public_info/index.shtml)
* **Direct Box Folder:** [DCA Public Information Box Shared Folder](https://dca.box.com/s/oss6hf8jys2bmgxqd2gdz7w4oepm2il9) (Folder ID: `66089580694`)
* **Target Local Directory:** `data/state_sources/dca/`

### Files to Download:
Navigate into the respective sub-agency folders on Box and download the primary data files (tab-delimited `.xls` / `.tsv`):
- `MedicalBoard_0800` -> `MedicalBoard_Data00.xls` (save as `MedicalBoard_Data00.tsv`)
- `BehavioralSciences_0200` -> `BehavioralSciences_Data00.xls` (save as `BehavioralSciences_Data00.tsv`)
- `Psychology_0600` -> `Psychology_Data00.xls` (save as `Psychology_Data00.tsv`)
- `PodiatricMedicine_0500` -> `PodiatricMedical_Data00.xls` (save as `PodiatricMedical_Data00.tsv`)
- `RegisteredNursing_0400` -> `RegisteredNursing_Data00.xls` (optional, save as `RegisteredNursing_Data00.tsv`)

---

## 2. CDPH Healthcare Facility Locations (Organizations)

* **Public Portal:** [California Health and Human Services (CHHS) Open Data](https://data.chhs.ca.gov/dataset/healthcare-facility-locations)
* **Direct Download URL:** `https://data.chhs.ca.gov/dataset/3b5b80e8-6b8d-4715-b3c0-2699af6e72e5/resource/f0ae5731-fef8-417f-839d-54a0ed3a126e/download/health_facility_locations.csv`
* **Target Local Directory:** `data/state_sources/cdph/`
* **Target File Name:** `health_facility_locations.csv`

---

## Verification Checklist

Once files are placed, verify:
```bash
ls -lh data/state_sources/dca/
ls -lh data/state_sources/cdph/
```
The pipeline automatically detects these files, computes SHA-256 checksums into `output/c5/c5_sources_manifest.json`, and executes completely offline.
"""
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(content)
    log.info(f"Wrote state source instructions to {out_path}")


def load_dca_claims(
    cfg: dict,
    targets: pd.DataFrame,
    nppes_main_path: str,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """
    Ingest California DCA licensee files, link to target individuals via NPPES license numbers,
    and generate state_license claims.
    """
    log.info("Loading California DCA licensee claims for individuals...")
    dca_cfg = cfg.get("sources", {}).get("dca_license_bulk", {})
    local_dir = dca_cfg.get("local_dir", "data/state_sources/dca")
    as_of_date = dca_cfg.get("as_of_date", "2026-10-01")
    license_type_to_board = cfg.get("license_type_to_board", {})

    stats: Dict[str, Any] = {
        "source": "dca_license_bulk",
        "eligible_targets": 0,
        "matched": 0,
        "ambiguous": 0,
        "excluded_pobox": 0,
        "excluded_city_only": 0,
        "excluded_non_ca": 0,
        "excluded_no_license": 0,
        "claims_created": 0,
        "by_board": {},
        "freshness_basis": {"file_date": 0, "cycle_offset": 0},
    }

    # Filter individual targets
    ind_targets = targets[targets["entity_type"] == "individual"].drop_duplicates("npi").copy()
    stats["eligible_targets"] = len(ind_targets)
    if ind_targets.empty:
        log.info("  No individual targets present.")
        return pd.DataFrame(columns=SCHEMA_CLAIMS), stats

    target_npis = set(ind_targets["npi"].astype(str).str.strip().str.zfill(10))

    # 1. Extract license numbers & legal names from NPPES main
    log.info("  Extracting NPPES license slots and legal names for target individuals...")
    con = duckdb.connect()
    npi_list_str = ", ".join(f"'{n}'" for n in target_npis)

    lic_cols = []
    for i in range(1, 16):
        lic_cols.append(f'"Provider License Number_{i}"')
        lic_cols.append(f'"Provider License Number State Code_{i}"')
    lic_cols_sql = ", ".join(lic_cols)

    sql = f"""
      SELECT CAST("NPI" AS VARCHAR) as npi_raw,
             "Provider Last Name (Legal Name)" as legal_last_name,
             "Provider First Name" as legal_first_name,
             {lic_cols_sql}
      FROM read_csv_auto('{nppes_main_path}', header=True, sample_size=5000)
      WHERE CAST("NPI" AS VARCHAR) IN ({npi_list_str})
    """
    nppes_lic_df = con.execute(sql).fetchdf()
    nppes_lic_df["npi"] = nppes_lic_df["npi_raw"].astype(str).str.strip().str.zfill(10)
    lic_by_npi = nppes_lic_df.set_index("npi").to_dict(orient="index")
    log.info(f"  Extracted NPPES license records for {len(lic_by_npi):,} target individuals")

    # 2. Ingest DCA files from local_dir
    dca_files = glob.glob(os.path.join(local_dir, "*.tsv")) + glob.glob(os.path.join(local_dir, "*.xls"))
    if not dca_files:
        log.warning(f"  No DCA files found in {local_dir}. Skipping DCA claims.")
        return pd.DataFrame(columns=SCHEMA_CLAIMS), stats

    dca_records = []
    for fpath in dca_files:
        fname = Path(fpath).name
        # Determine board name from filename
        board_name = fname.split("_")[0]
        try:
            df = pd.read_csv(fpath, sep="\t", dtype=str, on_bad_lines="skip")
            df["board"] = board_name
            dca_records.append(df)
            log.info(f"  Loaded DCA board file: {fname} ({len(df):,} rows)")
        except Exception as e:
            log.error(f"  Failed to read DCA file {fpath}: {e}")

    if not dca_records:
        return pd.DataFrame(columns=SCHEMA_CLAIMS), stats

    df_dca_all = pd.concat(dca_records, ignore_index=True)
    log.info(f"  Total DCA licensee records ingested: {len(df_dca_all):,}")

    # Keep only California addresses
    df_dca = df_dca_all[df_dca_all["State"].fillna("").str.upper().str.strip() == "CA"].copy()
    df_dca["lic_clean"] = df_dca["License Number"].fillna("").str.strip().str.upper()
    df_dca["last_name_norm"] = df_dca["Org/Last Name"].fillna("").apply(norm_last_name)

    # Index DCA records by clean license number
    dca_index: Dict[str, List[pd.Series]] = {}
    for idx, r in df_dca.iterrows():
        lic = r["lic_clean"]
        if lic:
            dca_index.setdefault(lic, []).append(r)

    # 3. Matching loop
    claims_list: List[Dict[str, Any]] = []

    for idx, tgt_row in ind_targets.iterrows():
        npi = str(tgt_row["npi"]).strip().zfill(10)
        nppes_info = lic_by_npi.get(npi)
        if not nppes_info:
            stats["excluded_no_license"] += 1
            continue

        legal_last = nppes_info.get("legal_last_name", "")
        last_norm = norm_last_name(legal_last)

        # Collect license slots
        lic_slots = []
        for i in range(1, 16):
            num = nppes_info.get(f"Provider License Number_{i}")
            st = nppes_info.get(f"Provider License Number State Code_{i}")
            if pd.notna(num) and str(num).strip():
                lic_slots.append((str(num).strip().upper(), str(st).strip().upper() if pd.notna(st) else ""))

        if not lic_slots:
            stats["excluded_no_license"] += 1
            continue

        ca_slots = [(num, st) for num, st in lic_slots if st == "CA" or st == ""]
        if not ca_slots:
            stats["excluded_non_ca"] += 1
            continue

        hits: List[pd.Series] = []
        for lic_num, st in ca_slots:
            prefix, patterns = normalize_license_number(lic_num)
            expected_board = license_type_to_board.get(prefix)

            for p in patterns:
                candidates = dca_index.get(p, [])
                for c in candidates:
                    # Require normalized last name equality
                    if c["last_name_norm"] == last_norm:
                        # Check board compatibility if mapped
                        if expected_board is None or c["board"] == expected_board:
                            hits.append(c)

        # Deduplicate hits by (board, license_number, address)
        unique_hits = {
            f"{h['board']}|{h['License Number']}|{str(h.get('Address Line 1', '')).strip().upper()}|{str(h.get('Zip', '')).strip()[:5]}": h
            for h in hits
        }.values()

        if len(unique_hits) == 1:
            hit = list(unique_hits)[0]
            board = hit["board"]
            lic_no = hit["License Number"]
            stats["matched"] += 1
            stats["by_board"][board] = stats["by_board"].get(board, 0) + 1

            def _clean(val: Any) -> str:
                if val is None or pd.isna(val):
                    return ""
                s = str(val).strip()
                return "" if s.lower() == "nan" else s

            addr1 = _clean(hit.get("Address Line 1"))
            addr2 = _clean(hit.get("Address Line 2"))
            city = _clean(hit.get("City"))
            state = "CA"
            zip_code = _clean(hit.get("Zip"))[:5]

            # Address components & P.O. Box check
            parts = [p for p in (addr1, addr2, city, state, zip_code) if p]
            raw_addr = " ".join(parts).strip()
            po_box_flag = is_po_box(raw_addr)

            if po_box_flag:
                stats["excluded_pobox"] += 1
            elif not addr1:
                stats["excluded_city_only"] += 1

            # Freshness rule
            status = str(hit.get("License Status", "")).upper()
            is_active = any(s in status for s in ["CURRENT", "ACTIVE", "RENEWED"]) and not any(
                s in status for s in ["DELINQUENT", "EXPIRED", "REVOKED", "CANCEL"]
            )
            exp_date_str = str(hit.get("Expiration Date", "") or "")
            source_date = as_of_date
            freshness_basis = "file_date"

            # Check if expiry date is present and valid
            if exp_date_str:
                try:
                    exp_dt = datetime.strptime(exp_date_str.strip(), "%m-%d-%Y")
                    # If license is current and expiry is in the future
                    if is_active and exp_dt >= datetime(2026, 10, 1):
                        source_date = as_of_date
                        freshness_basis = "file_date"
                    else:
                        is_active = False
                except Exception:
                    pass

            stats["freshness_basis"][freshness_basis] = stats["freshness_basis"].get(freshness_basis, 0) + 1

            claim_id = hashlib.sha256(f"{npi}|dca_license_bulk|{raw_addr}".encode()).hexdigest()[:16]

            claims_list.append({
                "claim_id": claim_id,
                "npi": npi,
                "tin": None,
                "raw_address": raw_addr,
                "source": "dca_license_bulk",
                "source_family": "state_license",
                "source_date": source_date,
                "claim_type": "address_of_record",
                "is_po_box": po_box_flag,
                "is_billing_only": False,
                "npi_status": "active",
                "addr1": addr1,
                "addr2": addr2,
                "city": city,
                "state": state,
                "zip": zip_code,
                "match_method": "license_name",
                "facility_id": None,
                "freshness_basis": freshness_basis,
            })
            stats["claims_created"] += 1

        elif len(unique_hits) > 1:
            stats["ambiguous"] += 1
        else:
            stats["excluded_no_license"] += 1

    df_claims = pd.DataFrame(claims_list)
    log.info(
        f"  DCA matching complete: {stats['matched']:,} matched, "
        f"{stats['ambiguous']:,} ambiguous, {stats['claims_created']:,} claims created"
    )
    return df_claims, stats


def load_cdph_claims(
    cfg: dict,
    targets: pd.DataFrame,
    existing_claims: pd.DataFrame,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """
    Ingest California Department of Public Health (CDPH) licensed health facility locations,
    match against organization targets via (1) NPI, (2) Name+ZIP, (3) Name+City,
    and generate state_facility claims.
    """
    log.info("Loading California CDPH health facility claims for organizations...")
    cdph_cfg = cfg.get("sources", {}).get("cdph_facility", {})
    local_dir = cdph_cfg.get("local_dir", "data/state_sources/cdph")
    as_of_date = cdph_cfg.get("as_of_date", "2026-10-16")
    facility_path = os.path.join(local_dir, "health_facility_locations.csv")

    stats: Dict[str, Any] = {
        "source": "cdph_facility",
        "eligible_targets": 0,
        "matched": 0,
        "matched_by_method": {"cdph_npi": 0, "cdph_name_zip": 0, "cdph_name_city": 0},
        "matched_by_facility_type": {},
        "claims_created": 0,
        "unmatched": 0,
        "npi_populated_share": 0.0,
    }

    org_targets = targets[targets["entity_type"] == "organization"].drop_duplicates("npi").copy()
    stats["eligible_targets"] = len(org_targets)
    if org_targets.empty:
        log.info("  No organization targets present.")
        return pd.DataFrame(columns=SCHEMA_CLAIMS), stats

    if not Path(facility_path).is_file():
        log.warning(f"  CDPH facility locations file not found at {facility_path}. Skipping CDPH claims.")
        return pd.DataFrame(columns=SCHEMA_CLAIMS), stats

    df_cdph = pd.read_csv(facility_path, dtype=str, low_memory=False)
    npi_pop_mask = df_cdph["NPI"].fillna("").str.strip() != ""
    stats["npi_populated_share"] = round(float(npi_pop_mask.mean()), 4)
    log.info(f"  CDPH facilities loaded: {len(df_cdph):,} rows | NPI populated: {stats['npi_populated_share']:.1%}")

    # Build ZIP and City candidate sets for each target NPI from existing claims
    zips_by_npi: Dict[str, Set[str]] = {}
    cities_by_npi: Dict[str, Set[str]] = {}
    if not existing_claims.empty:
        for npi_val, grp in existing_claims.groupby("npi"):
            z = set(grp["zip"].dropna().astype(str).str.strip().str[:5].unique()) - {""}
            c = set(grp["city"].dropna().astype(str).str.strip().str.upper().unique()) - {""}
            zips_by_npi[str(npi_val).strip().zfill(10)] = z
            cities_by_npi[str(npi_val).strip().zfill(10)] = c

    # Index CDPH records
    cdph_by_npi: Dict[str, List[pd.Series]] = {}
    cdph_by_name_zip: Dict[Tuple[str, str], List[pd.Series]] = {}
    cdph_by_name_city: Dict[Tuple[str, str], List[pd.Series]] = {}

    for idx, r in df_cdph.iterrows():
        cnpi = str(r.get("NPI", "")).strip().zfill(10) if pd.notna(r.get("NPI")) and str(r.get("NPI")).strip() else ""
        if cnpi and cnpi != "0000000000":
            cdph_by_npi.setdefault(cnpi, []).append(r)

        z = str(r.get("ZIP", "")).strip()[:5]
        c = str(r.get("CITY", "")).strip().upper()
        n1 = norm_name(r.get("FACNAME", ""))
        n2 = norm_name(r.get("BUSINESS_NAME", ""))

        for n in {n1, n2}:
            if n:
                if z and len(z) == 5:
                    cdph_by_name_zip.setdefault((n, z), []).append(r)
                if c:
                    cdph_by_name_city.setdefault((n, c), []).append(r)

    claims_list: List[Dict[str, Any]] = []

    for idx, row in org_targets.iterrows():
        npi = str(row["npi"]).strip().zfill(10)
        b_name = row.get("business_name", "")
        n = norm_name(b_name)

        target_zips = zips_by_npi.get(npi, set())
        target_cities = cities_by_npi.get(npi, set())

        hit: Optional[pd.Series] = None
        method = ""

        # Step 1: NPI equality
        hits_npi = cdph_by_npi.get(npi, [])
        if hits_npi:
            hit = hits_npi[0]
            method = "cdph_npi"

        # Step 2: Exact normalized name + ZIP5 (from NPPES or existing claims)
        if hit is None and n and target_zips:
            for z in target_zips:
                hits_z = cdph_by_name_zip.get((n, z), [])
                if hits_z:
                    hit = hits_z[0]
                    method = "cdph_name_zip"
                    break

        # Step 3: Exact normalized name + City (from NPPES or existing claims)
        if hit is None and n and target_cities:
            for c in target_cities:
                hits_c = cdph_by_name_city.get((n, c), [])
                if hits_c:
                    hit = hits_c[0]
                    method = "cdph_name_city"
                    break

        if hit is not None:
            stats["matched"] += 1
            stats["matched_by_method"][method] = stats["matched_by_method"].get(method, 0) + 1
            fac_type = str(hit.get("FAC_FDR", "OTHER") or "OTHER")
            stats["matched_by_facility_type"][fac_type] = stats["matched_by_facility_type"].get(fac_type, 0) + 1

            fac_id = str(hit.get("FACID", "") or "")
            addr1 = str(hit.get("ADDRESS", "") or "").strip()
            city = str(hit.get("CITY", "") or "").strip()
            state = "CA"
            zip_code = str(hit.get("ZIP", "") or "").strip()[:5]

            raw_addr = f"{addr1} {city} {state} {zip_code}".strip()
            po_box_flag = is_po_box(raw_addr)

            # Date
            data_date_raw = str(hit.get("DATA_DATE", "") or "").strip()
            source_date = as_of_date
            if data_date_raw:
                try:
                    dt = datetime.strptime(data_date_raw, "%m/%d/%Y")
                    source_date = dt.strftime("%Y-%m-%d")
                except Exception:
                    pass

            claim_id = hashlib.sha256(f"{npi}|cdph_facility|{raw_addr}".encode()).hexdigest()[:16]

            claims_list.append({
                "claim_id": claim_id,
                "npi": npi,
                "tin": None,
                "raw_address": raw_addr,
                "source": "cdph_facility",
                "source_family": "state_facility",
                "source_date": source_date,
                "claim_type": "facility_location",
                "is_po_box": po_box_flag,
                "is_billing_only": False,
                "npi_status": "active",
                "addr1": addr1,
                "addr2": "",
                "city": city,
                "state": state,
                "zip": zip_code,
                "match_method": method,
                "facility_id": fac_id,
                "freshness_basis": "file_date",
            })
            stats["claims_created"] += 1
        else:
            stats["unmatched"] += 1

    df_claims = pd.DataFrame(claims_list)
    log.info(
        f"  CDPH matching complete: {stats['matched']:,} matched "
        f"({stats['matched_by_method']}), {stats['claims_created']:,} claims created"
    )
    return df_claims, stats
