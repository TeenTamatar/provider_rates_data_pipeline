"""pipeline/c5/wp9_summary.py – WP9: Comprehensive execution summary generation.

Produces c5_summary.json:
- Counts and shares by label (NPIs and TINs separately)
- Geocode match rate
- Claims by source family
- Stale claims by reason
- Deactivated NPIs excluded
- No-claim targets
- Leftover queue size
- Strict versus inclusive split
- Run date, config hash, and elapsed time
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from .common import config_hash, get_logger

log = get_logger("c5.wp9")


def run_wp9(
    cfg: dict,
    targets: pd.DataFrame,
    claims: pd.DataFrame,
    sites: pd.DataFrame,
    stale_claims: pd.DataFrame,
    addresses: pd.DataFrame,
    leftovers: pd.DataFrame,
    lookup_stats: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Compile and write c5_summary.json. Returns summary dict."""
    log.info("=== WP9: Write c5_summary.json ===")

    # Label counts by target (TIN-NPI pair)
    label_counts = addresses["label"].value_counts().to_dict()
    total_targets = len(addresses)
    label_shares = {k: round(v / total_targets, 4) for k, v in label_counts.items()} if total_targets else {}

    # Label counts by unique NPI (an NPI is verified if verified for any TIN, else probable, etc.)
    npi_label_priority = {"verified": 1, "probable": 2, "unverified": 3}
    npi_labels = (
        addresses.assign(priority=addresses["label"].map(npi_label_priority))
        .sort_values(by="priority")
        .groupby("npi")["label"]
        .first()
    )
    npi_counts = npi_labels.value_counts().to_dict()
    total_npis = len(npi_labels)
    npi_shares = {k: round(v / total_npis, 4) for k, v in npi_counts.items()} if total_npis else {}

    # Label counts by unique TIN (a TIN is verified if >= 50% of its pairs are verified, etc.)
    tin_verified_counts = addresses.groupby("tin")["label"].apply(
        lambda s: "verified" if (s == "verified").mean() >= 0.5
        else ("probable" if (s.isin(["verified", "probable"])).mean() >= 0.5 else "unverified")
    )
    tin_counts = tin_verified_counts.value_counts().to_dict()
    total_tins = len(tin_verified_counts)
    tin_shares = {k: round(v / total_tins, 4) for k, v in tin_counts.items()} if total_tins else {}

    # Geocode match rate
    if not sites.empty and "geocode_matched" in sites.columns:
        geo_matched = int(sites["geocode_matched"].sum())
        geo_total = len(sites)
        geo_rate = round(geo_matched / geo_total, 4) if geo_total else 0.0
    else:
        geo_matched, geo_total, geo_rate = 0, 0, 0.0

    # Claims by source family
    claims_by_family = claims["source_family"].value_counts().to_dict() if not claims.empty else {}

    # Stale claims by reason
    stale_by_reason = stale_claims["reason"].value_counts().to_dict() if not stale_claims.empty else {}

    # Deactivated NPIs excluded
    deact_npis = int(claims[claims["npi_status"] == "deactivated"]["npi"].nunique()) if not claims.empty else 0

    # No-claim targets
    active_npis_with_claims = set(claims[claims["npi_status"] == "active"]["npi"].unique()) if not claims.empty else set()
    no_claim_targets = int((~targets["npi"].isin(active_npis_with_claims)).sum())

    # Leftover queue size
    leftover_size = len(leftovers)

    # Strict vs inclusive split
    scope_split = targets["ppo_scope"].value_counts().to_dict() if "ppo_scope" in targets.columns else {}

    summary = {
        "run_date": cfg.get("run_date"),
        "config_hash": config_hash(cfg),
        "scope": {
            "state": cfg.get("state"),
            "target_rate_file": cfg.get("target_rate_file"),
            "total_targets": total_targets,
            "total_unique_npis": total_npis,
            "total_unique_tins": total_tins,
            "ppo_scope_distribution": scope_split,
        },
        "verification_results": {
            "by_target_pair": {
                "counts": label_counts,
                "shares": label_shares,
            },
            "by_unique_npi": {
                "counts": npi_counts,
                "shares": npi_shares,
            },
            "by_unique_tin": {
                "counts": tin_counts,
                "shares": tin_shares,
            },
        },
        "geocoding": {
            "total_sites": geo_total,
            "matched_sites": geo_matched,
            "match_rate": geo_rate,
        },
        "claims_and_evidence": {
            "total_claims": len(claims),
            "claims_by_family": claims_by_family,
            "deactivated_npis_excluded": deact_npis,
            "no_claim_targets": no_claim_targets,
        },
        "stale_claims": {
            "total_stale": len(stale_claims),
            "by_reason": stale_by_reason,
        },
        "leftover_queue": {
            "total_unverified": leftover_size,
        },
    }

    # Before / After comparison
    before_path = None
    for cand in [
        Path("outputs/c5_before_tiered/c5_summary.json"),
        Path("output/c5_before_tiered/c5_summary.json"),
        Path("output/c5_baseline/c5_summary.json"),
    ]:
        if cand.is_file():
            before_path = cand
            break

    if before_path is not None:
        try:
            with open(before_path) as bf:
                base_s = json.load(bf)
            before_counts = base_s.get("verification_results", {}).get("by_target_pair", {}).get("counts", {})
            summary["before_after_comparison"] = {
                "baseline_source": str(before_path),
                "before_counts": before_counts,
                "after_counts": label_counts,
                "labels_upgraded": lookup_stats.get("labels_upgraded") if lookup_stats else None,
            }
        except Exception as e:
            log.warning(f"Could not compute before/after comparison: {e}")

    if lookup_stats:
        summary["web_and_gemini_lookups"] = lookup_stats

    out_json = cfg["paths"]["c5_summary"]
    Path(out_json).parent.mkdir(parents=True, exist_ok=True)
    with open(out_json, "w") as f:
        json.dump(summary, f, indent=2)

    log.info(f"c5_summary.json written → {out_json}")
    log.info("WP9 DONE")
    return summary
