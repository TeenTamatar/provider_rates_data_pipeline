"""pipeline/c5/wp9_summary.py – WP9: Comprehensive execution summary generation.

Produces c5_summary.json:
- Counts and shares by label (NPIs and TINs separately), split by entity_type
- State source contribution (DCA, CDPH matched/claims/eligible)
- Verified shares before/after split by individual and organization
- Remaining unverified targets split by individual and organization
- Geocode match rate
- Claims by source family
- Stale claims by reason
- Deactivated NPIs excluded
- No-claim targets
- Leftover queue size
- Strict versus inclusive split
- Run date, config hash, and elapsed time

Also writes c5_source_contribution.csv with per-source-family breakdown.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from .common import config_hash, get_logger

log = get_logger("c5.wp9")


def _label_counts_by_entity(
    addresses: pd.DataFrame,
    entity_col: str = "entity_type",
) -> dict:
    """Return label counts/shares split by entity_type."""
    result = {}
    if entity_col not in addresses.columns:
        return result
    for et in ["individual", "organization", "unknown"]:
        sub = addresses[addresses[entity_col] == et]
        if sub.empty:
            continue
        counts = sub["label"].value_counts().to_dict()
        total = len(sub)
        shares = {k: round(v / total, 4) for k, v in counts.items()}
        result[et] = {"counts": counts, "shares": shares, "total": total}
    return result


def _npi_label_by_entity(
    addresses: pd.DataFrame,
    targets: pd.DataFrame,
) -> dict:
    """Return unique-NPI label breakdown split by entity_type."""
    result = {}
    if "entity_type" not in targets.columns:
        return result

    npi_entity = targets.drop_duplicates("npi")[["npi", "entity_type"]].set_index("npi")["entity_type"].to_dict()
    npi_label_priority = {"verified": 1, "probable": 2, "unverified": 3}
    npi_labels = (
        addresses.assign(priority=addresses["label"].map(npi_label_priority))
        .sort_values(by="priority")
        .groupby("npi")["label"]
        .first()
    )

    for et in ["individual", "organization", "unknown"]:
        npis_in_et = {n for n, e in npi_entity.items() if e == et}
        sub = npi_labels[npi_labels.index.isin(npis_in_et)]
        if sub.empty:
            continue
        counts = sub.value_counts().to_dict()
        total = len(sub)
        shares = {k: round(v / total, 4) for k, v in counts.items()}
        result[et] = {"counts": counts, "shares": shares, "total": total}
    return result


def run_wp9(
    cfg: dict,
    targets: pd.DataFrame,
    claims: pd.DataFrame,
    sites: pd.DataFrame,
    stale_claims: pd.DataFrame,
    addresses: pd.DataFrame,
    leftovers: pd.DataFrame,
    lookup_stats: Optional[dict[str, Any]] = None,
    state_stats: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Compile and write c5_summary.json. Returns summary dict."""
    log.info("=== WP9: Write c5_summary.json ===")

    # Label counts by target (TIN-NPI pair)
    label_counts = addresses["label"].value_counts().to_dict()
    total_targets = len(addresses)
    label_shares = {k: round(v / total_targets, 4) for k, v in label_counts.items()} if total_targets else {}

    # Loose label counts
    label_loose_counts = {}
    if "label_loose" in addresses.columns:
        label_loose_counts = addresses["label_loose"].value_counts().to_dict()

    # Label counts by entity_type (target level)
    label_by_entity = _label_counts_by_entity(addresses)

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

    # NPI labels by entity_type
    npi_label_by_entity = _npi_label_by_entity(addresses, targets)

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

    # ── State source contribution ─────────────────────────────────────────────
    state_contribution: dict[str, Any] = {}
    if state_stats:
        dca_s = state_stats.get("dca", {})
        cdph_s = state_stats.get("cdph", {})
        state_contribution = {
            "dca_license_bulk": {
                "source_type": "individuals",
                "eligible_targets": dca_s.get("eligible_targets", 0),
                "matched": dca_s.get("matched", 0),
                "match_rate": round(
                    dca_s.get("matched", 0) / dca_s.get("eligible_targets", 1), 4
                ) if dca_s.get("eligible_targets") else 0.0,
                "ambiguous": dca_s.get("ambiguous", 0),
                "excluded_pobox": dca_s.get("excluded_pobox", 0),
                "excluded_city_only": dca_s.get("excluded_city_only", 0),
                "excluded_non_ca": dca_s.get("excluded_non_ca", 0),
                "excluded_no_license": dca_s.get("excluded_no_license", 0),
                "claims_created": dca_s.get("claims_created", 0),
                "by_board": dca_s.get("by_board", {}),
            },
            "cdph_facility": {
                "source_type": "organizations",
                "eligible_targets": cdph_s.get("eligible_targets", 0),
                "matched": cdph_s.get("matched", 0),
                "match_rate": round(
                    cdph_s.get("matched", 0) / cdph_s.get("eligible_targets", 1), 4
                ) if cdph_s.get("eligible_targets") else 0.0,
                "matched_by_method": cdph_s.get("matched_by_method", {}),
                "claims_created": cdph_s.get("claims_created", 0),
                "npi_populated_share": cdph_s.get("npi_populated_share", 0.0),
            },
        }

        # Write c5_source_contribution.csv
        rows = []
        for src, s in state_contribution.items():
            rows.append({
                "source": src,
                "source_type": s.get("source_type", ""),
                "eligible_targets": s.get("eligible_targets", 0),
                "matched": s.get("matched", 0),
                "match_rate": s.get("match_rate", 0.0),
                "claims_created": s.get("claims_created", 0),
            })
        contrib_path = str(Path(cfg["paths"]["c5_addresses"]).parent / "c5_source_contribution.csv")
        pd.DataFrame(rows).to_csv(contrib_path, index=False)
        log.info(f"c5_source_contribution.csv written → {contrib_path}")

    # ── Before / After comparison ─────────────────────────────────────────────
    before_path = None
    for cand in [
        Path("outputs/c5_before_state_sources/c5_summary.json"),
        Path("outputs/c5_before_tiered/c5_summary.json"),
        Path("output/c5_before_tiered/c5_summary.json"),
        Path("output/c5_baseline/c5_summary.json"),
    ]:
        if cand.is_file():
            before_path = cand
            break

    before_after: dict[str, Any] = {}
    if before_path is not None:
        try:
            with open(before_path) as bf:
                base_s = json.load(bf)
            before_counts = base_s.get("verification_results", {}).get("by_target_pair", {}).get("counts", {})
            before_after = {
                "baseline_source": str(before_path),
                "before_counts": before_counts,
                "after_counts": label_counts,
                "labels_upgraded": lookup_stats.get("labels_upgraded") if lookup_stats else None,
                "after_counts_by_entity": label_by_entity,
            }
        except Exception as e:
            log.warning(f"Could not compute before/after comparison: {e}")

    # ── Remaining unverified ──────────────────────────────────────────────────
    unverified_remaining: dict[str, Any] = {}
    if "entity_type" in addresses.columns:
        unv = addresses[addresses["label"] == "unverified"]
        for et in ["individual", "organization"]:
            sub = unv[unv["entity_type"] == et]
            unverified_remaining[et] = {
                "target_pairs": len(sub),
                "unique_npis": sub["npi"].nunique(),
            }
        unverified_remaining["total"] = len(unv)

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
                "counts_loose": label_loose_counts,
                "by_entity_type": label_by_entity,
            },
            "by_unique_npi": {
                "counts": npi_counts,
                "shares": npi_shares,
                "by_entity_type": npi_label_by_entity,
            },
            "by_unique_tin": {
                "counts": tin_counts,
                "shares": tin_shares,
            },
        },
        "unverified_remaining": unverified_remaining,
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
        "state_source_contribution": state_contribution,
        "before_after_comparison": before_after,
    }

    if lookup_stats:
        summary["web_and_gemini_lookups"] = lookup_stats

    out_json = cfg["paths"]["c5_summary"]
    Path(out_json).parent.mkdir(parents=True, exist_ok=True)
    with open(out_json, "w") as f:
        json.dump(summary, f, indent=2)

    log.info(f"c5_summary.json written → {out_json}")
    log.info("WP9 DONE")
    return summary
