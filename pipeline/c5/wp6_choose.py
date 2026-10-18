"""pipeline/c5/wp6_choose.py – WP6: Stale checks, candidate selection, labelling.

Rules:
1. Stale check:
   - A self-reported claim is stale if a fresher claim from a different family points to a different site.
   - A candidate is stale if it is > stale_distance_km from all validated TIN sites (when TIN has sites).
   - Writes c5_stale_claims (claim_id, npi, site_id, reason, contradicting_claim_id, distance_km).
2. Choose highest-scoring remaining candidate per target.
3. Label (strict profile):
   - verified: >= verified_min_independent_families independent families agree AND newest source <= fresh_months.
   - probable: supported but short of verified (e.g. 1 fresh source + TIN fit, or >=2 families but not fresh).
   - unverified: single weak source, stale-only, or no valid candidate.
4. label_loose (loose profile):
   - verified_loose: >= 1 fresh family AND s_tin_fit >= loose_tin_fit_threshold.
   - probable_loose: >= 1 fresh family OR s_tin_fit >= loose_tin_fit_threshold.
   - unverified_loose: otherwise.
5. Output:
   - Parquet table at cfg['paths']['c5_stale']
   - CSV file at cfg['paths']['c5_addresses'] (includes label and label_loose)
   - c5_source_disagreements.csv (top 50 state-source address conflicts by tin_size)
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from .common import SCHEMA_ADDRESSES, SCHEMA_STALE, get_logger

log = get_logger("c5.wp6")

# Add label_loose to output schema
SCHEMA_ADDRESSES_V2 = SCHEMA_ADDRESSES + ["label_loose"]


def _compute_label(
    is_stale_cand: bool,
    n_families: int,
    is_fresh: bool,
    s_tin_fit: float = 0.0,
    verified_min_families: int = 2,
) -> str:
    """Compute strict-profile label."""
    if is_stale_cand:
        return "unverified"
    if n_families >= verified_min_families and is_fresh:
        return "verified"
    if (n_families >= 1 and is_fresh and s_tin_fit > 0) or (n_families >= verified_min_families):
        return "probable"
    return "unverified"


def _compute_loose_label(
    is_stale_cand: bool,
    n_families: int,
    is_fresh: bool,
    s_tin_fit: float,
    loose_tin_fit_threshold: float,
) -> str:
    """Compute loose-profile label."""
    if is_stale_cand:
        return "unverified"
    if n_families >= 1 and is_fresh and s_tin_fit >= loose_tin_fit_threshold:
        return "verified"
    if (n_families >= 1 and is_fresh) or (s_tin_fit >= loose_tin_fit_threshold):
        return "probable"
    return "unverified"


def run_wp6(
    cfg: dict,
    targets: pd.DataFrame,
    claims: pd.DataFrame,
    candidates: pd.DataFrame,
    state_stats: dict | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Run stale checks, select winning candidate per target, label and output c5_addresses."""
    log.info("=== WP6: Stale & conflict checks, choose, label ===")

    stale_dist_km = float(cfg.get("stale_distance_km", 100))
    fresh_months = float(cfg.get("fresh_months", 24))
    verified_min_families = int(cfg.get("verified_min_independent_families", 2))
    loose_tin_fit_threshold = float(cfg.get("loose_tin_fit_threshold", 0.25))
    run_date_str = cfg.get("run_date")
    run_dt = datetime.fromisoformat(run_date_str) if run_date_str else datetime.utcnow()

    stale_records: list[dict[str, Any]] = []
    stale_candidate_keys: set[tuple[str, str, str]] = set()  # (tin, npi, site_id)

    # 1. Contradiction stale check: NPI claims from different families with different sites
    if not claims.empty and "npi_status" in claims.columns:
        active_claims = claims[
            (claims["npi_status"] == "active") &
            (~claims["is_po_box"]) &
            (claims["site_id"].notna()) &
            (claims["site_id"] != "PO_BOX") &
            (claims["site_id"] != "")
        ].copy()
    else:
        active_claims = pd.DataFrame()

    if not active_claims.empty and "npi" in active_claims.columns:
        for npi, grp in active_claims.groupby("npi"):
            if len(grp) <= 1:
                continue
            for i, c1 in grp.iterrows():
                d1_str = str(c1.get("source_date") or "")[:10]
                fam1 = c1.get("source_family")
                site1 = c1.get("site_id")
                if not d1_str or not site1:
                    continue

                for j, c2 in grp.iterrows():
                    if i == j:
                        continue
                    d2_str = str(c2.get("source_date") or "")[:10]
                    fam2 = c2.get("source_family")
                    site2 = c2.get("site_id")
                    if not d2_str or not site2 or fam1 == fam2 or site1 == site2:
                        continue

                    try:
                        dt1 = datetime.strptime(d1_str, "%Y-%m-%d")
                        dt2 = datetime.strptime(d2_str, "%Y-%m-%d")
                        if dt2 > dt1 and fam1 == "nppes_self":
                            stale_records.append({
                                "claim_id": c1["claim_id"],
                                "npi": npi,
                                "site_id": site1,
                                "reason": "fresher_contradiction",
                                "contradicting_claim_id": c2["claim_id"],
                                "distance_km": None,
                            })
                    except Exception:
                        pass

    # 2. Distance-based staleness from candidate table
    if not candidates.empty and "distance_to_tin_site_km" in candidates.columns:
        dist_stale_mask = (
            candidates["distance_to_tin_site_km"].notna() &
            (candidates["distance_to_tin_site_km"] > stale_dist_km)
        )
        for _, cand_row in candidates[dist_stale_mask].iterrows():
            stale_candidate_keys.add((str(cand_row["tin"]), str(cand_row["npi"]), str(cand_row["site_id"])))
            stale_records.append({
                "claim_id": str(cand_row.get("claim_ids", "")).split(";")[0] if cand_row.get("claim_ids") else "N/A",
                "npi": str(cand_row["npi"]),
                "site_id": str(cand_row["site_id"]),
                "reason": "distance_from_tin_sites",
                "contradicting_claim_id": None,
                "distance_km": round(float(cand_row["distance_to_tin_site_km"]), 2),
            })

    # Save c5_stale_claims
    df_stale = pd.DataFrame(stale_records) if stale_records else pd.DataFrame(columns=SCHEMA_STALE)
    stale_out_path = cfg["paths"]["c5_stale"]
    Path(stale_out_path).parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pandas(df_stale), stale_out_path, compression="snappy")
    log.info(f"c5_stale_claims written: {len(df_stale):,} rows → {stale_out_path}")

    # Mark candidates that are stale
    cands = candidates.copy()
    if not cands.empty:
        cands["is_stale"] = cands.apply(
            lambda r: (str(r["tin"]), str(r["npi"]), str(r["site_id"])) in stale_candidate_keys,
            axis=1,
        )
    else:
        cands["is_stale"] = False

    # 3. Choose winner per target
    cands_by_target = {}
    if not cands.empty:
        for (tin, npi), grp in cands.groupby(["tin", "npi"]):
            cands_by_target[(str(tin), str(npi))] = grp

    # TIN size map for disagrement ranking
    tin_sizes = targets.groupby("tin")["npi"].nunique().to_dict()

    addresses_rows: list[dict[str, Any]] = []
    disagreement_rows: list[dict[str, Any]] = []

    # Index of current chosen addresses for disagreement detection (site_id by npi)
    # We build this after the loop by comparing state claims to winners
    state_site_by_npi: dict[str, str] = {}
    winner_site_by_npi: dict[str, str] = {}

    for _, tgt in targets.iterrows():
        tin = str(tgt["tin"])
        npi = str(tgt["npi"])
        ppo_scope = str(tgt.get("ppo_scope", "inclusive_only"))
        entity_type = str(tgt.get("entity_type", ""))

        tgt_cands = cands_by_target.get((tin, npi))
        winner = None

        if tgt_cands is not None and not tgt_cands.empty:
            non_stale = tgt_cands[~tgt_cands["is_stale"]].sort_values(by="score", ascending=False)
            if not non_stale.empty:
                winner = non_stale.iloc[0]
            else:
                winner = tgt_cands.sort_values(by="score", ascending=False).iloc[0]

        if winner is not None:
            n_families = int(winner.get("n_independent_families", 0))
            freshest_date = str(winner.get("freshest_source_date") or "")
            is_stale_cand = bool(winner.get("is_stale", False))
            s_tin_fit = float(winner.get("s_tin_fit", 0.0))
            score = float(winner.get("score", 0.0))

            # Determine freshness
            is_fresh = False
            if freshest_date:
                try:
                    dt = datetime.strptime(freshest_date[:10], "%Y-%m-%d")
                    age_months = (run_dt - dt).days / 30.4375
                    if age_months <= fresh_months:
                        is_fresh = True
                except Exception:
                    pass

            # Strict label
            label = _compute_label(
                is_stale_cand, n_families, is_fresh, s_tin_fit, verified_min_families
            )

            # Loose label
            label_loose = _compute_loose_label(
                is_stale_cand, n_families, is_fresh, s_tin_fit, loose_tin_fit_threshold
            )

            addr_val = str(winner.get("address_line", "") or "")
            county_val = str(winner.get("county", "") or "")
            sources_val = str(winner.get("families", "") or "")
            lat_val = float(winner["lat"]) if pd.notna(winner.get("lat")) else None
            lon_val = float(winner["lon"]) if pd.notna(winner.get("lon")) else None
            site_id_val = str(winner.get("site_id", "") or "")

            winner_site_by_npi[npi] = site_id_val

            addresses_rows.append({
                "tin": tin,
                "npi": npi,
                "address": addr_val,
                "address_line": addr_val,
                "unit": str(winner.get("unit", "") or ""),
                "city": str(winner.get("city", "") or ""),
                "state": str(winner.get("state", "") or ""),
                "zip5": str(winner.get("zip5", "") or ""),
                "lat": lat_val,
                "lon": lon_val,
                "county": county_val,
                "county_fips": county_val,
                "site_id": site_id_val,
                "label": label,
                "label_loose": label_loose,
                "score": score,
                "n_independent_families": n_families,
                "newest_source_date": freshest_date,
                "freshest_source_date": freshest_date,
                "source_list": sources_val,
                "evidence": sources_val,
                "claim_ids": str(winner.get("claim_ids", "") or ""),
                "ppo_scope": ppo_scope,
                "entity_type": entity_type,
            })
        else:
            addresses_rows.append({
                "tin": tin,
                "npi": npi,
                "address": "",
                "address_line": "",
                "unit": "",
                "city": "",
                "state": "",
                "zip5": "",
                "lat": None,
                "lon": None,
                "county": "",
                "county_fips": "",
                "site_id": "",
                "label": "unverified",
                "label_loose": "unverified",
                "score": 0.0,
                "n_independent_families": 0,
                "newest_source_date": "",
                "freshest_source_date": "",
                "source_list": "",
                "evidence": "",
                "claim_ids": "",
                "ppo_scope": ppo_scope,
                "entity_type": entity_type,
            })

    df_addresses = pd.DataFrame(addresses_rows)

    # ── State-source disagreement detection ───────────────────────────────────
    # For each NPI that has a state claim (DCA or CDPH) AND a winner with a
    # different site_id, record a disagreement row. Compare by site_id, then
    # zip5 if site_id is absent.
    if not claims.empty and "source_family" in claims.columns:
        state_claims = claims[
            claims["source_family"].isin(["state_license", "state_facility"])
        ].copy()
        if not state_claims.empty and "site_id" in state_claims.columns:
            for npi_val, grp in state_claims.groupby("npi"):
                npi_str = str(npi_val)
                winner_site = winner_site_by_npi.get(npi_str, "")
                for _, sc in grp.iterrows():
                    state_site = str(sc.get("site_id") or "")
                    if state_site and winner_site and state_site != winner_site:
                        state_zip = str(sc.get("zip") or "")[:5]
                        winner_rows = df_addresses[df_addresses["npi"] == npi_str]
                        winner_zip = winner_rows["zip5"].iloc[0] if not winner_rows.empty else ""
                        zip_match = (state_zip == winner_zip) if state_zip and winner_zip else None

                        # Determine comparison category
                        if zip_match is True:
                            comparison = "same_zip_different_site"
                        elif zip_match is False:
                            comparison = "different_zip"
                        else:
                            comparison = "different_site_zip_unknown"

                        tins_for_npi = targets[targets["npi"] == npi_str]["tin"].unique()
                        tin_size = max(
                            [tin_sizes.get(t, 1) for t in tins_for_npi], default=1
                        )
                        disagreement_rows.append({
                            "npi": npi_str,
                            "source_family": sc.get("source_family"),
                            "source": sc.get("source"),
                            "state_address": sc.get("raw_address"),
                            "state_site_id": state_site,
                            "winner_site_id": winner_site,
                            "winner_address": winner_rows["address"].iloc[0] if not winner_rows.empty else "",
                            "comparison": comparison,
                            "tin_size": tin_size,
                        })

    if disagreement_rows:
        df_dis = pd.DataFrame(disagreement_rows).sort_values("tin_size", ascending=False)
        log.info(
            f"  State-source disagreements: {len(df_dis):,} total | "
            f"same_zip_diff_site: {(df_dis['comparison']=='same_zip_different_site').sum()} | "
            f"diff_zip: {(df_dis['comparison']=='different_zip').sum()}"
        )
        top50 = df_dis.head(50)
        dis_path = str(Path(cfg["paths"]["c5_addresses"]).parent / "c5_source_disagreements.csv")
        top50.to_csv(dis_path, index=False)
        log.info(f"  Top-50 disagreements written → {dis_path}")

    # Verification checks
    assert len(df_addresses) == len(targets), (
        f"Row count mismatch: {len(df_addresses)} addresses vs {len(targets)} targets"
    )
    dup_check = df_addresses.duplicated(subset=["tin", "npi"]).sum()
    assert dup_check == 0, f"Duplicate (tin, npi) found in addresses output: {dup_check}"

    out_csv = cfg["paths"]["c5_addresses"]
    Path(out_csv).parent.mkdir(parents=True, exist_ok=True)

    # Write with label_loose if present; fall back gracefully if SCHEMA_ADDRESSES is old
    out_cols = [c for c in SCHEMA_ADDRESSES_V2 if c in df_addresses.columns]
    df_addresses[out_cols].to_csv(out_csv, index=False)
    log.info(f"c5_addresses written: {len(df_addresses):,} rows → {out_csv}")

    # Summary counts
    label_counts = df_addresses["label"].value_counts()
    label_loose_counts = df_addresses["label_loose"].value_counts() if "label_loose" in df_addresses.columns else pd.Series()
    log.info(f"  Label (strict):\n{label_counts.to_string()}")
    log.info(f"  Label (loose):\n{label_loose_counts.to_string()}")

    # Split by entity_type
    if "entity_type" in df_addresses.columns:
        for et in ["individual", "organization"]:
            sub = df_addresses[df_addresses["entity_type"] == et]
            if not sub.empty:
                log.info(f"  [{et}] strict: {sub['label'].value_counts().to_dict()} | loose: {sub['label_loose'].value_counts().to_dict()}")

    log.info("WP6 DONE")
    return df_stale, df_addresses
