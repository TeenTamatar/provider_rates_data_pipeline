"""pipeline/c5/wp4_tin_sites.py – WP4: Build TIN site sets.

A site belongs to a TIN's set if:
  (a) it is supported by an org NPI under the TIN (org_npi_supported),
  (b) it is listed by at least site_consensus_min_npis individual NPIs
      under the TIN (consensus), or
  (c) it is linked to a Medicare group that overlaps ≥ tin_group_overlap_min
      of the TIN's NPIs (medicare_group_overlap).

Output: config.paths.c5_tin_sites (Parquet)
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from .common import SCHEMA_TIN_SITES, get_logger

log = get_logger("c5.wp4")


def run_wp4(cfg: dict, targets: pd.DataFrame, claims: pd.DataFrame) -> pd.DataFrame:
    """Build and write c5_tin_sites. Returns the DataFrame."""
    log.info("=== WP4: Build TIN site sets ===")

    consensus_min = cfg["site_consensus_min_npis"]
    group_overlap = cfg["tin_group_overlap_min"]

    # Active, non-PO-box claims with a real site_id
    active_claims = claims[
        (claims["npi_status"] == "active") &
        (~claims["is_po_box"]) &
        (claims["site_id"].notna()) &
        (claims["site_id"] != "PO_BOX") &
        (claims["site_id"] != "")
    ].copy()

    # Join targets to know entity_type and TIN
    tgt_slim = targets[["tin", "npi", "entity_type"]].drop_duplicates()
    if "tin" in active_claims.columns:
        active_claims = active_claims.drop(columns=["tin"])
    enriched = active_claims.merge(tgt_slim, on="npi", how="inner")

    rows: list[dict] = []

    tin_groups = targets.groupby("tin")
    n_tins = len(tin_groups)
    log.info(f"  Processing {n_tins:,} TINs…")

    for tin, grp in tin_groups:
        tin_npis   = set(grp["npi"].unique())
        n_tin_npis = len(tin_npis)

        # Claims for any NPI in this TIN
        tin_claims = enriched[enriched["tin"] == tin]
        if tin_claims.empty:
            rows.append({"tin": tin, "site_id": None, "evidence_types": "none",
                         "n_supporting_npis": 0})
            continue

        # (a) Org NPI: entity_type == 'organization'
        org_sites = set(
            tin_claims[tin_claims["entity_type"] == "organization"]["site_id"].dropna().unique()
        )

        # (b) Individual consensus: ≥ site_consensus_min_npis individuals at site
        ind_claims = tin_claims[tin_claims["entity_type"] == "individual"]
        site_npi_counts = ind_claims.groupby("site_id")["npi"].nunique()
        consensus_sites = set(site_npi_counts[site_npi_counts >= consensus_min].index)

        # (c) Medicare group overlap – uses DAC org_pac_id if available
        # DAC doesn't carry a TIN; we approximate by NPI overlap with same org_pac_id
        medicare_sites: set = set()
        if "org_pac_id" in tin_claims.columns:
            dac_claims = tin_claims[(tin_claims["source"] == "cms_dac") &
                                    (tin_claims["org_pac_id"].notna())]
            for pac_id, pac_grp in dac_claims.groupby("org_pac_id"):
                pac_npis = set(pac_grp["npi"].unique())
                overlap  = len(pac_npis & tin_npis) / n_tin_npis
                if overlap >= group_overlap:
                    medicare_sites.update(pac_grp["site_id"].dropna().unique())

        # Combine
        all_sites = org_sites | consensus_sites | medicare_sites

        for site_id in (all_sites if all_sites else {None}):
            evidence_parts = []
            if site_id in org_sites:        evidence_parts.append("org_npi_supported")
            if site_id in consensus_sites:  evidence_parts.append("consensus")
            if site_id in medicare_sites:   evidence_parts.append("medicare_group_overlap")
            n_support = int(
                ind_claims[ind_claims["site_id"] == site_id]["npi"].nunique()
                if site_id else 0
            )
            rows.append({
                "tin":               tin,
                "site_id":          site_id,
                "evidence_types":   ";".join(evidence_parts) if evidence_parts else "none",
                "n_supporting_npis": n_support,
            })

    result = pd.DataFrame(rows)

    out_path = cfg["paths"]["c5_tin_sites"]
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pandas(result), out_path, compression="snappy")
    log.info(f"c5_tin_sites written: {len(result):,} rows → {out_path}")

    # ── Diagnostics ────────────────────────────────────────────────────────────
    tins_no_site = result[result["site_id"].isna()]["tin"].nunique()
    log.info(f"  TINs with no site set: {tins_no_site:,}")
    log.info(f"  Evidence type counts:\n{result.groupby('evidence_types').size().to_string()}")

    # Print 20 TINs for manual review (mix of large, small, solo)
    tin_sizes = targets.groupby("tin")["npi"].nunique().rename("tin_size")
    sample_tins = pd.concat([
        tin_sizes.sort_values(ascending=False).head(7),  # large
        tin_sizes.sort_values().head(7),                 # small / solo
        tin_sizes.sample(min(6, len(tin_sizes)), random_state=cfg.get("random_seed", 42)),
    ]).index.unique()[:20]

    log.info("\n  === 20 TINs for manual review ===")
    for tin in sample_tins:
        tin_result = result[result["tin"] == tin]
        sites_list = tin_result["site_id"].dropna().tolist()
        log.info(f"  TIN {tin} | size={int(tin_sizes.get(tin, 0))} | sites={len(sites_list)} | {sites_list[:3]}")

    log.info("WP4 DONE")
    return result
