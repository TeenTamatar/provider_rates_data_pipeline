"""C2b: Network-File Agent

For each rate file in the C2a output:
  - Tier 1 (reachable JSON): stream-download and parse into parquet
  - Tier 3 (unreachable / 404): attempt URL recovery via candidate probing,
    then parse if found

Recovery strategy for broken URLs
----------------------------------
The known URL pattern is:
  <base>/<date>_centene-management-company-llc_<brand>[-<state>]_in-network.json

For a 404 we try:
  1. Variant suffixes: brand-ar, brand-or, brand-xx for every US state code
  2. Date variants: walk back/forward up to 60 days from the index date
  3. Compressed variants: .json.gz, .json.bz2
  4. Fetch the payer's CMS MRF index page and scan for matching URLs

Outputs (per rate file, in --out-dir/<brand>_<state>/):
  rates.parquet       provider × code × rate rows
  meta.json           file-level metadata (source url, date, schema version)

Summary:
  c2b_summary.json    per-file status, row counts, recovery actions taken

Usage:
    python c2b/c2b_agent.py --rate-files output/c2a/c2a_rate_files.csv --out-dir output/c2b
    python c2b/c2b_agent.py --rate-files output/c2a/c2a_rate_files.csv --out-dir output/c2b --tier 1
    python c2b/c2b_agent.py --rate-files output/c2a/c2a_rate_files.csv --out-dir output/c2b --brand qualchoice --recover-only
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import gzip
import bz2
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

try:
    import ijson
    from ijson.common import ObjectBuilder
except ImportError:
    sys.exit("ijson is required: pip install ijson")

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:
    sys.exit("pyarrow is required: pip install pyarrow")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

US_STATES = (
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN "
    "MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA "
    "WV WI WY PR VI GU AS MP"
).split()

BASE_URL = (
    "https://www.centene.com/content/dam/centene/Centene%20Corporate/"
    "json/DOCUMENT/"
)

# Centene price transparency page (discovered via browser agent)
CENTENE_TRANSPARENCY_URL = "https://www.centene.com/price-transparency-files.html"

# Known employer sub-index URL patterns for brands that split files by employer.
# Each value is a list of sub-index URL templates (use {date} and {brand}).
SUB_INDEX_PATTERNS = {
    "qualchoice": [
        "{base}{date}_{brand}_alf_plan_index.json",
        "{base}{date}_{brand}_arnold_and_blevins_index.json",
        "{base}{date}_{brand}_big_four_index.json",
        "{base}{date}_{brand}_city_of_fort_smith_index.json",
        "{base}{date}_{brand}_city_of_hot_springs_index.json",
        "{base}{date}_{brand}_dillards_index.json",
        "{base}{date}_{brand}_jv_manufacturing_index.json",
    ]
}

# Parquet schema for rate rows
RATE_SCHEMA = pa.schema([
    ("reporting_entity_name", pa.string()),
    ("reporting_entity_type", pa.string()),
    ("plan_name",             pa.string()),
    ("plan_id",               pa.string()),
    ("plan_id_type",          pa.string()),
    ("plan_market_type",      pa.string()),
    ("npi",                   pa.string()),
    ("tin_type",              pa.string()),
    ("tin_value",             pa.string()),
    ("billing_code_type",     pa.string()),
    ("billing_code",          pa.string()),
    ("billing_code_modifier", pa.string()),
    ("negotiated_type",       pa.string()),
    ("negotiated_rate",       pa.float64()),
    ("expiration_date",       pa.string()),
    ("service_code",          pa.string()),
    ("billing_class",         pa.string()),
    ("additional_information",pa.string()),
    ("source_url",            pa.string()),
])

CHUNK_ROWS = 100_000   # flush parquet every N rows


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def head_ok(url: str, timeout: int = 20) -> bool:
    """Return True if a HEAD request returns HTTP 200."""
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def open_url_stream(url: str, timeout: int = 120):
    """Open a URL for streaming. Returns a file-like binary object."""
    req = urllib.request.Request(url, headers={"Accept-Encoding": "identity"})
    return urllib.request.urlopen(req, timeout=timeout)


def decompress_stream(stream, url: str):
    """Wrap stream in a decompressor based on file extension."""
    lower = url.lower()
    if lower.endswith(".gz"):
        return gzip.GzipFile(fileobj=stream)
    if lower.endswith(".bz2"):
        return bz2.BZ2File(stream)
    return stream


# ---------------------------------------------------------------------------
# URL Recovery
# ---------------------------------------------------------------------------

def extract_url_parts(url: str) -> dict:
    """Parse known Centene URL pattern into components."""
    name = os.path.basename(urllib.parse.unquote(urllib.parse.urlparse(url).path))
    # e.g. 2026-09-28_centene-management-company-llc_qualchoice_in-network.json
    m = re.match(
        r"(?P<date>\d{4}-\d{2}-\d{2})_(?P<entity>[^_]+(?:_[^_]+)*)_"
        r"(?P<brand>[a-z0-9]+)(?:-(?P<state>[a-z]{2}))?_in-network"
        r"(?P<ext>\.json(?:\.gz|\.bz2|\.zst)?)",
        name, re.I
    )
    if not m:
        return {}
    return {
        "date": m.group("date"),
        "entity": m.group("entity"),
        "brand": m.group("brand"),
        "state": m.group("state") or "",
        "ext": m.group("ext"),
    }


def build_url(date: str, entity: str, brand: str, state: str, ext: str) -> str:
    suffix = f"-{state.lower()}" if state else ""
    name = f"{date}_{entity}_{brand}{suffix}_in-network{ext}"
    return BASE_URL + urllib.parse.quote(name, safe="")


def candidate_urls(url: str) -> Iterator[str]:
    """Generate candidate alternative URLs for a broken rate file URL."""
    parts = extract_url_parts(url)
    if not parts:
        return

    date_obj = datetime.strptime(parts["date"], "%Y-%m-%d")
    brand = parts["brand"]
    entity = parts["entity"]
    ext = parts["ext"]
    orig_state = parts["state"]

    # 1. Try every US state code suffix (and no suffix)
    state_variants = [""] + [s.lower() for s in US_STATES]
    for state in state_variants:
        candidate = build_url(parts["date"], entity, brand, state, ext)
        if candidate != url:
            yield candidate

    # 2. Try compressed variants of the original URL pattern
    for comp_ext in [".json.gz", ".json.bz2"]:
        if ext != comp_ext:
            state = orig_state
            yield build_url(parts["date"], entity, brand, state, comp_ext)
            # also without state
            yield build_url(parts["date"], entity, brand, "", comp_ext)

    # 3. Date variants: walk ±60 days
    for delta in range(1, 61):
        for sign in [1, -1]:
            d = (date_obj + timedelta(days=delta * sign)).strftime("%Y-%m-%d")
            yield build_url(d, entity, brand, orig_state, ext)
            yield build_url(d, entity, brand, "", ext)


def scrape_transparency_page(brand: str) -> list[str]:
    """Scrape the Centene transparency page and return JSON URLs matching brand."""
    try:
        log(f"  scraping transparency page for brand '{brand}' ...")
        with urllib.request.urlopen(CENTENE_TRANSPARENCY_URL, timeout=30) as r:
            html = r.read().decode("utf-8", errors="replace")
        # Extract all href/src URLs pointing to .json files containing the brand
        pattern = re.compile(
            r'(?:href|src|location)["\']?\s*[:=]\s*["\']'
            r'(https?://[^"\'>\s]+\.json(?:\.gz|\.bz2)?)["\']',
            re.I
        )
        found = [m.group(1) for m in pattern.finditer(html)
                 if brand.lower() in m.group(1).lower()]
        # Also look for bare URLs in the page
        bare = re.compile(
            r'(https://www\.centene\.com[^\s"\'>]+in-network\.json(?:\.gz|\.bz2)?)',
            re.I
        )
        found += [m.group(1) for m in bare.finditer(html)
                  if brand.lower() in m.group(1).lower()]
        unique = list(dict.fromkeys(found))  # dedupe, preserve order
        log(f"  transparency page: {len(unique)} matching URL(s) found")
        return unique
    except Exception as e:
        log(f"  transparency page scrape failed: {e}")
        return []


def fetch_sub_index_urls(broken_url: str) -> list[str]:
    """For brands with employer sub-indexes, fetch them and collect in-network URLs."""
    parts = extract_url_parts(broken_url)
    if not parts:
        return []
    brand = parts["brand"]
    templates = SUB_INDEX_PATTERNS.get(brand, [])
    if not templates:
        return []
    log(f"  fetching {len(templates)} known sub-index(es) for brand '{brand}' ...")
    found: list[str] = []
    for tmpl in templates:
        sub_url = tmpl.format(
            base=BASE_URL,
            date=parts["date"],
            brand=brand,
        )
        try:
            with urllib.request.urlopen(sub_url, timeout=30) as r:
                data = json.load(r)
            for struct in data.get("reporting_structure", []):
                for f in struct.get("in_network_files", []):
                    loc = f.get("location", "")
                    if loc and loc not in found:
                        found.append(loc)
        except Exception as e:
            log(f"    sub-index error {sub_url}: {e}")
    log(f"  sub-indexes returned {len(found)} unique in-network URL(s)")
    return found


def recover_url(broken_url: str, max_probes: int = 60) -> str | None:
    """Multi-strategy URL recovery. Returns a working URL or None."""
    log(f"  recovering URL for: {broken_url}")
    parts = extract_url_parts(broken_url)
    brand = parts.get("brand", "") if parts else ""

    # Strategy 1: employer sub-indexes (brand-specific)
    sub_urls = fetch_sub_index_urls(broken_url)
    for candidate in sub_urls:
        if candidate == broken_url:
            continue
        log(f"    probing sub-index URL: {candidate}")
        if head_ok(candidate):
            log(f"  FOUND via sub-index: {candidate}")
            return candidate

    # Strategy 2: transparency page scrape
    if brand:
        page_urls = scrape_transparency_page(brand)
        for candidate in page_urls:
            if candidate == broken_url:
                continue
            log(f"    probing page URL: {candidate}")
            if head_ok(candidate):
                log(f"  FOUND via transparency page: {candidate}")
                return candidate

    # Strategy 3: systematic URL variant probing
    probed = 0
    seen = set()
    for candidate in candidate_urls(broken_url):
        if probed >= max_probes:
            break
        if candidate in seen:
            continue
        seen.add(candidate)
        log(f"    probing [{probed+1}/{max_probes}]: {candidate}")
        if head_ok(candidate):
            log(f"  FOUND via probe: {candidate}")
            return candidate
        probed += 1

    log(f"  recovery failed: all strategies exhausted ({probed} probes)")
    return None


# ---------------------------------------------------------------------------
# Streaming JSON parser → rate rows
# ---------------------------------------------------------------------------

def iter_in_network_items(stream) -> Iterator[dict]:
    """Yield each item from the in_network array as a dict."""
    builder = None
    for prefix, event, value in ijson.parse(stream, use_float=True):
        if builder is None:
            if prefix == "in_network.item" and event == "start_map":
                builder = ObjectBuilder()
                builder.event(event, value)
            continue
        builder.event(event, value)
        if prefix == "in_network.item" and event == "end_map":
            yield builder.value
            builder = None


def read_header_fields(stream) -> dict:
    """Pull top-level header fields from the JSON stream."""
    fields = {}
    want = {
        "reporting_entity_name", "reporting_entity_type",
        "last_updated_on", "version",
    }
    for prefix, event, value in ijson.parse(stream, use_float=True):
        if prefix in want and event in ("string", "number"):
            fields[prefix] = value
        # Stop after first in_network item starts (header is done)
        if prefix == "in_network" and event == "start_array":
            break
    return fields


def flatten_item(item: dict, header: dict, source_url: str) -> list[dict]:
    """Flatten one in_network item into one row per provider x rate."""
    rows = []
    code_type = item.get("billing_code_type", "")
    code = str(item.get("billing_code", ""))
    modifiers = item.get("billing_code_modifier") or []
    modifier_str = ";".join(modifiers) if isinstance(modifiers, list) else str(modifiers)
    b_class = item.get("billing_class", "")
    neg_type = item.get("negotiated_type", "")
    add_info = item.get("additional_information", "")

    for pg in item.get("negotiated_rates", []):
        providers = pg.get("provider_groups", [])
        for price in pg.get("negotiated_prices", []):
            rate = price.get("negotiated_rate")
            exp = price.get("expiration_date", "")
            svc_codes = price.get("service_code") or [""]
            if isinstance(svc_codes, str):
                svc_codes = [svc_codes]
            for provider in providers:
                tin_type = (provider.get("tin") or {}).get("type", "")
                tin_value = (provider.get("tin") or {}).get("value", "")
                for npi in provider.get("npi", [""]):
                    for svc in svc_codes:
                        rows.append({
                            "reporting_entity_name": header.get("reporting_entity_name", ""),
                            "reporting_entity_type": header.get("reporting_entity_type", ""),
                            "plan_name":             "",
                            "plan_id":               "",
                            "plan_id_type":          "",
                            "plan_market_type":      "",
                            "npi":                   str(npi),
                            "tin_type":              tin_type,
                            "tin_value":             tin_value,
                            "billing_code_type":     code_type,
                            "billing_code":          code,
                            "billing_code_modifier": modifier_str,
                            "negotiated_type":       neg_type,
                            "negotiated_rate":       float(rate) if rate is not None else None,
                            "expiration_date":       exp,
                            "service_code":          str(svc),
                            "billing_class":         b_class,
                            "additional_information": str(add_info) if add_info else "",
                            "source_url":            source_url,
                        })
    return rows


# ---------------------------------------------------------------------------
# Parquet writer
# ---------------------------------------------------------------------------

class ParquetWriter:
    def __init__(self, path: str):
        self.path = path
        self.writer = None
        self.buffer: list[dict] = []
        self.total = 0

    def add(self, rows: list[dict]) -> None:
        self.buffer.extend(rows)
        if len(self.buffer) >= CHUNK_ROWS:
            self._flush()

    def _flush(self) -> None:
        if not self.buffer:
            return
        table = pa.Table.from_pylist(self.buffer, schema=RATE_SCHEMA)
        if self.writer is None:
            self.writer = pq.ParquetWriter(self.path, schema=RATE_SCHEMA)
        self.writer.write_table(table)
        self.total += len(self.buffer)
        self.buffer = []

    def close(self) -> int:
        self._flush()
        if self.writer:
            self.writer.close()
        return self.total


# ---------------------------------------------------------------------------
# Per-file processing
# ---------------------------------------------------------------------------

def process_file(url: str, out_dir: Path, label: str) -> dict:
    """Download and parse one rate file. Returns a status dict."""
    out_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = str(out_dir / "rates.parquet")
    meta_path = str(out_dir / "meta.json")

    result = {
        "label": label,
        "source_url": url,
        "status": "error",
        "rows_written": 0,
        "error": "",
    }

    log(f"  downloading & parsing: {url}")
    try:
        stream = open_url_stream(url)
        raw = decompress_stream(stream, url)

        # Two-pass: header first, then in_network items
        # We need to buffer a bit - use BytesIO for small files,
        # or re-open for large ones. Re-open is simpler and reliable.
        stream.close()

        # Pass 1: header
        stream1 = decompress_stream(open_url_stream(url), url)
        header = read_header_fields(stream1)
        stream1.close()

        # Pass 2: items
        stream2 = decompress_stream(open_url_stream(url), url)
        writer = ParquetWriter(parquet_path)
        items_seen = 0
        for item in iter_in_network_items(stream2):
            rows = flatten_item(item, header, url)
            writer.add(rows)
            items_seen += 1
            if items_seen % 1000 == 0:
                log(f"    {items_seen} items … {writer.total + len(writer.buffer)} rows")
        total = writer.close()
        stream2.close()

        # meta
        meta = {
            **header,
            "source_url": url,
            "items_parsed": items_seen,
            "rows_written": total,
            "parsed_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)

        result.update({"status": "ok", "rows_written": total})
        log(f"  done: {items_seen} items, {total} rows → {parquet_path}")

    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        log(f"  ERROR: {result['error']}")

    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--rate-files", required=True,
        help="Path to c2a_rate_files.csv",
    )
    ap.add_argument(
        "--out-dir", default="output/c2b",
        help="Root output folder (default: output/c2b)",
    )
    ap.add_argument(
        "--tier", type=int, choices=[1, 2, 3], default=None,
        help="Only process files of this tier (default: all tiers)",
    )
    ap.add_argument(
        "--brand", default=None,
        help="Only process files matching this brand slug",
    )
    ap.add_argument(
        "--recover-only", action="store_true",
        help="Only attempt URL recovery for tier-3 files; do not download",
    )
    ap.add_argument(
        "--max-probes", type=int, default=60,
        help="Max candidate URLs to probe per broken file (default: 60)",
    )
    ap.add_argument(
        "--workers", type=int, default=1,
        help="Parallel download workers (default: 1; large files = 1 is safest)",
    )
    args = ap.parse_args(argv)

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    # Read rate files table
    with open(args.rate_files, newline="", encoding="utf-8") as f:
        rate_files = list(csv.DictReader(f))

    # Filter
    if args.tier is not None:
        rate_files = [r for r in rate_files if int(r["tier"]) == args.tier]
    if args.brand:
        rate_files = [r for r in rate_files if r["brand"].lower() == args.brand.lower()]

    log(f"Agent starting: {len(rate_files)} file(s) to process")

    results = []
    recovered_urls: dict[str, str | None] = {}  # original_url -> found_url

    for row in rate_files:
        url = row["rate_file_url"]
        tier = int(row["tier"])
        brand = row["brand"]
        label = row["rate_file_name"].replace(".json", "").replace(".gz", "")

        log(f"\n[{brand}] tier={tier} {row['rate_file_name']}")

        # --- Tier 3: recover first -------------------------------------------
        if tier == 3:
            found = recover_url(url, max_probes=args.max_probes)
            recovered_urls[url] = found
            if found is None:
                results.append({
                    "label": label,
                    "source_url": url,
                    "status": "recovery_failed",
                    "rows_written": 0,
                    "error": "all probes exhausted",
                })
                continue
            url = found  # use recovered URL for download

        if args.recover_only:
            results.append({
                "label": label,
                "source_url": row["rate_file_url"],
                "recovered_url": url if tier == 3 else "",
                "status": "recovery_only",
                "rows_written": 0,
                "error": "",
            })
            continue

        # --- Download & parse ------------------------------------------------
        file_out = out / label
        result = process_file(url, file_out, label)
        if tier == 3:
            result["recovered_url"] = url
        results.append(result)

    # --- Write summary -------------------------------------------------------
    summary = {
        "run_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "files_attempted": len(results),
        "ok": sum(1 for r in results if r["status"] == "ok"),
        "recovery_failed": sum(1 for r in results if r["status"] == "recovery_failed"),
        "errors": sum(1 for r in results if r["status"] == "error"),
        "total_rows_written": sum(r.get("rows_written", 0) for r in results),
        "files": results,
    }
    summary_path = str(out / "c2b_summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print(json.dumps(summary, indent=2))
    log(f"\nSummary written to {summary_path}")


if __name__ == "__main__":
    main()
