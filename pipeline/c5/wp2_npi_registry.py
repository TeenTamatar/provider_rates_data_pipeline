"""pipeline/c5/wp2_npi_registry.py – Ingest live CMS NPI Registry API data (v2.1).

Fetches official CMS live NPI data for target providers to:
1. Freshen stale NPPES provider practice location addresses with their live `last_updated` date.
2. Ingest active provider endpoints.
3. Fully offline-cached in `data/npi_registry_cache/{npi}.json`.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Optional, Set

import pandas as pd
import requests

from .common import SCHEMA_CLAIMS, fmt_npi, get_logger, is_po_box

log = get_logger("c5.wp2_npi_registry")

API_URL = "https://npiregistry.cms.hhs.gov/api/?version=2.1&number={npi}&limit=1"


def _fetch_or_read_cache(npi: str, cache_dir: Path, timeout: float = 6.0) -> Optional[dict]:
    """Read NPI record from cache, or fetch from CMS API and cache to disk."""
    cache_file = cache_dir / f"{npi}.json"
    if cache_file.is_file():
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass

    # Fetch from live API
    url = API_URL.format(npi=npi)
    try:
        resp = requests.get(url, timeout=timeout)
        if resp.status_code == 200:
            data = resp.json()
            try:
                with open(cache_file, "w", encoding="utf-8") as f:
                    json.dump(data, f)
            except Exception:
                pass
            return data
    except Exception as exc:
        log.debug(f"NPI Registry fetch failed for {npi}: {exc}")

    return None


def load_npi_registry_claims(
    cfg: dict,
    npis: Set[str],
    max_workers: int = 10,
    max_fetch_count: Optional[int] = None,
) -> pd.DataFrame:
    """Fetch/load CMS NPI Registry claims for target NPIs.

    Returns a DataFrame conforming to SCHEMA_CLAIMS.
    """
    p = cfg.get("paths", {})
    cache_dir = Path(p.get("npi_registry_cache", "data/npi_registry_cache"))
    cache_dir.mkdir(parents=True, exist_ok=True)

    npi_list = sorted(list(npis))
    if max_fetch_count is not None and max_fetch_count > 0:
        npi_list = npi_list[:max_fetch_count]

    log.info(f"Loading live CMS NPI Registry claims for {len(npi_list):,} NPIs...")

    # Load from cache first
    needed_fetch = []
    cached_records: dict[str, dict] = {}
    for npi in npi_list:
        cache_file = cache_dir / f"{npi}.json"
        if cache_file.is_file():
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    cached_records[npi] = json.load(f)
            except Exception:
                needed_fetch.append(npi)
        else:
            needed_fetch.append(npi)

    log.info(f"  NPI Registry cache hit: {len(cached_records):,} | To fetch: {len(needed_fetch):,}")

    # Fetch missing NPIs concurrently
    if needed_fetch:
        log.info(f"  Fetching {len(needed_fetch):,} NPIs from CMS Registry API (workers={max_workers})...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_npi = {
                executor.submit(_fetch_or_read_cache, npi, cache_dir): npi
                for npi in needed_fetch
            }
            for future in concurrent.futures.as_completed(future_to_npi):
                npi = future_to_npi[future]
                try:
                    data = future.result()
                    if data:
                        cached_records[npi] = data
                except Exception as exc:
                    log.debug(f"Error reading result for {npi}: {exc}")

    claims_rows: list[dict[str, Any]] = []

    for npi, data in cached_records.items():
        if not data or data.get("result_count", 0) == 0:
            continue
        results = data.get("results", [])
        if not results:
            continue
        rec = results[0]

        basic = rec.get("basic", {})
        last_updated = str(basic.get("last_updated") or "").strip()[:10]
        entity_code = str(rec.get("enumeration_type", "")).strip()
        entity_type = "individual" if "1" in entity_code else "organization"

        # 1. Practice Location Addresses
        addresses = rec.get("addresses", [])
        for addr in addresses:
            purpose = str(addr.get("address_purpose", "")).upper()
            addr1 = str(addr.get("address_1", "") or "").strip()
            addr2 = str(addr.get("address_2", "") or "").strip()
            city = str(addr.get("city", "") or "").strip()
            state = str(addr.get("state", "") or "").strip()
            raw_zip = str(addr.get("postal_code", "") or "").strip()
            zip5 = raw_zip[:5] if raw_zip else ""

            parts = [p for p in (addr1, addr2, city, state, zip5) if p and p.lower() != "nan"]
            raw_addr = " ".join(parts)
            if not raw_addr:
                continue

            po_box = is_po_box(raw_addr)
            claim_type = "primary" if purpose == "LOCATION" else "mailing"
            claim_id = hashlib.sha256(f"{npi}|nppes_registry_live|{raw_addr}".encode()).hexdigest()[:16]

            claims_rows.append({
                "claim_id": claim_id,
                "npi": fmt_npi(npi),
                "tin": None,
                "raw_address": raw_addr,
                "source": "nppes_registry_live",
                "source_family": "nppes_self",
                "source_date": last_updated if last_updated else None,
                "claim_type": claim_type,
                "is_po_box": po_box,
                "is_billing_only": (purpose == "MAILING"),
                "npi_status": "active" if basic.get("status") == "A" else "deactivated",
                "addr1": addr1,
                "addr2": addr2,
                "city": city,
                "state": state,
                "zip": zip5,
                "entity_type": entity_type,
            })

        # 2. Endpoints
        endpoints = rec.get("endpoints", [])
        for ep in endpoints:
            ep_addr = str(ep.get("endpoint", "") or "").strip()
            if not ep_addr or "@" in ep_addr:
                continue  # skip email addresses
            # If endpoint contains a web URL / address
            ep_claim_id = hashlib.sha256(f"{npi}|nppes_endpoint_live|{ep_addr}".encode()).hexdigest()[:16]
            claims_rows.append({
                "claim_id": ep_claim_id,
                "npi": fmt_npi(npi),
                "tin": None,
                "raw_address": ep_addr,
                "source": "nppes_endpoint_live",
                "source_family": "nppes_self",
                "source_date": last_updated if last_updated else None,
                "claim_type": "endpoint",
                "is_po_box": False,
                "is_billing_only": False,
                "npi_status": "active",
                "addr1": ep_addr,
                "addr2": "",
                "city": "",
                "state": "",
                "zip": "",
                "entity_type": entity_type,
            })

    df = pd.DataFrame(claims_rows) if claims_rows else pd.DataFrame(columns=SCHEMA_CLAIMS)
    log.info(f"  Live NPI Registry generated {len(df):,} claims across {df['npi'].nunique() if not df.empty else 0:,} NPIs")
    return df
