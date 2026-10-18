"""pipeline/c5/wp8_validation.py – WP8: Calibration sampling and precision evaluation.

Produces a stratified sample of ~200 TIN-NPI pairs across:
- entity_type: individual vs organization
- tin_size: small (<=10) vs large (>10)
- label: verified, probable, unverified
- source_stratum: dca_supported, cdph_supported, bulk_only

Provides precision calculation helpers for calibration and evaluation.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from .common import get_logger

log = get_logger("c5.wp8")


def generate_calibration_sample(
    cfg: dict,
    targets: pd.DataFrame,
    addresses: pd.DataFrame,
    n_samples: int = 200,
) -> pd.DataFrame:
    """Generate stratified sample of ~200 TIN-NPI pairs with human audit columns."""
    log.info("=== WP8: Generate calibration sample ===")

    seed = int(cfg.get("random_seed", 42))
    small_tin_max = int(cfg.get("small_tin_max_npis", 10))

    # Join targets with addresses safely without colliding columns
    tin_sizes = targets.groupby("tin")["npi"].nunique().rename("tin_size")
    target_cols = [c for c in ["entity_type", "business_name"] if c not in addresses.columns]
    if target_cols:
        merged = addresses.merge(
            targets[["tin", "npi"] + target_cols].drop_duplicates(),
            on=["tin", "npi"],
            how="left",
        )
    else:
        merged = addresses.copy()
    merged = merged.merge(tin_sizes, on="tin", how="left")
    merged["entity_type"] = merged["entity_type"].fillna("unknown")

    merged["size_category"] = merged["tin_size"].apply(
        lambda s: "small" if s <= small_tin_max else "large"
    )

    # Assign source stratum: dca_supported (individuals with state_license in source_list),
    # cdph_supported (organizations with state_facility in source_list), else bulk_only.
    def _source_stratum(row: pd.Series) -> str:
        sl = str(row.get("source_list") or "")
        entity = str(row.get("entity_type") or "")
        if entity == "individual" and "state_license" in sl:
            return "dca_supported"
        if entity == "organization" and "state_facility" in sl:
            return "cdph_supported"
        return "bulk_only"

    merged["source_stratum"] = merged.apply(_source_stratum, axis=1)

    # Strata definition: (entity_type, size_category, label, source_stratum)
    strata_groups = merged.groupby(["entity_type", "size_category", "label", "source_stratum"])
    n_groups = len(strata_groups)
    per_group = max(1, n_samples // max(n_groups, 1))

    sample_list = []
    for _, grp in strata_groups:
        sample_list.append(grp.sample(min(len(grp), per_group), random_state=seed))

    df_sample = pd.concat(sample_list).drop_duplicates(subset=["tin", "npi"])
    # If short of n_samples, top up randomly
    if len(df_sample) < n_samples and len(merged) > len(df_sample):
        remaining = merged[~merged.set_index(["tin", "npi"]).index.isin(df_sample.set_index(["tin", "npi"]).index)]
        needed = min(n_samples - len(df_sample), len(remaining))
        df_sample = pd.concat([df_sample, remaining.sample(needed, random_state=seed)])

    # Add audit columns
    df_sample["checked_address"] = ""
    df_sample["checked_source_url"] = ""
    df_sample["correct"] = ""  # human fills: yes / no
    df_sample["notes"] = ""

    cols = [
        "tin", "npi", "entity_type", "size_category", "tin_size", "source_stratum",
        "label", "score", "address_line", "unit", "city", "state", "zip5",
        "n_independent_families", "source_list",
        "checked_address", "checked_source_url", "correct", "notes",
    ]
    out_df = df_sample[[c for c in cols if c in df_sample.columns]]

    out_csv = cfg["paths"]["c5_sample"]
    Path(out_csv).parent.mkdir(parents=True, exist_ok=True)
    out_df.to_csv(out_csv, index=False)
    log.info(f"c5_calibration_sample written: {len(out_df):,} rows → {out_csv}")

    # Log stratum breakdown
    stratum_counts = out_df["source_stratum"].value_counts().to_dict() if "source_stratum" in out_df.columns else {}
    log.info(f"  Calibration sample strata: {stratum_counts}")

    log.info("WP8 DONE")
    return out_df


def evaluate_precision(audit_df: pd.DataFrame) -> dict[str, Any]:
    """Calculate empirical precision from filled-in audit sheet."""
    filled = audit_df[audit_df["correct"].str.lower().isin(["yes", "no", "y", "n"])].copy()
    if filled.empty:
        return {"status": "unfilled", "n_reviewed": 0}

    filled["is_correct"] = filled["correct"].str.lower().isin(["yes", "y"])
    metrics: dict[str, Any] = {"n_reviewed": len(filled)}

    for label in ["verified", "probable", "unverified"]:
        sub = filled[filled["label"] == label]
        if not sub.empty:
            metrics[f"precision_{label}"] = float(sub["is_correct"].mean())
            metrics[f"count_{label}"] = len(sub)

    return metrics
