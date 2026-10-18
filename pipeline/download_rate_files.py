"""Download the rate files listed by C1, with resume and size checks.

Reads `output/c1/c1_rate_files.csv` and downloads every reachable file (HTTP 200)
into `Data/rate_files/`. A file whose local size equals the published
`content_length` is skipped; a partial file is resumed. Files are fetched a few
at a time, and a small manifest is written at the end.

Usage:
    python pipeline/download_rate_files.py
    python pipeline/download_rate_files.py --brand ambetter --workers 4
"""
import argparse
import concurrent.futures as cf
import csv
import datetime as dt
import json
import os
import sys
import time
import urllib.request

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_LIST = os.path.join(PROJECT_ROOT, "output", "c1", "c1_rate_files.csv")
DEFAULT_DIR = os.path.join(PROJECT_ROOT, "Data", "rate_files")
CHUNK = 1 << 20


def download(job):
    url, path, expected = job
    have = os.path.getsize(path) if os.path.exists(path) else 0
    if expected and have == expected:
        return path, "already complete", have
    for attempt in range(5):
        try:
            have = os.path.getsize(path) if os.path.exists(path) else 0
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            if have:
                req.add_header("Range", f"bytes={have}-")
            with urllib.request.urlopen(req, timeout=60) as resp, open(path, "ab" if have and resp.status == 206 else "wb") as out:
                while True:
                    block = resp.read(CHUNK)
                    if not block:
                        break
                    out.write(block)
            size = os.path.getsize(path)
            status = "ok" if not expected or size == expected else f"size mismatch ({size} vs {expected})"
            return path, status, size
        except Exception as e:  # network hiccup: retry with resume
            last = repr(e)
            time.sleep(5 * (attempt + 1))
    return path, f"failed: {last}", os.path.getsize(path) if os.path.exists(path) else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", default=DEFAULT_LIST, help="C1 rate file table (default: output/c1/c1_rate_files.csv)")
    ap.add_argument("--out", default=DEFAULT_DIR, help="download folder (default: Data/rate_files)")
    ap.add_argument("--brand", help="only this brand")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    with open(args.list, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r["http_status"] == "200" and (not args.brand or r["brand"] == args.brand)]
    jobs = [(r["rate_file_url"], os.path.join(args.out, r["rate_file_name"]), int(float(r["content_length"] or 0)))
            for r in rows]
    print(f"{len(jobs)} files, {sum(j[2] for j in jobs) / 1e9:.2f} GB", file=sys.stderr, flush=True)
    results = []
    with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
        for path, status, size in pool.map(download, jobs):
            results.append({"file": os.path.basename(path), "status": status, "bytes": size})
            print(f"{status:>20}  {size / 1e6:8.1f} MB  {os.path.basename(path)}", file=sys.stderr, flush=True)
    manifest = {"downloaded_at": dt.datetime.now().isoformat(timespec="seconds"), "files": results}
    with open(os.path.join(args.out, "download_manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    bad = [r for r in results if r["status"] not in ("ok", "already complete")]
    print(f"done: {len(results) - len(bad)} ok, {len(bad)} problems", file=sys.stderr)


if __name__ == "__main__":
    main()
