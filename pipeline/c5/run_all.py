"""pipeline/c5/run_all.py – Execute the C5 Address Mapping pipeline end-to-end.

Sequentially executes WP0 through WP9 in accordance with LOGIC.md Section 5:
- WP0: Setup & Readiness
- WP1: Targets
- WP2: Claims Table
- WP3: Parse, Geocode, Sites
- WP4: TIN Site Sets
- WP5: Candidates & Scoring
- WP6: Stale & Conflict Checks, Winner Selection, Labelling
- WP7: Unverified Leftover Queue & Quote Check interface
- WP8: Calibration & Stratified Validation Sample
- WP9: Summary Reporting (c5_summary.json)
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .common import get_logger, load_config
from .wp0_readiness import run_wp0
from .wp1_targets import run_wp1
from .wp2_claims import run_wp2
from .wp3_sites import run_wp3
from .wp4_tin_sites import run_wp4
from .wp5_candidates import run_wp5
from .wp6_choose import run_wp6
from .wp7_leftovers import run_wp7
from .wp8_validation import generate_calibration_sample
from .wp9_summary import run_wp9

log = get_logger("c5.run_all")


def run_pipeline(config_path: Path | None = None, dry_run: bool = False) -> None:
    t0 = time.time()
    cfg = load_config(config_path) if config_path else load_config()

    log.info("=" * 60)
    log.info("STARTING C5 ADDRESS MAPPING PIPELINE (Health Net CA PPO)")
    log.info(f"Target Rate File: {cfg.get('target_rate_file')}")
    log.info(f"Run Date: {cfg.get('run_date')}")
    log.info("=" * 60)

    # WP0: Readiness & Manifest
    manifest = run_wp0(cfg)

    # WP1: Targets
    targets = run_wp1(cfg)

    # WP2: Claims Table
    claims = run_wp2(cfg, targets)

    # WP3: Sites & Geocoding
    claims_with_sites, sites = run_wp3(cfg, claims, dry_run=dry_run)

    # WP4: TIN Site Sets
    tin_sites = run_wp4(cfg, targets, claims_with_sites)

    # WP5: Candidates & Scoring
    candidates = run_wp5(cfg, targets, claims_with_sites, sites, tin_sites)

    # WP6: Stale Checks, Selection, Labelling
    stale_claims, addresses = run_wp6(cfg, targets, claims_with_sites, candidates)

    # WP7: Leftovers Queue & Allowlisted Web / Gemini lookups
    leftovers, addresses, candidates, lookup_stats = run_wp7(
        cfg, targets, addresses, candidates,
        claims=claims_with_sites, sites=sites, tin_sites=tin_sites
    )

    # WP8: Calibration Sample
    sample = generate_calibration_sample(cfg, targets, addresses, n_samples=200)

    # WP9: Summary Report
    summary = run_wp9(cfg, targets, claims_with_sites, sites, stale_claims, addresses, leftovers, lookup_stats=lookup_stats)

    elapsed = round(time.time() - t0, 2)
    log.info("=" * 60)
    log.info(f"C5 PIPELINE COMPLETE in {elapsed}s")
    log.info(f"Summary: Verified: {summary['verification_results']['by_target_pair']['counts'].get('verified', 0):,} | "
             f"Probable: {summary['verification_results']['by_target_pair']['counts'].get('probable', 0):,} | "
             f"Unverified: {summary['verification_results']['by_target_pair']['counts'].get('unverified', 0):,}")
    log.info("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="Run C5 Address Mapping pipeline")
    parser.add_argument("--config", type=str, default=None, help="Path to c5.yaml config")
    parser.add_argument("--dry-run", action="store_true", help="Dry run geocoding (skip census calls)")
    args = parser.parse_args()

    cfg_path = Path(args.config) if args.config else None
    run_pipeline(config_path=cfg_path, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
