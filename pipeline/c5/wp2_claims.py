"""pipeline/c5/wp2_claims.py – WP2: Build c5_claims.

One evidence row per (NPI, source, address).
Sources: NPPES primary, secondary locations, endpoints, CMS DAC.
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
    parts = [row.get("addr1", ""), row.get("addr2", ""),
             row.get("city", ""), row.get("state", ""), row.get("zip", "")]
    return " ".join(str(p).strip() for p in parts if str(p).strip())


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


def run_wp2(cfg: dict, targets: pd.DataFrame) -> pd.DataFrame:
    """Build and write c5_claims. Returns the DataFrame."""
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

    if not parts:
        log.error("No claim sources available. Cannot proceed.")
        return pd.DataFrame(columns=SCHEMA_CLAIMS)

    claims = pd.concat(parts, ignore_index=True)

    # Raw address + derived columns
    claims["addr1"] = claims.get("addr1", "").fillna("")
    claims["addr2"] = claims.get("addr2", "").fillna("")
    claims["city"]  = claims.get("city", "").fillna("")
    claims["state"] = claims.get("state", "").fillna("")
    claims["zip"]   = claims.get("zip", "").fillna("")

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

    # Claim ID (deterministic)
    claims["claim_id"] = claims.apply(
        lambda r: _make_claim_id(r["npi"], r["source"], r["raw_address"]), axis=1
    )

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

    log.info("WP2 DONE")
    return result
