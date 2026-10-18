"""C1: Parse payer index (Table of Contents) files into a plan -> rate file table.

Reads Transparency in Coverage index files as a stream, flattens every
reporting structure into one row per plan x in-network rate file, enriches
each row (plan ID type check, HIOS breakdown, state), removes duplicates,
and builds a table of unique rate files.

Outputs (in --out):
    c1_plans.csv              one row per unique plan x rate file
    c1_plans_<state>.csv      the in-scope rows: one state (--state, default CA) and one
                              market (--market, default group; organizers confirmed
                              commercial group only)
    c1_duplicates_removed.csv rows dropped by deduplication, with the key they matched
    c1_rate_files.csv         one row per unique rate file URL
    c1_summary.json           counts for the run summary

Usage:
    python pipeline/c1_parse_index.py
    python pipeline/c1_parse_index.py --index Data/2026-09-30_healthnet_index.json --no-head
    python pipeline/c1_parse_index.py --index https://example.com/index.json --out output/c1

Index sources may be local paths, glob patterns or http(s) URLs. URLs are
streamed to a cache folder inside --out before parsing.
"""
import argparse
import concurrent.futures as cf
import csv
import glob
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone

import ijson
from ijson.common import ObjectBuilder

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_INDEX = os.path.join(PROJECT_ROOT, "Data", "*_index.json")
DEFAULT_OUT = os.path.join(PROJECT_ROOT, "output", "c1")

US_STATES = set(
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS "
    "MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY "
    "PR VI GU AS MP".split()
)

HEADER_KEYS = {"reporting_entity_name", "reporting_entity_type", "last_updated_on", "version"}

# Plan ID shapes. HIOS IDs nest: issuer (5 digits) + state (2 letters) + product (3 digits)
# + plan (4 digits) + variant ("-NN"). EINs are 9 digits.
ID_PATTERNS = [
    ("ein", re.compile(r"^\d{9}$")),
    ("hios_issuer", re.compile(r"^\d{5}$")),
    ("hios_product", re.compile(r"^\d{5}[A-Z]{2}\d{3}$")),
    ("hios_plan", re.compile(r"^\d{5}[A-Z]{2}\d{7}$")),
    ("hios_plan_variant", re.compile(r"^\d{5}[A-Z]{2}\d{7}-\d{2}$")),
]
HIOS_PARTS = re.compile(r"^(?P<issuer>\d{5})(?:(?P<state>[A-Z]{2})(?P<product>\d{3}))?")

# "..._healthnet-ca_in-network.json" -> brand slug "healthnet", suffix "ca"
URL_SLUG = re.compile(r"_(?P<slug>[a-z0-9]+)(?:-(?P<suffix>[a-z]{2}))?_in-network", re.I)
NAME_STATE = re.compile(r"^(?P<state>[A-Z]{2})\s")

PLAN_COLUMNS = [
    "brand", "source_index", "reporting_entity_name", "index_last_updated_on", "index_version",
    "structure_index", "issuer_name", "plan_sponsor_name", "plan_name", "plan_id",
    "plan_id_type", "plan_id_type_inferred", "plan_id_type_mismatch",
    "hios_issuer_id", "hios_state", "hios_product_id", "plan_market_type",
    "state", "state_source", "state_conflict",
    "rate_file_url", "rate_file_name", "rate_file_description", "allowed_amount_url",
]


def log(msg):
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- sources

def resolve_sources(patterns, cache_dir):
    """Expand globs and fetch URLs. Returns a list of local file paths."""
    paths = []
    for p in patterns:
        if re.match(r"^https?://", p, re.I):
            paths.append(fetch_to_cache(p, cache_dir))
            continue
        matches = sorted(glob.glob(p)) if any(ch in p for ch in "*?[") else [p]
        if not matches:
            raise SystemExit(f"No index files match: {p}")
        for m in matches:
            if not os.path.isfile(m):
                raise SystemExit(f"Index file not found: {m}")
            paths.append(os.path.abspath(m))
    return paths


def fetch_to_cache(url, cache_dir):
    os.makedirs(cache_dir, exist_ok=True)
    name = os.path.basename(urllib.parse.urlparse(url).path) or "index.json"
    dest = os.path.join(cache_dir, name)
    log(f"downloading {url}")
    with urllib.request.urlopen(url, timeout=120) as r, open(dest, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    return dest


# ---------------------------------------------------------------- parsing

def read_header(path):
    """Collect top-level scalar fields without building the whole document."""
    header = {}
    with open(path, "rb") as f:
        for prefix, event, value in ijson.parse(f):
            if prefix in HEADER_KEYS and event in ("string", "number"):
                header[prefix] = value
                if len(header) == len(HEADER_KEYS):
                    break
    return header


def iter_structures(path):
    """Yield each reporting_structure entry as a dict, one at a time."""
    builder = None
    with open(path, "rb") as f:
        for prefix, event, value in ijson.parse(f, use_float=True):
            if builder is None:
                if prefix == "reporting_structure.item" and event == "start_map":
                    builder = ObjectBuilder()
                    builder.event(event, value)
                continue
            builder.event(event, value)
            if prefix == "reporting_structure.item" and event == "end_map":
                yield builder.value
                builder = None


def brand_from_filename(path):
    """'2026-09-30_healthnet_index.json' -> 'healthnet'."""
    stem = os.path.splitext(os.path.basename(path))[0]
    parts = [p for p in stem.split("_") if p and not re.match(r"^\d{4}-\d{2}-\d{2}$", p)]
    parts = [p for p in parts if p.lower() not in ("index", "toc", "table", "of", "contents")]
    return "_".join(parts).lower() or stem.lower()


def infer_id_type(plan_id):
    for name, pattern in ID_PATTERNS:
        if pattern.match(plan_id):
            return name
    return "unknown"


def derive_state(rate_file_url, hios_state, plan_name):
    """Pick a state from the strongest available source.

    Order: rate file URL suffix, HIOS state code, a leading state token in the
    plan name (fill_state_from_issuer adds a fourth, after dedup). Returns (state, source, conflict) where conflict lists any
    sources that disagree with the chosen state.
    """
    candidates = []
    m = URL_SLUG.search(rate_file_url or "")
    if m and m.group("suffix") and m.group("suffix").upper() in US_STATES:
        candidates.append(("rate_file_url", m.group("suffix").upper()))
    if hios_state in US_STATES:
        candidates.append(("hios_id", hios_state))
    m = NAME_STATE.match((plan_name or "").upper())
    if m and m.group("state") in US_STATES:
        candidates.append(("plan_name", m.group("state")))
    if not candidates:
        return "", "", ""
    source, state = candidates[0]
    conflicts = [f"{s}={v}" for s, v in candidates[1:] if v != state]
    return state, source, ";".join(conflicts)


def parse_index(path):
    """Parse one index file into plan x rate file rows plus parse stats."""
    header = read_header(path)
    brand = brand_from_filename(path)
    rows, stats = [], Counter()
    for s_idx, structure in enumerate(iter_structures(path)):
        stats["structures"] += 1
        plans = structure.get("reporting_plans") or []
        files = structure.get("in_network_files") or []
        allowed = (structure.get("allowed_amount_file") or {}).get("location", "")
        stats["plan_entries"] += len(plans)
        stats["file_entries"] += len(files)
        if not files:
            stats["structures_without_rate_file"] += 1
            files = [{}]
        for plan in plans:
            plan_id = str(plan.get("plan_id", "")).strip()
            id_type = str(plan.get("plan_id_type", "")).strip().lower()
            inferred = infer_id_type(plan_id)
            family = "hios" if inferred.startswith("hios") else inferred
            hios = HIOS_PARTS.match(plan_id) if family == "hios" else None
            hios_state = (hios.group("state") or "") if hios else ""
            for fobj in files:
                url = fobj.get("location", "")
                state, state_source, conflict = derive_state(url, hios_state, plan.get("plan_name", ""))
                rows.append({
                    "brand": brand,
                    "source_index": os.path.basename(path),
                    "reporting_entity_name": header.get("reporting_entity_name", ""),
                    "index_last_updated_on": header.get("last_updated_on", ""),
                    "index_version": header.get("version", ""),
                    "structure_index": s_idx,
                    "issuer_name": plan.get("issuer_name", ""),
                    "plan_sponsor_name": plan.get("plan_sponsor_name", ""),
                    "plan_name": plan.get("plan_name", ""),
                    "plan_id": plan_id,
                    "plan_id_type": id_type,
                    "plan_id_type_inferred": inferred,
                    "plan_id_type_mismatch": id_type != family,
                    "hios_issuer_id": hios.group("issuer") if hios else "",
                    "hios_state": hios_state,
                    "hios_product_id": (plan_id[:10] if hios and hios.group("product") else ""),
                    "plan_market_type": str(plan.get("plan_market_type", "")).strip().lower(),
                    "state": state,
                    "state_source": state_source,
                    "state_conflict": conflict,
                    "rate_file_url": url,
                    "rate_file_name": os.path.basename(urllib.parse.unquote(urllib.parse.urlparse(url).path)),
                    "rate_file_description": fobj.get("description", ""),
                    "allowed_amount_url": allowed,
                })
    stats["rows"] = len(rows)
    return path, header, rows, stats


# ---------------------------------------------------------------- dedup + file table

def norm(text):
    return re.sub(r"\s+", " ", str(text or "")).strip().upper()


def dedup_key(row):
    return (row["brand"], norm(row["plan_name"]), row["plan_id"], row["plan_id_type"],
            row["plan_market_type"], row["rate_file_url"])


def fill_state_from_issuer(rows):
    """Last-resort state for rows that have a HIOS issuer ID but no state.

    HIOS issuer IDs are assigned per issuer per state, so if every other row
    with the same issuer ID resolves to a single state, this row is in that
    state too. Issuers seen in several states are left blank.
    """
    issuer_states = defaultdict(set)
    for r in rows:
        if r["hios_issuer_id"] and r["state"]:
            issuer_states[r["hios_issuer_id"]].add(r["state"])
    filled = 0
    for r in rows:
        states = issuer_states.get(r["hios_issuer_id"], set())
        if not r["state"] and len(states) == 1:
            r["state"], r["state_source"] = next(iter(states)), "hios_issuer_lookup"
            filled += 1
    return filled


def deduplicate(rows):
    kept, removed, first_seen = [], [], {}
    for row in rows:
        key = dedup_key(row)
        if key in first_seen:
            removed.append({**row, "duplicate_of_row": first_seen[key]})
        else:
            first_seen[key] = len(kept)
            kept.append(row)
    return kept, removed


def head_request(url, timeout=30):
    info = {"http_status": "", "content_length": "", "last_modified": "", "content_type": "", "head_error": ""}
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
    except Exception as e:  # network errors are recorded, not fatal
        info["head_error"] = f"{type(e).__name__}: {e}"
    return info


def build_file_table(plans, do_head, workers):
    by_url = defaultdict(list)
    for row in plans:
        by_url[row["rate_file_url"]].append(row)

    files = []
    for url, rows in by_url.items():
        states = Counter(r["state"] for r in rows if r["state"])
        files.append({
            "rate_file_url": url,
            "rate_file_name": rows[0]["rate_file_name"],
            "brand": rows[0]["brand"],
            "state": ";".join(sorted(states)),
            "plan_rows": len(rows),
            "unique_plan_ids": len({r["plan_id"] for r in rows}),
            "unique_plan_names": len({norm(r["plan_name"]) for r in rows}),
            "market_types": ";".join(sorted({r["plan_market_type"] for r in rows})),
            "plan_id_types": ";".join(sorted({r["plan_id_type"] for r in rows})),
            "plan_id_type_mismatches": sum(r["plan_id_type_mismatch"] for r in rows),
            "hios_product_ids": len({r["hios_product_id"] for r in rows if r["hios_product_id"]}),
            "index_last_updated_on": ";".join(sorted({r["index_last_updated_on"] for r in rows})),
            "allowed_amount_urls": len({r["allowed_amount_url"] for r in rows if r["allowed_amount_url"]}),
        })

    if do_head:
        urls = [f["rate_file_url"] for f in files if f["rate_file_url"]]
        log(f"checking {len(urls)} rate file URLs (HEAD only, nothing downloaded)")
        with cf.ThreadPoolExecutor(max_workers=workers) as pool:
            results = dict(zip(urls, pool.map(head_request, urls)))
        for f in files:
            f.update(results.get(f["rate_file_url"], {}))
    files.sort(key=lambda f: (f["brand"], f["state"], f["rate_file_name"]))
    return files


# ---------------------------------------------------------------- output

def write_csv(path, rows, columns=None):
    columns = columns or (list(rows[0].keys()) if rows else [])
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--index", nargs="+", default=[DEFAULT_INDEX],
                    help="index files: paths, globs or URLs (default: Data/*_index.json)")
    ap.add_argument("--out", default=DEFAULT_OUT, help="output folder (default: output/c1)")
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 4,
                    help="parallel workers for parsing and URL checks")
    ap.add_argument("--no-head", action="store_true", help="skip HEAD checks on rate file URLs")
    ap.add_argument("--state", default="CA",
                    help="in-scope state for the in-scope plan file (default: CA)")
    ap.add_argument("--market", default="group",
                    help="in-scope plan_market_type for the in-scope plan file: group, individual or all "
                         "(default: group, per the organizers)")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    sources = resolve_sources(args.index, os.path.join(args.out, "cache"))
    log(f"parsing {len(sources)} index file(s)")

    results = []
    with cf.ProcessPoolExecutor(max_workers=min(args.workers, len(sources))) as pool:
        for path, header, rows, stats in pool.map(parse_index, sources):
            log(f"  {os.path.basename(path)}: {stats['structures']} structures, {stats['rows']} rows")
            results.append((path, header, rows, stats))

    all_rows = [r for _, _, rows, _ in results for r in rows]
    plans, removed = deduplicate(all_rows)
    state_filled = fill_state_from_issuer(plans)
    files = build_file_table(plans, not args.no_head, args.workers)

    write_csv(os.path.join(args.out, "c1_plans.csv"), plans, PLAN_COLUMNS)
    state = args.state.strip().upper()
    market = args.market.strip().lower()
    state_plans = [r for r in plans if r["state"] == state
                   and (market == "all" or r["plan_market_type"] == market)]
    write_csv(os.path.join(args.out, f"c1_plans_{state.lower()}.csv"), state_plans, PLAN_COLUMNS)
    write_csv(os.path.join(args.out, "c1_duplicates_removed.csv"), removed, PLAN_COLUMNS + ["duplicate_of_row"])
    write_csv(os.path.join(args.out, "c1_rate_files.csv"), files)

    summary = {
        "run_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "index_files_parsed": len(results),
        "per_index": {
            os.path.basename(p): {**dict(s), "last_updated_on": h.get("last_updated_on", ""),
                                  "version": h.get("version", "")}
            for p, h, _, s in results
        },
        "plan_rows_raw": len(all_rows),
        "duplicate_rows_removed": len(removed),
        "plan_rows_unique": len(plans),
        "in_scope_state": state,
        "in_scope_market": market,
        "state_plan_rows_all_markets": sum(1 for r in plans if r["state"] == state),
        "in_scope_plan_rows": len(state_plans),
        "in_scope_rate_files": len({r["rate_file_url"] for r in state_plans}),
        "unique_plan_ids": len({(r["plan_id_type"], r["plan_id"]) for r in plans}),
        "unique_rate_files": len(files),
        "rows_by_market_type": dict(Counter(r["plan_market_type"] for r in plans)),
        "rows_by_state": dict(Counter(r["state"] or "unknown" for r in plans).most_common()),
        "state_source_counts": dict(Counter(r["state_source"] or "none" for r in plans)),
        "state_filled_from_issuer": state_filled,
        "state_conflicts": sum(1 for r in plans if r["state_conflict"]),
        "plan_id_type_mismatches": sum(r["plan_id_type_mismatch"] for r in plans),
        "plan_id_types_inferred": dict(Counter(r["plan_id_type_inferred"] for r in plans)),
        "rate_files_by_state": dict(Counter(f["state"] or "unknown" for f in files)),
        "rate_files_unreachable": sum(1 for f in files if f.get("head_error")),
    }
    with open(os.path.join(args.out, "c1_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
