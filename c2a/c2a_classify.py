"""C2a: Classify and prioritise in-network rate files from the C1 plan table.

Reads the plan-level CSV produced by C1 (c1_plans_ca.csv) and produces:

  c2a_rate_files.csv        one row per unique rate_file_url with classification
                            metadata and a download priority score
  c2a_plans.csv             c1 rows enriched with the file classification
  c2a_skipped.csv           rows whose rate file was rejected with a reason
  c2a_summary.json          counts for the run summary

Classification rules
--------------------
Each unique rate_file_url is assessed for:

  file_format   json | json.gz | json.bz2 | json.zst | parquet | csv | unknown
  file_size_mb  from the Content-Length HEAD response (blank if unavailable)
  http_status   200 | 4xx | 5xx | error
  is_reachable  True if http_status == 200

Files are then classified into one of three download tiers:

  tier 1  reachable JSON (plain or compressed) - highest priority
  tier 2  reachable but unknown / non-standard format
  tier 3  unreachable or HEAD-error

Within tier 1 files are ranked by ascending file_size_mb so the smallest
(fastest) files download first.

Outputs (in --out-dir):
  c2a_rate_files.csv        url, format, size, status, tier, priority_rank
  c2a_plans.csv             all input rows + file classification columns
  c2a_skipped.csv           rows whose file is tier 3, with skip_reason
  c2a_summary.json          run metadata and counts

Usage:
    python c2a/c2a_classify.py --plans output/c1/c1_plans_ca.csv --out-dir output/c2a
    python c2a/c2a_classify.py --plans output/c1/c1_plans_ca.csv --out-dir output/c2a --no-head
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FORMAT_RE = [
    ("json.gz",  re.compile(r"\.json\.gz$",  re.I)),
    ("json.bz2", re.compile(r"\.json\.bz2$", re.I)),
    ("json.zst", re.compile(r"\.json\.zst$", re.I)),
    ("json",     re.compile(r"\.json$",       re.I)),
    ("parquet",  re.compile(r"\.parquet$",    re.I)),
    ("csv",      re.compile(r"\.csv$",        re.I)),
]

TIER_LABELS = {1: "ready", 2: "non_standard", 3: "unreachable"}

RATE_FILE_COLS = [
    "priority_rank", "tier", "tier_label",
    "rate_file_url", "rate_file_name", "brand",
    "plan_rows", "unique_plan_ids",
    "file_format", "is_reachable",
    "http_status", "head_error",
    "file_size_mb", "content_type", "last_modified",
]

ENRICH_COLS = [
    "file_format", "is_reachable", "http_status", "head_error",
    "file_size_mb", "tier", "tier_label", "priority_rank", "skip_reason",
]

SKIP_COLS = [
    "brand", "plan_id", "plan_name", "plan_market_type",
    "rate_file_url", "rate_file_name",
    "file_format", "http_status", "head_error", "tier", "skip_reason",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def detect_format(url: str) -> str:
    path = urllib.parse.urlparse(url).path
    for fmt, pat in FORMAT_RE:
        if pat.search(path):
            return fmt
    return "unknown"


def head_request(args_tuple) -> tuple[str, dict]:
    """Worker-safe: takes (url, timeout) tuple, returns (url, info)."""
    url, timeout = args_tuple
    info: dict = {
        "http_status": "",
        "content_length": "",
        "last_modified": "",
        "content_type": "",
        "head_error": "",
    }
    if not url:
        info["head_error"] = "empty_url"
        return url, info
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            info["http_status"] = r.status
            info["content_length"] = r.headers.get("Content-Length", "")
            info["last_modified"] = r.headers.get("Last-Modified", "")
            info["content_type"] = r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        info["http_status"] = e.code
        info["head_error"] = str(e.reason)
    except Exception as e:
        info["head_error"] = f"{type(e).__name__}: {e}"
    return url, info


def bytes_to_mb(raw: str) -> str:
    try:
        return f"{int(raw) / 1_048_576:.1f}"
    except (ValueError, TypeError):
        return ""


def assign_tier(fmt: str, is_reachable: bool) -> int:
    if not is_reachable:
        return 3
    if fmt in {"json", "json.gz", "json.bz2", "json.zst"}:
        return 1
    return 2


def build_skip_reason(tier: int, http_status, head_error: str) -> str:
    if tier < 3:
        return ""
    if head_error:
        return f"network_error: {head_error}"
    return f"http_{http_status}"


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def read_plans(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def build_file_table(plans: list[dict]) -> dict[str, dict]:
    by_url: dict[str, dict] = {}
    for row in plans:
        url = row.get("rate_file_url", "").strip()
        if url not in by_url:
            by_url[url] = {
                "rate_file_url": url,
                "rate_file_name": row.get("rate_file_name", ""),
                "brand": row.get("brand", ""),
                "plan_rows": 0,
                "unique_plan_ids": set(),
                "file_format": detect_format(url),
                "http_status": "",
                "head_error": "",
                "content_length": "",
                "content_type": "",
                "last_modified": "",
                "is_reachable": False,
                "file_size_mb": "",
                "tier": 3,
                "tier_label": "",
                "priority_rank": 0,
                "skip_reason": "",
            }
        by_url[url]["plan_rows"] += 1
        by_url[url]["unique_plan_ids"].add(row.get("plan_id", ""))
    for entry in by_url.values():
        entry["unique_plan_ids"] = len(entry["unique_plan_ids"])
    return by_url


def run_head_checks(file_table: dict[str, dict], workers: int, timeout: int) -> None:
    urls = list(file_table.keys())
    log(f"HEAD-checking {len(urls)} unique rate file URL(s) ...")
    tasks = [(url, timeout) for url in urls]
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        for url, info in pool.map(head_request, tasks):
            entry = file_table[url]
            entry.update(info)
            entry["is_reachable"] = str(info["http_status"]) == "200"
            entry["file_size_mb"] = bytes_to_mb(info["content_length"])


def classify_and_rank(file_table: dict[str, dict]) -> list[dict]:
    entries = list(file_table.values())
    for e in entries:
        e["tier"] = assign_tier(e["file_format"], e["is_reachable"])
        e["tier_label"] = TIER_LABELS[e["tier"]]
        e["skip_reason"] = build_skip_reason(
            e["tier"], e["http_status"], e["head_error"]
        )

    def sort_key(e):
        try:
            size = float(e["file_size_mb"])
        except (ValueError, TypeError):
            size = float("inf")
        return (e["tier"], size, e["rate_file_url"])

    entries.sort(key=sort_key)
    for rank, e in enumerate(entries, start=1):
        e["priority_rank"] = rank
    return entries


def enrich_plans(
    plans: list[dict], file_table: dict[str, dict]
) -> tuple[list[dict], list[dict]]:
    enriched, skipped = [], []
    for row in plans:
        url = row.get("rate_file_url", "").strip()
        info = file_table.get(url, {})
        enriched_row = dict(row)
        for col in ENRICH_COLS:
            enriched_row[col] = info.get(col, "")
        enriched.append(enriched_row)
        if info.get("tier", 3) == 3:
            skipped.append({k: enriched_row.get(k, "") for k in SKIP_COLS})
    return enriched, skipped


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def write_csv(path: str, rows: list[dict], columns: list[str] | None = None) -> None:
    cols = columns or (list(rows[0].keys()) if rows else [])
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        if rows:
            w.writerows(rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--plans", required=True,
        help="Path to c1_plans_ca.csv (output of C1 step)",
    )
    ap.add_argument(
        "--out-dir", default="output/c2a",
        help="Output folder (default: output/c2a)",
    )
    ap.add_argument(
        "--workers", type=int, default=min(16, (os.cpu_count() or 4) * 2),
        help="Parallel workers for HEAD checks (default: 2x CPU, max 16)",
    )
    ap.add_argument(
        "--no-head", action="store_true",
        help="Skip HEAD requests; classify by URL pattern only",
    )
    ap.add_argument(
        "--timeout", type=int, default=30,
        help="Timeout in seconds per HEAD request (default: 30)",
    )
    args = ap.parse_args(argv)

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    # --- Read plans ----------------------------------------------------------
    log(f"reading plans from {args.plans}")
    plans = read_plans(args.plans)
    unique_urls = len({r.get("rate_file_url", "") for r in plans})
    log(f"  {len(plans)} plan rows, {unique_urls} unique rate file URLs")

    # --- Build per-URL table -------------------------------------------------
    file_table = build_file_table(plans)

    # --- HEAD checks ---------------------------------------------------------
    if args.no_head:
        log("--no-head: skipping HEAD checks, classifying by URL pattern only")
        for entry in file_table.values():
            entry["is_reachable"] = False
            entry["http_status"] = "skipped"
    else:
        run_head_checks(file_table, args.workers, args.timeout)

    # --- Classify & rank -----------------------------------------------------
    ranked_files = classify_and_rank(file_table)

    # --- Enrich plans --------------------------------------------------------
    enriched_plans, skipped = enrich_plans(plans, file_table)

    # --- Plan column list ----------------------------------------------------
    c1_cols = list(plans[0].keys()) if plans else []
    plan_cols = c1_cols + [c for c in ENRICH_COLS if c not in c1_cols]

    # --- Write outputs -------------------------------------------------------
    write_csv(str(out / "c2a_rate_files.csv"), ranked_files, RATE_FILE_COLS)
    write_csv(str(out / "c2a_plans.csv"),      enriched_plans, plan_cols)
    write_csv(str(out / "c2a_skipped.csv"),    skipped, SKIP_COLS)

    # --- Summary -------------------------------------------------------------
    tier_counts = {1: 0, 2: 0, 3: 0}
    for e in ranked_files:
        tier_counts[e["tier"]] += 1

    fmt_counts: dict[str, int] = {}
    for e in ranked_files:
        fmt_counts[e["file_format"]] = fmt_counts.get(e["file_format"], 0) + 1

    summary = {
        "run_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "input_plans_file": str(args.plans),
        "plan_rows": len(plans),
        "unique_rate_files": len(ranked_files),
        "head_checks_performed": not args.no_head,
        "tier_counts": {
            "tier1_ready": tier_counts[1],
            "tier2_non_standard": tier_counts[2],
            "tier3_unreachable": tier_counts[3],
        },
        "format_counts": fmt_counts,
        "skipped_plan_rows": len(skipped),
    }

    with open(str(out / "c2a_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print(json.dumps(summary, indent=2))
    log(f"\nOutputs written to {out}/")
    log(f"  c2a_rate_files.csv  ({len(ranked_files)} files)")
    log(f"  c2a_plans.csv       ({len(enriched_plans)} rows)")
    log(f"  c2a_skipped.csv     ({len(skipped)} rows)")
    log(f"  c2a_summary.json")


if __name__ == "__main__":
    main()
