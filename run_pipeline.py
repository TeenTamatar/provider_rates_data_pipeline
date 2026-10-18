#!/usr/bin/env python3
"""Run the whole pipeline (downloads, C1 to C7) with one command.

    python run_pipeline.py                      # everything, in order
    python run_pipeline.py --list               # show the steps
    python run_pipeline.py --from c4            # resume from a step (name or stage prefix)
    python run_pipeline.py --only c6 c7_geocode # just these steps
    python run_pipeline.py --offline            # no Gemini and no Census calls (C5 estimate/dry run, C7 cached geocodes)

Each step is a separate script under pipeline/; the run stops at the first step that fails.
Files the organizers provide (index JSONs, HiLabs workbook) must already be in Data/; everything
else public is downloaded by the `download` steps (skip them with --skip-download).
"""
import argparse
import datetime as dt
import glob
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable

# Provided by the organizers; cannot be downloaded.
REQUIRED = [
    ("Data/*_index.json", "Centene index files (C1)"),
    ("Data/Rate Benchmarking Individual Weights_Hackathon.xlsx", "HiLabs workbook (C3, C4, C6, C7)"),
]


def steps(a):
    """(name, command) in run order."""
    rd = ["--run-date", a.run_date]
    c5 = ["--estimate", "--dry-run"] if a.offline else []
    return [
        ("download_public", ["download_c5_data.py"]),  # NPPES, deactivation report, CMS DAC, NUCC
        ("download_cms_ref", ["download_c5_data.py", "--file", "cms_ref"]),
        ("c1_parse_index", ["pipeline/c1_parse_index.py"]),
        ("download_rate_files", ["pipeline/download_rate_files.py"]),
        ("c2_step1_ppo_names", ["pipeline/c2_step1_ppo_names.py"]),
        ("c2_step2_other_network_names", ["pipeline/c2_step2_other_network_names.py"]),
        ("c2_step3_unlabelled_names", ["pipeline/c2_step3_unlabelled_names.py"]),
        ("c3_reference", ["pipeline/c3_reference.py"]),
        ("rate_scan", ["pipeline/rate_scan.py", *rd]),
        ("c2b_step1_groups", ["pipeline/c2b_step1_groups.py"]),
        ("c2b_step2_name_labels", ["pipeline/c2b_step2_name_labels.py"]),
        ("c2b_step3_capitation", ["pipeline/c2b_step3_capitation.py"]),
        ("c2b_step4_contract_dates", ["pipeline/c2b_step4_contract_dates.py"]),
        ("c2b_step5_parallel_copies", ["pipeline/c2b_step5_parallel_copies.py"]),
        ("c2b_export_kept", ["pipeline/c2b_export_kept.py"]),
        ("c2b_step6_ppo_rates", ["pipeline/c2b_step6_ppo_rates.py"]),
        ("c2_final_plans", ["pipeline/c2_final_plans.py"]),
        ("c3_metrics", ["pipeline/c3_metrics.py", *rd]),
        ("c3_index_metrics", ["pipeline/c3_index_metrics.py", *rd]),
        ("c3_diagnose", ["pipeline/c3_diagnose.py"]),
        ("c3_report", ["pipeline/c3_report.py"]),
        ("c4_select", ["pipeline/c4_select.py"]),
        ("c5_run", ["pipeline/run_c5.py", *c5]),
        ("c6_provider_rate_file", ["pipeline/c6_provider_rate_file.py"]),
        ("c7_geocode", ["pipeline/c7_geocode.py", *(["--offline"] if a.offline else [])]),
        ("c7_cost_effectiveness", ["pipeline/c7_cost_effectiveness.py"]),
        ("c7_final_output", ["pipeline/c7_final_output.py"]),
    ]


def pick(all_steps, key):
    """First step whose name equals key or starts with it."""
    for i, (n, _) in enumerate(all_steps):
        if n == key or n.startswith(key):
            return i
    sys.exit(f"unknown step: {key} (see --list)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="print the steps and exit")
    ap.add_argument("--from", dest="start", help="first step to run (name or prefix, e.g. c4)")
    ap.add_argument("--to", dest="end", help="last step to run (name or prefix)")
    ap.add_argument("--only", nargs="+", help="run only these steps")
    ap.add_argument("--skip-download", action="store_true", help="skip the download steps")
    ap.add_argument("--offline", action="store_true", help="C5 estimate + dry run (no Gemini/Census), C7 cached geocodes")
    ap.add_argument("--run-date", default=dt.date.today().isoformat(), help="run date for the scan and C3 (default: today)")
    a = ap.parse_args()

    all_steps = steps(a)
    if a.list:
        for i, (n, cmd) in enumerate(all_steps, 1):
            print(f"{i:2d}. {n:30s} python {' '.join(cmd)}")
        return

    if a.only:
        run = [all_steps[pick(all_steps, k)] for k in a.only]
    else:
        i = pick(all_steps, a.start) if a.start else 0
        j = pick(all_steps, a.end) if a.end else len(all_steps) - 1
        run = all_steps[i:j + 1]
    if a.skip_download:
        run = [s for s in run if not s[0].startswith("download")]

    missing = [d for pat, d in REQUIRED if not glob.glob(os.path.join(ROOT, pat))]
    if missing and any(n.startswith(("c1", "c3", "c4", "c6", "c7")) for n, _ in run):
        sys.exit("missing organizer files in Data/: " + "; ".join(missing) + " (see README, Data sources)")

    t0 = time.time()
    for k, (n, cmd) in enumerate(run, 1):
        print(f"\n=== [{k}/{len(run)}] {n}: python {' '.join(cmd)}", flush=True)
        t = time.time()
        r = subprocess.run([PY, *cmd], cwd=ROOT)
        if r.returncode:
            sys.exit(f"step {n} failed (exit {r.returncode}); fix it and resume with: python run_pipeline.py --from {n}")
        print(f"=== {n} done in {time.time() - t:.0f}s", flush=True)
    print(f"\nAll {len(run)} steps done in {(time.time() - t0) / 60:.1f} min.")


if __name__ == "__main__":
    main()
