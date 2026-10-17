"""pipeline/c5/wp1_targets.py – WP1: Build c5_targets.

One row per TIN-NPI from the Health Net CA rate file providers.
Enriches with: entity_type from NPPES, county_label from business name,
ppo_scope (strict / inclusive_only).

Since C2b step 6 (parallel-contract resolution) is still open, we treat
all Health Net CA pairs as ppo_scope=strict and mark them accordingly.

Output: config.paths.c5_targets (Parquet)
"""
from __future__ import annotations

import re
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from .common import (
    SCHEMA_TARGETS,
    extract_county_label,
    fmt_npi,
    fmt_tin,
    get_logger,
    require_file,
    resolve,
)

log = get_logger("c5.wp1")

# C2b has not yet published ppo_scope column; use strict for all healthnet-ca rows.
# Update this mapping when C2b step 6 delivers c2b_group_labels.csv.
_DEFAULT_PPO_SCOPE = "strict"


def _load_entity_types(npis: set, cfg: dict) -> pd.DataFrame:
    """Batch-lookup Entity Type Code from NPPES main for the target NPI set."""
    nppes_path = cfg["paths"]["nppes_main"]
    if not Path(nppes_path).is_file():
        log.warning("NPPES main not available; entity_type will be empty.")
        return pd.DataFrame(columns=["npi", "entity_type"])

    quoted = ", ".join(f"'{n}'" for n in npis)
    sql = f"""
      SELECT
        LPAD(CAST("NPI" AS VARCHAR), 10, '0') AS npi,
        "Entity Type Code"                    AS entity_type
      FROM read_csv_auto('{nppes_path}', header=True, sample_size=5000)
      WHERE CAST("NPI" AS VARCHAR) IN ({quoted})
    """
    con = duckdb.connect()
    df = con.execute(sql).df()
    con.close()
    df["entity_type"] = df["entity_type"].astype(str).str.strip().map(
        {"1": "individual", "2": "organization"}
    ).fillna("unknown")
    return df[["npi", "entity_type"]].drop_duplicates("npi")


def _load_deactivated(cfg: dict) -> set[str]:
    """Return set of NPI strings that appear in the NPPES deactivation report."""
    deact_path = cfg["paths"]["nppes_deact"]
    if not Path(deact_path).is_file():
        log.warning("Deactivation report not found; no NPIs excluded for deactivation.")
        return set()
    try:
        df = pd.read_excel(deact_path, dtype=str)
        # Column names vary; look for any column that looks like 'NPI'
        npi_col = next((c for c in df.columns if re.match(r"^npi$", c.strip(), re.I)), None)
        if npi_col is None and len(df) > 0:
            # First row in excel might be title
            if any("npi" in str(v).lower() for v in df.iloc[0].values):
                df.columns = [str(v).strip() for v in df.iloc[0].values]
                df = df.iloc[1:].reset_index(drop=True)
                npi_col = next((c for c in df.columns if re.match(r"^npi$", c.strip(), re.I)), None)
        if npi_col is None:
            log.warning(f"No NPI column in deactivation report. Columns: {df.columns.tolist()}")
            return set()
        return set(df[npi_col].dropna().astype(str).str.strip().str.zfill(10))
    except Exception as exc:
        log.warning(f"Could not read deactivation report: {exc}")
        return set()


def run_wp1(cfg: dict) -> pd.DataFrame:
    """Build and write c5_targets. Returns the DataFrame."""
    log.info("=== WP1: Build c5_targets ===")

    targets_path = require_file(cfg["paths"]["targets"], "targets")
    rate_file    = cfg["target_rate_file"]

    # --- Load raw rows ---
    raw = pd.read_csv(str(targets_path), dtype=str)
    raw = raw[raw["rate_file"] == rate_file].copy()

    # Standardise identifiers
    raw["npi"] = raw["npi"].apply(fmt_npi)
    raw["tin"] = raw["tin"].apply(fmt_tin)

    # Deduplicate TIN-NPI (keep one row; business_name from first occurrence)
    raw = raw.drop_duplicates(subset=["tin", "npi"])

    log.info(f"Raw pairs ({rate_file}): {len(raw):,}  NPIs: {raw.npi.nunique():,}  TINs: {raw.tin.nunique():,}")

    # --- County label ---
    raw["county_label"] = raw["business_name"].apply(extract_county_label)

    # --- PPO scope ---
    # Assumption A-C5-01: All healthnet-ca pairs are ppo_scope=strict
    # until C2b step 6 delivers a column. Log the assumption.
    raw["ppo_scope"] = _DEFAULT_PPO_SCOPE
    raw["n_ppo_codes"] = 0    # will be filled by C4 when available

    # --- Entity type from NPPES ---
    unique_npis = set(raw["npi"].unique())
    log.info(f"Looking up entity types for {len(unique_npis):,} NPIs in NPPES…")
    entity_df = _load_entity_types(unique_npis, cfg)
    log.info(f"Entity types found for {len(entity_df):,} NPIs")
    raw = raw.merge(entity_df, on="npi", how="left")
    raw["entity_type"] = raw["entity_type"].fillna("unknown")

    # --- Deactivation ---
    deact_npis = _load_deactivated(cfg)
    raw["npi_deactivated"] = raw["npi"].isin(deact_npis)
    n_deact = raw["npi_deactivated"].sum()
    if n_deact:
        log.warning(f"Deactivated NPIs in targets: {n_deact} (will be excluded from scoring but kept in table)")

    # --- Specialty code (placeholder; NUCC / HiLabs crosswalk filled later) ---
    raw["specialty_code"] = ""

    # --- Output ---
    out_cols = SCHEMA_TARGETS + ["npi_deactivated"]
    for col in out_cols:
        if col not in raw.columns:
            raw[col] = ""

    result = raw[out_cols].reset_index(drop=True)

    out_path = cfg["paths"]["c5_targets"]
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pandas(result), out_path, compression="snappy")
    log.info(f"c5_targets written: {len(result):,} rows → {out_path}")

    # ── Report ───────────────────────────────────────────────────────────────
    by_entity = result.groupby("entity_type").size().to_dict()
    by_scope  = result.groupby("ppo_scope").size().to_dict()
    log.info(f"WP1 DONE: {len(result):,} pairs | entity_type: {by_entity} | scope: {by_scope}")
    log.info(f"  county labels found: {(result.county_label != '').sum():,}")
    log.info(f"  deactivated NPIs: {n_deact}")

    # Expected size check (from prompt: ~3,780 pairs, 3,735 NPIs, 2,888 TINs)
    npis_actual = result["npi"].nunique()
    tins_actual = result["tin"].nunique()
    if abs(len(result) - 3780) > 50:
        log.warning(f"Pair count {len(result)} differs significantly from expected ~3,780")
    if abs(npis_actual - 3735) > 50:
        log.warning(f"NPI count {npis_actual} differs from expected ~3,735")
    if abs(tins_actual - 2888) > 50:
        log.warning(f"TIN count {tins_actual} differs from expected ~2,888")

    return result
