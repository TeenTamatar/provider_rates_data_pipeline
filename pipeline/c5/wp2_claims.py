"""pipeline/c5/wp2_claims.py – WP2: Build c5_claims.

One evidence row per (NPI, source, address).
Sources: NPPES primary, secondary locations, endpoints, CMS DAC,
         California DCA licensee bulk files (individuals),
         California CDPH healthcare facility locations (organizations).
Deactivated NPIs are marked but not dropped so they are traceable.
"""
from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Iterator, Set

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from .common import (
    SCHEMA_CLAIMS,
    fmt_npi,
    fmt_tin,
    get_logger,
    is_po_box,
    source_to_family,
)
from .wp2_state_sources import (
    load_cdph_claims,
    load_dca_claims,
    write_source_instructions,
)
from .wp2_npi_registry import load_npi_registry_claims

log = get_logger("c5.wp2")

# ── NPPES main columns ────────────────────────────────────────────────────────
_NPPES_MAIN = {
    "NPI":  "npi_raw",
    "Provider First Line Business Practice Location Address": "addr1",
    "Provider Second Line Business Practice Location Address": "addr2",
    "Provider Business Practice Location Address City Name": "city",
    "Provider Business Practice Location Address State Name": "state",
    "Provider Business Practice Location Address Postal Code": "zip",
    "Entity Type Code": "entity_type",
    "Last Update Date": "source_date",
}

# ── NPPES secondary locations ─────────────────────────────────────────────────
_NPPES_PL = {
    "NPI": "npi_raw",
    "Provider Secondary Practice Location Address- Address Line 1": "addr1",
    "Provider Secondary Practice Location Address-  Address Line 2": "addr2",
    "Provider Secondary Practice Location Address - City Name": "city",
    "Provider Secondary Practice Location Address - State Name": "state",
    "Provider Secondary Practice Location Address - Postal Code": "zip",
}

# ── NPPES endpoints ───────────────────────────────────────────────────────────
_NPPES_EP = {
    "NPI": "npi_raw",
    "Affiliation Address Line One": "addr1",
    "Affiliation Address Line Two": "addr2",
    "Affiliation Address City": "city",
    "Affiliation Address State": "state",
    "Affiliation Address Postal Code": "zip",
}

# ── CMS DAC ───────────────────────────────────────────────────────────────────
_DAC = {
    "NPI":       "npi_raw",
    "adr_ln_1":  "addr1",
    "adr_ln_2":  "addr2",
    "City/Town": "city",
    "State":     "state",
    "ZIP Code":  "zip",
}


def _make_claim_id(npi: str, source: str, addr: str) -> str:
    """Deterministic claim_id: sha256 of (npi|source|raw_address)[:16]."""
    key = f"{npi}|{source}|{addr}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _raw_address(row: pd.Series) -> str:
    parts = []
    for col in ("addr1", "addr2", "city", "state", "zip"):
        val = row.get(col, "")
        if val is not None and not pd.isna(val):
            s = str(val).strip()
            if s and s.lower() != "nan":
                parts.append(s)
    return " ".join(parts)


def _col_select(col_map: dict) -> str:
    return ", ".join(f'"{orig}" AS {alias}' for orig, alias in col_map.items())


def _duck_query(sql: str) -> pd.DataFrame:
    con = duckdb.connect()
    df = con.execute(sql).df()
    con.close()
    return df


def _npi_filter(npis: Set[str]) -> str:
    if not npis:
        return "TRUE"
    quoted = ", ".join(f"'{n}'" for n in npis)
    return f"CAST(\"NPI\" AS VARCHAR) IN ({quoted})"


def load_nppes_main_claims(path: str, npis: Set[str]) -> pd.DataFrame:
    npi_filter = _npi_filter(npis)
    cols = _col_select(_NPPES_MAIN)
    sql = f"""
      SELECT {cols}
      FROM read_csv_auto('{path}', header=True, sample_size=5000)
      WHERE {npi_filter}
    """
    df = _duck_query(sql)
    df["npi"]      = df["npi_raw"].astype(str).str.strip().str.zfill(10)
    df["source"]   = "nppes_primary"
    df["source_family"] = "nppes_self"
    df["claim_type"] = "primary"
    df["tin"]      = None
    return df


def load_nppes_pl_claims(path: str, npis: Set[str]) -> pd.DataFrame:
    npi_filter = _npi_filter(npis)
    cols = _col_select(_NPPES_PL)
    sql = f"""
      SELECT {cols}
      FROM read_csv_auto('{path}', header=True, sample_size=5000)
      WHERE {npi_filter}
    """
    df = _duck_query(sql)
    df["npi"]           = df["npi_raw"].astype(str).str.strip().str.zfill(10)
    df["source"]        = "nppes_secondary"
    df["source_family"] = "nppes_self"
    df["claim_type"]    = "secondary"
    df["source_date"]   = None
    df["tin"]           = None
    df["entity_type"]   = None
    return df


def load_nppes_ep_claims(path: str, npis: Set[str]) -> pd.DataFrame:
    npi_filter = _npi_filter(npis)
    cols = _col_select(_NPPES_EP)
    sql = f"""
      SELECT {cols}
      FROM read_csv_auto('{path}', header=True, sample_size=5000)
      WHERE {npi_filter}
    """
    df = _duck_query(sql)
    df["npi"]           = df["npi_raw"].astype(str).str.strip().str.zfill(10)
    df["source"]        = "nppes_endpoint"
    df["source_family"] = "nppes_self"
    df["claim_type"]    = "endpoint"
    df["source_date"]   = None
    df["tin"]           = None
    df["entity_type"]   = None
    return df


def load_dac_claims(path: str, npis: Set[str]) -> pd.DataFrame:
    npi_filter = _npi_filter(npis)
    cols = _col_select(_DAC)
    sql = f"""
      SELECT {cols}
      FROM read_csv_auto('{path}', header=True, sample_size=5000)
      WHERE {npi_filter}
    """
    df = _duck_query(sql)
    df["npi"]           = df["npi_raw"].astype(str).str.strip().str.zfill(10)
    df["source"]        = "cms_dac"
    df["source_family"] = "medicare"
    df["claim_type"]    = "primary"
    df["source_date"]   = None
    df["tin"]           = None
    df["entity_type"]   = None
    return df


def run_wp2(
    cfg: dict,
    targets: pd.DataFrame,
) -> tuple[pd.DataFrame, dict]:
    """Build and write c5_claims. Returns (DataFrame, state_source_stats)."""
    log.info("=== WP2: Build c5_claims ===")

    npis: Set[str] = set(targets["npi"].unique())
    deact_npis: Set[str] = set(targets[targets["npi_deactivated"] == True]["npi"].unique())

    parts: list[pd.DataFrame] = []
    p = cfg["paths"]

    # NPPES primary
    if Path(p["nppes_main"]).is_file():
        log.info("  Loading NPPES primary claims via DuckDB…")
        df = load_nppes_main_claims(p["nppes_main"], npis)
        log.info(f"    → {len(df):,} rows")
        parts.append(df)

    # NPPES secondary locations
    if Path(p["nppes_pl"]).is_file():
        log.info("  Loading NPPES secondary location claims…")
        df = load_nppes_pl_claims(p["nppes_pl"], npis)
        log.info(f"    → {len(df):,} rows")
        parts.append(df)

    # NPPES endpoints
    if Path(p["nppes_ep"]).is_file():
        log.info("  Loading NPPES endpoint claims…")
        df = load_nppes_ep_claims(p["nppes_ep"], npis)
        log.info(f"    → {len(df):,} rows")
        parts.append(df)

    # CMS DAC
    if Path(p["cms_dac"]).is_file():
        log.info("  Loading CMS DAC claims via DuckDB…")
        df = load_dac_claims(p["cms_dac"], npis)
        log.info(f"    → {len(df):,} rows")
        parts.append(df)

    # ── Live CMS NPI Registry (freshen stale provider locations & endpoints) ──
    npi_reg_cfg = cfg.get("sources", {}).get("npi_registry_live", {})
    if npi_reg_cfg.get("enabled", True):
        log.info("  Loading live CMS NPI Registry claims…")
        try:
            df_reg = load_npi_registry_claims(
                cfg,
                npis,
                max_workers=int(npi_reg_cfg.get("max_workers", 10)),
                max_fetch_count=npi_reg_cfg.get("max_fetch_count", None),
            )
            if not df_reg.empty:
                log.info(f"    → {len(df_reg):,} live NPI registry claims created")
                parts.append(df_reg)
        except Exception as exc:
            log.error(f"  Live NPI Registry source failed (non-fatal): {exc}")

    # ── California DCA licensee bulk files (individuals) ──────────────────────
    state_stats: dict = {"dca": {}, "cdph": {}}
    dca_cfg = cfg.get("sources", {}).get("dca_license_bulk", {})
    dca_local_dir = dca_cfg.get("local_dir", "data/state_sources/dca")
    # Check if any DCA files exist
    import glob as _glob
    import os as _os
    dca_files_present = (
        bool(_glob.glob(_os.path.join(dca_local_dir, "*.tsv"))) or
        bool(_glob.glob(_os.path.join(dca_local_dir, "*.xls")))
    )
    if dca_files_present and Path(p["nppes_main"]).is_file():
        log.info("  Loading California DCA licensee claims (individuals)…")
        try:
            df_dca, dca_stats = load_dca_claims(cfg, targets, p["nppes_main"])
            state_stats["dca"] = dca_stats
            if not df_dca.empty:
                log.info(f"    → {len(df_dca):,} DCA claims created")
                parts.append(df_dca)
        except Exception as exc:
            log.error(f"  DCA source failed (non-fatal, run continues): {exc}")
    else:
        log.info("  DCA files not found – skipping (see c5_source_instructions.md)")

    # ── Build bulk claims concat so CDPH can use ZIP/city from existing claims ─
    bulk_parts = [part.dropna(how="all", axis=1) for part in parts if not part.empty]
    bulk_claims_for_cdph = pd.concat(bulk_parts, ignore_index=True) if bulk_parts else pd.DataFrame()

    # ── California CDPH healthcare facility locations (organizations) ─────────
    cdph_cfg = cfg.get("sources", {}).get("cdph_facility", {})
    cdph_local_dir = cdph_cfg.get("local_dir", "data/state_sources/cdph")
    cdph_file = _os.path.join(cdph_local_dir, "health_facility_locations.csv")
    if Path(cdph_file).is_file():
        log.info("  Loading California CDPH facility claims (organizations)…")
        try:
            df_cdph, cdph_stats = load_cdph_claims(cfg, targets, bulk_claims_for_cdph)
            state_stats["cdph"] = cdph_stats
            if not df_cdph.empty:
                log.info(f"    → {len(df_cdph):,} CDPH claims created")
                parts.append(df_cdph)
        except Exception as exc:
            log.error(f"  CDPH source failed (non-fatal, run continues): {exc}")
    else:
        log.info("  CDPH file not found – skipping (see c5_source_instructions.md)")

    # ── Write download instructions (always, so humans know what to place) ────
    instructions_path = str(Path(p["c5_claims"]).parent / "c5_source_instructions.md")
    write_source_instructions(cfg, instructions_path)

    parts_clean = [part.dropna(how="all", axis=1) for part in parts if not part.empty]
    if not parts_clean:
        log.error("No claim sources available. Cannot proceed.")
        return pd.DataFrame(columns=SCHEMA_CLAIMS), state_stats

    claims = pd.concat(parts_clean, ignore_index=True)

    # Raw address + derived columns
    for col in ("addr1", "addr2", "city", "state", "zip"):
        if col not in claims.columns:
            claims[col] = ""
        claims[col] = claims[col].fillna("")

    claims["raw_address"] = claims.apply(_raw_address, axis=1)

    # Exclude blank addresses (log reason)
    n_blank = (claims["raw_address"].str.strip() == "").sum()
    claims = claims[claims["raw_address"].str.strip() != ""].copy()
    log.info(f"  Excluded {n_blank:,} blank addresses")

    # PO box flag
    claims["is_po_box"]       = claims["raw_address"].apply(is_po_box)
    claims["is_billing_only"] = False   # will be filled by scoring rules

    # NPI status
    claims["npi_status"] = claims["npi"].apply(
        lambda n: "deactivated" if n in deact_npis else "active"
    )

    # Claim ID (deterministic; only set for non-state rows that don't already have one)
    def _assign_claim_id(r: pd.Series) -> str:
        if r.get("claim_id") and str(r["claim_id"]).strip():
            return r["claim_id"]
        return _make_claim_id(r["npi"], r["source"], r["raw_address"])

    claims["claim_id"] = claims.apply(_assign_claim_id, axis=1)

    def _norm_date(val: Any) -> Optional[str]:
        if val is None or pd.isna(val):
            return None
        if hasattr(val, "strftime"):
            return val.strftime("%Y-%m-%d")
        s = str(val).strip()
        if not s:
            return None
        try:
            return pd.to_datetime(s).strftime("%Y-%m-%d")
        except Exception:
            return s[:10]

    claims["source_date"] = claims["source_date"].apply(_norm_date)

    # ── Final column selection ────────────────────────────────────────────────
    for col in SCHEMA_CLAIMS:
        if col not in claims.columns:
            claims[col] = None

    result = claims[SCHEMA_CLAIMS].reset_index(drop=True)

    out_path = cfg["paths"]["c5_claims"]
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pandas(result), out_path, compression="snappy")
    log.info(f"c5_claims written: {len(result):,} rows → {out_path}")

    # ── Report ────────────────────────────────────────────────────────────────
    by_family = result.groupby("source_family").size().to_dict()
    log.info(f"  Claims by source_family: {by_family}")
    npis_in_claims = result["npi"].nunique()
    npis_missing   = len(npis) - npis_in_claims
    log.info(f"  NPIs with at least one claim: {npis_in_claims:,}  (no claim: {npis_missing:,})")
    pct_two_families = (
        result.groupby("npi")["source_family"].nunique().ge(2).mean() * 100
    )
    log.info(f"  NPIs with ≥ 2 families: {pct_two_families:.1f}%")
    po_box_count = result["is_po_box"].sum()
    log.info(f"  PO box claims flagged: {po_box_count:,}")
    deact_claims = (result["npi_status"] == "deactivated").sum()
    log.info(f"  Claims on deactivated NPIs: {deact_claims:,}")

    if state_stats.get("dca"):
        ds = state_stats["dca"]
        log.info(
            f"  DCA: eligible={ds.get('eligible_targets',0):,}, matched={ds.get('matched',0):,}, "
            f"ambiguous={ds.get('ambiguous',0):,}, claims={ds.get('claims_created',0):,}"
        )
    if state_stats.get("cdph"):
        cs = state_stats["cdph"]
        log.info(
            f"  CDPH: eligible={cs.get('eligible_targets',0):,}, matched={cs.get('matched',0):,}, "
            f"claims={cs.get('claims_created',0):,}"
        )

    log.info("WP2 DONE")
    return result, state_stats
