"""pipeline/c5/wp3_sites.py – WP3: Parse, geocode, build c5_sites.

Normalises addresses → geocodes unique addresses via Census batch API
(cached to Parquet) → assigns site_id → attaches site_id to claims.

Cache guarantee: a second run makes zero network calls.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
import time
from pathlib import Path
from typing import Optional

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import requests
import usaddress

from .common import SCHEMA_SITES, get_logger, is_po_box

log = get_logger("c5.wp3")

CENSUS_URL   = "https://geocoding.geo.census.gov/geocoder/geographies/addressbatch"
CENSUS_BENCH = "Public_AR_Current"

# ── Address normalisation ─────────────────────────────────────────────────────
_ABBREV = {
    r"\bROAD\b":    "RD",
    r"\bSTREET\b":  "ST",
    r"\bAVENUE\b":  "AVE",
    r"\bDRIVE\b":   "DR",
    r"\bCOURT\b":   "CT",
    r"\bCIRCLE\b":  "CIR",
    r"\bBOULEVARD\b": "BLVD",
    r"\bLANE\b":    "LN",
    r"\bWAY\b":     "WAY",
    r"\bTERRACE\b": "TER",
    r"\bPLACE\b":   "PL",
    r"\bHIGHWAY\b": "HWY",
    r"\bPARKWAY\b": "PKWY",
}
_ABBREV_COMPILED = [(re.compile(k, re.I), v) for k, v in _ABBREV.items()]


def normalize_address(raw: str) -> str:
    """Uppercase + abbreviate street types. Return normalised string."""
    s = raw.upper().strip()
    s = re.sub(r"\s+", " ", s)
    for pattern, repl in _ABBREV_COMPILED:
        s = pattern.sub(repl, s)
    return s


_STREET_TAGS = {
    "AddressNumber", "AddressNumberPrefix", "AddressNumberSuffix",
    "StreetName", "StreetNamePreDirectional", "StreetNamePostDirectional",
    "StreetNamePreModifier", "StreetNamePreType", "StreetNamePostType",
}
_UNIT_TAGS = {"OccupancyType", "OccupancyIdentifier",
              "SubaddressType", "SubaddressIdentifier"}


def parse_components(raw: str) -> dict:
    """Use usaddress to split raw address into components."""
    try:
        tagged, _ = usaddress.tag(raw)
    except usaddress.RepeatedLabelError:
        tagged = {}

    street  = " ".join(v for k, v in tagged.items() if k in _STREET_TAGS).strip()
    unit    = " ".join(v for k, v in tagged.items() if k in _UNIT_TAGS).strip()
    city    = tagged.get("PlaceName", "").strip()
    state   = tagged.get("StateName", "").strip()
    raw_zip = re.sub(r"\D", "", tagged.get("ZipCode", ""))
    zip5 = raw_zip[:5].zfill(5) if raw_zip else ""
    return {"address_line": street, "unit": unit, "city": city, "state": state, "zip5": zip5}


def _site_id(norm: str) -> str:
    """Stable site_id = 'SITE_' + sha256[:12] of normalised address."""
    return "SITE_" + hashlib.sha256(norm.encode()).hexdigest()[:12].upper()


# ── Census Geocoder batch ─────────────────────────────────────────────────────

def _geocode_batch(batch: pd.DataFrame) -> list[dict]:
    """Send a batch (up to 9,000 rows) to Census geocoder. Returns list of result dicts."""
    buf = io.StringIO()
    for idx, row in batch.iterrows():
        parts = parse_components(row["raw_address"])
        # Census format: id, address, city, state, zip
        buf.write(f'{idx},"{parts["address_line"]}","{parts["city"]}","{parts["state"]}","{parts["zip5"]}"\n')
    data = buf.getvalue().encode()

    try:
        resp = requests.post(
            CENSUS_URL,
            files={"addressFile": ("addrs.csv", data, "text/csv"),
                   "benchmark":   (None, CENSUS_BENCH),
                   "vintage":     (None, "Current_Current")},
            timeout=120,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning(f"  Census Geocoder request failed: {exc}")
        return []

    # Response (CSV): id, input, match, match type, matched address, "lon,lat", tiger id, side,
    # state fips, county fips, tract, block.
    results = []
    for fields in csv.reader(io.StringIO(resp.text)):
        if len(fields) < 3:
            continue
        matched = fields[2].strip() == "Match"
        lon = lat = None
        if matched and len(fields) > 5 and "," in fields[5]:
            try:
                lon, lat = (float(x) for x in fields[5].split(","))
            except ValueError:
                matched = False
        county_fips = (fields[8].strip() + fields[9].strip()) if matched and len(fields) > 9 else ""
        results.append({"_uid": fields[0].strip(), "matched": matched,
                        "match_address": fields[4].strip() if matched and len(fields) > 4 else "",
                        "lat": lat, "lon": lon, "county_fips": county_fips})
    return results


def geocode_unique_addresses(
    unique_addrs: pd.DataFrame,
    cache_path: str,
    batch_size: int,
    dry_run: bool,
) -> pd.DataFrame:
    """
    Geocode unique (raw_address, address_norm) rows. Cache to Parquet.
    In dry_run mode: skip network calls; return stubs with matched=False.
    Second run reads cache and makes zero calls for already-known addresses.
    """
    # Load cache
    cached = pd.DataFrame()
    if Path(cache_path).is_file():
        cached = pq.read_table(cache_path).to_pandas()
        log.info(f"  geocode cache loaded: {len(cached):,} entries")

    known = set(cached["address_norm"].tolist()) if not cached.empty and "address_norm" in cached.columns else set()
    to_geocode = unique_addrs[~unique_addrs["address_norm"].isin(known)].copy().reset_index(drop=True)
    log.info(f"  addresses to geocode: {len(to_geocode):,} (cached: {len(unique_addrs) - len(to_geocode):,})")

    new_rows: list[dict] = []

    if dry_run:
        log.info("  dry_run: skipping Census Geocoder calls; generating stubs")
        for _, row in to_geocode.iterrows():
            parts = parse_components(row["raw_address"])
            new_rows.append({
                "address_norm":  row["address_norm"],
                "raw_address":   row["raw_address"],
                "matched":       False,
                "match_address": "",
                "lat":   None, "lon": None, "county_fips": "",
                "address_line": parts["address_line"],
                "unit":         parts["unit"],
                "city":         parts["city"],
                "state":        parts["state"],
                "zip5":         parts["zip5"],
            })
    elif len(to_geocode) > 0:
        batches = [to_geocode.iloc[i: i + batch_size]
                   for i in range(0, len(to_geocode), batch_size)]
        log.info(f"  sending {len(batches)} batch(es) to Census Geocoder…")
        for b_idx, batch in enumerate(batches):
            log.info(f"    batch {b_idx+1}/{len(batches)} ({len(batch)} rows)")
            geo_results = _geocode_batch(batch)
            uid_map = {str(r["_uid"]): r for r in geo_results}
            for idx, row in batch.iterrows():
                geo = uid_map.get(str(idx), {})
                parts = parse_components(row["raw_address"])
                new_rows.append({
                    "address_norm":  row["address_norm"],
                    "raw_address":   row["raw_address"],
                    "matched":       geo.get("matched", False),
                    "match_address": geo.get("match_address", ""),
                    "lat":           geo.get("lat"),
                    "lon":           geo.get("lon"),
                    "county_fips":   geo.get("county_fips", ""),
                    "address_line":  parts["address_line"],
                    "unit":          parts["unit"],
                    "city":          parts["city"],
                    "state":         parts["state"],
                    "zip5":          parts["zip5"],
                })
            time.sleep(0.5)  # be polite

    new_df = pd.DataFrame(new_rows) if new_rows else pd.DataFrame()
    combined = pd.concat([cached, new_df], ignore_index=True) if not new_df.empty else cached

    if combined.empty:
        combined = unique_addrs[["address_norm", "raw_address"]].copy()
        combined["matched"] = False
        for col in ["match_address", "lat", "lon", "county_fips", "address_line",
                    "unit", "city", "state", "zip5"]:
            combined[col] = None

    # Save cache
    Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pandas(combined.drop_duplicates("address_norm")),
                   cache_path, compression="snappy")
    log.info(f"  geocode cache updated: {len(combined):,} entries → {cache_path}")

    match_rate = combined["matched"].mean() if len(combined) else 0.0
    log.info(f"  geocode match rate: {match_rate:.1%}")

    # Sample failures
    failures = combined[~combined["matched"]].head(20)
    log.info(f"  sample failures (up to 20):\n{failures[['address_norm']].to_string()}")

    return combined


def run_wp3(cfg: dict, claims: pd.DataFrame, dry_run: bool = False) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Build c5_sites and attach site_id to claims.
    Returns (claims_with_site_id, sites).
    """
    log.info("=== WP3: Parse, geocode, build c5_sites ===")

    # Exclude PO boxes from geocoding (they have no geocodable address)
    valid_claims = claims[~claims["is_po_box"]].copy()
    po_box_count = len(claims) - len(valid_claims)
    log.info(f"  PO boxes excluded from geocoding: {po_box_count:,}")

    # Unique addresses
    valid_claims["address_norm"] = valid_claims["raw_address"].apply(normalize_address)
    unique_addrs = (
        valid_claims[["raw_address", "address_norm"]]
        .drop_duplicates("address_norm")
        .reset_index(drop=True)
    )
    log.info(f"  Unique normalised addresses: {len(unique_addrs):,}")

    # Geocode
    geocoded = geocode_unique_addresses(
        unique_addrs=unique_addrs,
        cache_path=cfg["paths"]["geocode_cache"],
        batch_size=cfg["geocode_batch_size"],
        dry_run=dry_run,
    )

    # Assign site_id
    geocoded["site_id"] = geocoded["address_norm"].apply(_site_id)

    # Build c5_sites (one row per unique building / site)
    sites = geocoded.rename(columns={"match_address": "normalized_address",
                                     "county_fips": "county"})[
        ["site_id", "normalized_address", "address_line", "unit", "city",
         "state", "zip5", "lat", "lon", "county", "matched"]
    ].drop_duplicates("site_id").copy()
    sites = sites.rename(columns={"matched": "geocode_matched"})
    sites["geocode_quality"] = sites["geocode_matched"].map({True: "match", False: "no_match"})

    # Attach site_id to valid claims
    addr_to_site = geocoded.set_index("address_norm")["site_id"].to_dict()
    valid_claims["site_id"] = valid_claims["address_norm"].map(addr_to_site)

    # Mark PO-box claims with site_id = PO_BOX (excluded from scoring)
    po_claims = claims[claims["is_po_box"]].copy()
    po_claims["site_id"] = "PO_BOX"
    po_claims["address_norm"] = ""

    # Reassemble
    claims_out = pd.concat([valid_claims, po_claims], ignore_index=True)

    # Write sites
    out_sites = cfg["paths"]["c5_sites"]
    Path(out_sites).parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pandas(sites), out_sites, compression="snappy")
    log.info(f"c5_sites written: {len(sites):,} rows → {out_sites}")

    # Write updated claims (with site_id)
    out_claims = cfg["paths"]["c5_claims"]
    pq.write_table(pa.Table.from_pandas(claims_out), out_claims, compression="snappy")
    log.info(f"c5_claims (with site_id) updated: {len(claims_out):,} rows")

    by_family = sites.groupby("geocode_quality").size().to_dict()
    log.info(f"WP3 DONE: {len(sites):,} sites | match distribution: {by_family}")
    return claims_out, sites
