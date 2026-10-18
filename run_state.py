"""Task F: Config-driven per-state runner.

Runs C1 filter → C2a (network classify) → C2 evidence → C2 merge for one
state and market. All payer URLs, plan codes and rate limits come from
config/payers.json. No state logic is hardcoded here.

Usage:
    python run_state.py --state TX --market group
    python run_state.py --state CA --market group --no-fetch
    python run_state.py --state OR --market all
    python run_state.py --state TX --market group \\
        --cms-plan-attributes data/plan_attributes_py2026.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def load_config(config_path: str) -> dict:
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)


def run(cmd: list[str], label: str) -> int:
    print(f"\n{'='*60}", flush=True)
    print(f"STEP: {label}", flush=True)
    print(f"CMD:  {' '.join(cmd)}", flush=True)
    print(f"{'='*60}", flush=True)
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        print(f"ERROR: {label} failed with exit code {result.returncode}", file=sys.stderr)
    return result.returncode


def filter_plans(all_plans_csv: str, state: str, market: str, out_csv: str) -> int:
    """Filter c1_plans.csv to one state + market, write filtered CSV."""
    with open(all_plans_csv, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    cols = list(rows[0].keys()) if rows else []

    filtered = [r for r in rows
                if r.get("state","").upper() == state.upper()
                and (market == "all" or r.get("plan_market_type","").lower() == market.lower())]

    Path(out_csv).parent.mkdir(parents=True, exist_ok=True)
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(filtered)

    print(f"Filtered {len(filtered)} plans for state={state} market={market} -> {out_csv}")
    return len(filtered)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--state",  required=True, help="Two-letter state code, e.g. TX")
    ap.add_argument("--market", default="group",
                    help="Plan market type: group | individual | all (default: group)")
    ap.add_argument("--config", default="config/payers.json",
                    help="Payer config file (default: config/payers.json)")
    ap.add_argument("--c1-plans", default="output/c1/c1_plans.csv",
                    help="Full C1 plans CSV (default: output/c1/c1_plans.csv)")
    ap.add_argument("--out-root", default="output",
                    help="Root output folder (default: output)")
    ap.add_argument("--cms-plan-attributes", default="",
                    help="Path to CMS Plan Attributes CSV")
    ap.add_argument("--sbc-patterns", default="",
                    help="Path to JSON file of brand -> SBC URL patterns")
    ap.add_argument("--no-fetch", action="store_true",
                    help="Skip all network fetches in evidence step")
    ap.add_argument("--python", default=str(Path(sys.executable)),
                    help="Python interpreter (default: current venv)")
    args = ap.parse_args(argv)

    state  = args.state.upper()
    market = args.market.lower()
    cfg    = load_config(args.config)
    root   = Path(args.out_root)
    py     = args.python

    # Directories
    state_root = root / f"states/{state}_{market}"
    c1_state   = str(state_root / "c1_plans_state.csv")
    c2a_dir    = str(state_root / "c2a")
    ev_dir     = str(state_root / "c2_evidence")
    c2_dir     = str(state_root / "c2")
    prof_dir   = str(state_root / "docs/state_profiles")

    steps = []

    # ---- Step 1: Filter C1 plans -------------------------------------------
    n = filter_plans(args.c1_plans, state, market, c1_state)
    if n == 0:
        print(f"No plans found for state={state} market={market}. Stopping.")
        sys.exit(0)
    steps.append({"step": "c1_filter", "plans": n, "output": c1_state})

    # ---- Step 2: C2a classify (network type from plan names) ----------------
    cmd2a = [py, "c2/c2a_classify.py", "--plans", c1_state, "--out-dir", c2a_dir]
    rc = run(cmd2a, f"C2a: classify network type for {state}/{market}")
    steps.append({"step": "c2a_classify", "rc": rc, "out_dir": c2a_dir})
    if rc != 0:
        sys.exit(rc)

    # ---- Step 3: C2 evidence (resolve unknowns) ----------------------------
    unresolved = str(Path(c2a_dir) / "c2a_unresolved.csv")
    cmd_ev = [py, "c2/c2_network_evidence.py",
              "--plans", unresolved, "--out-dir", ev_dir]
    if args.cms_plan_attributes:
        cmd_ev += ["--cms-plan-attributes", args.cms_plan_attributes]
    if args.sbc_patterns:
        cmd_ev += ["--sbc-patterns", args.sbc_patterns]
    elif cfg.get("sbc_url_patterns"):
        # Write inline patterns from config to a temp file
        pat_path = str(state_root / "sbc_patterns.json")
        Path(pat_path).parent.mkdir(parents=True, exist_ok=True)
        Path(pat_path).write_text(json.dumps(cfg["sbc_url_patterns"]))
        cmd_ev += ["--sbc-patterns", pat_path]
    if args.no_fetch:
        cmd_ev.append("--no-fetch")
    rc = run(cmd_ev, f"C2 evidence: resolve unknowns for {state}/{market}")
    steps.append({"step": "c2_evidence", "rc": rc, "out_dir": ev_dir})
    if rc != 0:
        sys.exit(rc)

    # ---- Step 4: Merge + state profiles ------------------------------------
    c2a_all = str(Path(c2a_dir) / "c2a_plans.csv")
    ev_all  = str(Path(ev_dir)  / "c2_evidence_all.csv")
    cmd_merge = [py, "c2/c2_merge_network.py",
                 "--plans", c2a_all,
                 "--evidence", ev_all,
                 "--out-dir", c2_dir,
                 "--profiles-dir", prof_dir]
    rc = run(cmd_merge, f"C2 merge: network table + state profile for {state}/{market}")
    steps.append({"step": "c2_merge", "rc": rc, "out_dir": c2_dir})
    if rc != 0:
        sys.exit(rc)

    # ---- Final summary -------------------------------------------------------
    summary = {
        "run_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "state": state, "market": market,
        "steps": steps,
        "outputs": {
            "network_table":  str(Path(c2_dir)   / "c2_plans_network.csv"),
            "state_profile":  str(Path(prof_dir) / f"{state}.md"),
            "evidence_resolved":   str(Path(ev_dir) / "c2_evidence_resolved.csv"),
            "evidence_unresolved": str(Path(ev_dir) / "c2_evidence_unresolved.csv"),
        },
    }
    summary_path = state_root / "run_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2))
    print(f"\nRun complete. Summary: {summary_path}")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
