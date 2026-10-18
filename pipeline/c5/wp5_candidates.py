"""pipeline/c5/wp5_candidates.py – WP5: Candidate generation and scoring.

Generates candidate sites per target (TIN, NPI):
- Always includes NPI's own sites from valid claims.
- Adds the TIN's site set only if:
  (a) The TIN is small (num_npis <= small_tin_max_npis), or
  (b) The NPI has no valid claim/site of its own.

Computes scoring features and transparent weighted contributions:
- s_families: min(n_independent_families / verified_min_independent_families, 1.0) * w_families
- s_tin_fit: (1.0 if site in TIN site set else 0.0) * w_tin_fit
- s_freshness: max(0.0, 1.0 - (age_in_months / fresh_months)) * w_freshness
- s_geocode: (1.0 if geocoded matched else 0.0) * w_geocode
- s_county: (1.0 if county matches target county_label else 0.0) * w_county
- s_in_state: (1.0 if state == target_state else 0.0) * w_in_state
Total score = sum of contributions.

Outputs:
- Parquet table at cfg['paths']['c5_candidates']
"""
from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from .common import SCHEMA_CANDIDATES, count_independent_families, get_logger

log = get_logger("c5.wp5")


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate the great circle distance in kilometers between two points."""
    if any(math.isnan(x) for x in (lat1, lon1, lat2, lon2)):
        return float("nan")
    R = 6371.0  # Earth radius in kilometers
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def run_wp5(
    cfg: dict,
    targets: pd.DataFrame,
    claims: pd.DataFrame,
    sites: pd.DataFrame,
    tin_sites: pd.DataFrame,
) -> pd.DataFrame:
    """Build and write c5_candidates. Returns the DataFrame."""
    log.info("=== WP5: Candidates and scoring ===")

    weights = cfg.get("score_weights", {})
    w_fam = float(weights.get("w_families", 0.40))
    w_tin = float(weights.get("w_tin_fit", 0.25))
    w_fresh = float(weights.get("w_freshness", 0.20))
    w_geo = float(weights.get("w_geocode", 0.05))
    w_cnty = float(weights.get("w_county", 0.05))
    w_state = float(weights.get("w_in_state", 0.05))
    w_solo_nppes = float(weights.get("w_solo_nppes", 0.15))

    fresh_months = float(cfg.get("fresh_months", 24))
    small_tin_max = int(cfg.get("small_tin_max_npis", 10))
    verified_families_min = float(cfg.get("verified_min_independent_families", 2))
    target_state = str(cfg.get("state", "CA")).upper()
    run_date_str = cfg.get("run_date")
    run_dt = datetime.fromisoformat(run_date_str) if run_date_str else datetime.utcnow()

    # Pre-index sites dictionary for fast attribute lookup
    sites_dict = sites.set_index("site_id").to_dict(orient="index")

    # Filter active, non-po-box claims with valid site_id
    valid_claims = claims[
        (claims["npi_status"] == "active") &
        (~claims["is_po_box"]) &
        (claims["site_id"].notna()) &
        (claims["site_id"] != "PO_BOX") &
        (claims["site_id"] != "")
    ].copy()

    # Group valid claims by NPI
    claims_by_npi = {}
    for npi, grp in valid_claims.groupby("npi"):
        claims_by_npi[npi] = grp

    # TIN sites mapping: tin -> set of site_ids
    valid_tin_sites = tin_sites[tin_sites["site_id"].notna()]
    tin_site_map: dict[str, set[str]] = {}
    for tin, grp in valid_tin_sites.groupby("tin"):
        tin_site_map[tin] = set(grp["site_id"].unique())

    # TIN coordinates for distance calculation
    tin_site_coords: dict[str, list[tuple[float, float]]] = {}
    for tin, site_set in tin_site_map.items():
        coords = []
        for sid in site_set:
            s_info = sites_dict.get(sid)
            if s_info and pd.notna(s_info.get("lat")) and pd.notna(s_info.get("lon")):
                coords.append((float(s_info["lat"]), float(s_info["lon"])))
        tin_site_coords[tin] = coords

    # TIN size (number of unique NPIs)
    tin_sizes = targets.groupby("tin")["npi"].nunique().to_dict()

    candidate_rows: list[dict[str, Any]] = []

    log.info(f"  Generating candidates for {len(targets):,} targets...")
    for _, tgt in targets.iterrows():
        tin = str(tgt["tin"])
        npi = str(tgt["npi"])
        ppo_scope = str(tgt.get("ppo_scope", "inclusive_only"))
        county_label = str(tgt.get("county_label", "")).lower()

        t_size = tin_sizes.get(tin, 1)
        tin_s_set = tin_site_map.get(tin, set())

        # 1. NPI's own sites
        npi_claims = claims_by_npi.get(npi)
        npi_sites = set(npi_claims["site_id"].unique()) if npi_claims is not None else set()

        # 2. Decide whether to add TIN sites
        candidate_site_ids = set(npi_sites)
        if len(npi_sites) == 0 or t_size <= small_tin_max:
            candidate_site_ids.update(tin_s_set)

        if not candidate_site_ids:
            continue

        # Score each candidate site for this target
        for sid in candidate_site_ids:
            site_info = sites_dict.get(sid, {})
            # Claims for this specific (npi, site_id)
            if npi_claims is not None:
                site_claims = npi_claims[npi_claims["site_id"] == sid]
            else:
                site_claims = pd.DataFrame()

            # Independent families
            if not site_claims.empty:
                families = sorted(site_claims["source_family"].dropna().unique().tolist())
                claim_ids = site_claims["claim_id"].dropna().tolist()
                # Freshness: find newest source date
                newest_date = None
                for d_val in site_claims["source_date"].dropna():
                    try:
                        d_str = str(d_val)[:10]
                        d_obj = datetime.strptime(d_str, "%Y-%m-%d")
                        if newest_date is None or d_obj > newest_date:
                            newest_date = d_obj
                    except Exception:
                        pass
            else:
                families = []
                claim_ids = []
                newest_date = None

            n_families = count_independent_families(families)
            # TIN fit
            is_tin_fit = 1.0 if sid in tin_s_set else 0.0

            # Freshness score
            if newest_date is not None:
                age_months = max(0.0, (run_dt - newest_date).days / 30.4375)
                freshest_date_str = newest_date.strftime("%Y-%m-%d")
                freshness_factor = max(0.0, 1.0 - (age_months / fresh_months))
            else:
                age_months = 999.0
                freshest_date_str = ""
                freshness_factor = 0.0

            # Geocode match
            is_geo_matched = 1.0 if site_info.get("geocode_matched") else 0.0

            # County match
            site_county = str(site_info.get("county", "")).lower()
            county_match = 1.0 if (county_label and county_label in site_county) else 0.0

            # In state
            site_state = str(site_info.get("state", "")).upper()
            is_in_state = 1.0 if site_state == target_state else 0.0

            # Solo practitioner primary NPPES baseline weight
            # Slightly increases primary NPPES weight for solo practitioners so easy cases aren't
            # displaced unless the alternative source has multiple independent corroborations (>= 2 families).
            is_solo = (t_size <= 2) or (t_size <= small_tin_max and str(tgt.get("entity_type", "")).lower() == "individual")
            has_primary_nppes = bool(
                not site_claims.empty and
                (
                    (site_claims["source"] == "nppes_primary").any() or
                    (site_claims["source"] == "nppes_registry_live").any() or
                    ((site_claims["source_family"] == "nppes_self") & (site_claims.get("claim_type", "") == "primary")).any()
                )
            )
            s_solo_nppes = w_solo_nppes if (is_solo and has_primary_nppes) else 0.0

            # Feature contributions
            s_families = min(n_families / max(verified_families_min, 1.0), 1.0) * w_fam
            s_tin_fit = is_tin_fit * w_tin
            s_freshness = freshness_factor * w_fresh
            s_geocode = is_geo_matched * w_geo
            s_county = county_match * w_cnty
            s_in_state = is_in_state * w_state

            total_score = s_families + s_tin_fit + s_freshness + s_geocode + s_county + s_in_state + s_solo_nppes

            # Distance to nearest TIN site
            min_dist_km = float("nan")
            s_lat = site_info.get("lat")
            s_lon = site_info.get("lon")
            if pd.notna(s_lat) and pd.notna(s_lon):
                s_lat_f = float(s_lat)
                s_lon_f = float(s_lon)
                coords_list = tin_site_coords.get(tin, [])
                if coords_list:
                    dists = [haversine_km(s_lat_f, s_lon_f, t_lat, t_lon) for t_lat, t_lon in coords_list]
                    min_dist_km = min(dists)

            candidate_rows.append({
                "tin": tin,
                "npi": npi,
                "site_id": sid,
                "address": site_info.get("address_line", ""),
                "address_line": site_info.get("address_line", ""),
                "unit": site_info.get("unit", ""),
                "city": site_info.get("city", ""),
                "state": site_info.get("state", ""),
                "zip5": site_info.get("zip5", ""),
                "lat": float(site_info["lat"]) if pd.notna(site_info.get("lat")) else None,
                "lon": float(site_info["lon"]) if pd.notna(site_info.get("lon")) else None,
                "county": site_info.get("county", ""),
                "county_fips": site_info.get("county", ""),
                "n_independent_families": n_families,
                "families": ";".join(families),
                "s_families": round(s_families, 4),
                "s_tin_fit": round(s_tin_fit, 4),
                "s_freshness": round(s_freshness, 4),
                "s_geocode": round(s_geocode, 4),
                "s_county": round(s_county, 4),
                "s_in_state": round(s_in_state, 4),
                "score": round(total_score, 4),
                "rank": 0,  # calculated below
                "freshest_source_date": freshest_date_str,
                "newest_source_date": freshest_date_str,
                "evidence": f"families={len(families)};tin_fit={int(is_tin_fit)};dist_km={min_dist_km:.1f}" if pd.notna(min_dist_km) else f"families={len(families)};tin_fit={int(is_tin_fit)}",
                "claim_ids": ";".join(claim_ids),
                "ppo_scope": ppo_scope,
                "distance_to_tin_site_km": min_dist_km,
            })

    if candidate_rows:
        df_candidates = pd.DataFrame(candidate_rows)
        # Assign ranks per (tin, npi) group by score descending
        df_candidates["rank"] = df_candidates.groupby(["tin", "npi"])["score"].rank(
            ascending=False, method="first"
        ).astype(int)
    else:
        df_candidates = pd.DataFrame(columns=SCHEMA_CANDIDATES + ["distance_to_tin_site_km"])

    out_path = cfg["paths"]["c5_candidates"]
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pandas(df_candidates), out_path, compression="snappy")
    log.info(f"c5_candidates written: {len(df_candidates):,} rows → {out_path}")
    log.info("WP5 DONE")
    return df_candidates
