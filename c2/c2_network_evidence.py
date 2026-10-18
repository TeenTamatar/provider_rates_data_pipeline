"""C2 Network Evidence: resolve unknown-type plans by reading payer documents.

For each plan whose network_type is 'unknown' after C2a name-keyword pass,
this script tries three evidence sources in priority order:

  1. cms_plan_attributes  -- CMS Plan Attributes file (HIOS plan ID join)
  2. sbc_document         -- Summary of Benefits & Coverage PDF (payer-hosted)
  3. model_fallback       -- stub; only wired if user supplies a model

Each successful resolution records:
  network_type   e.g. PPO, HMO, EPO
  is_ppo         true/false/""
  evidence_type  cms_plan_attributes | sbc_document | model_fallback | unknown
  source_url     exact URL fetched
  quote          verbatim sentence from document (checked against document text)
  confidence     high | medium | low | unknown

Hard rules (from AGENT_CONTEXT.md):
  - Never guess network type. Evidence or 'unknown'.
  - Models only read and quote; every answer needs a quote in the source text.
  - Cache every download. A re-run makes zero network calls.
  - robots.txt check and per-host delay before any fetch.
  - Plan-year check: reject documents for wrong year.
  - Do not scrape provider directories (Q10 unanswered).

Usage:
  python c2/c2_network_evidence.py --plans output/c2a_network/c2a_unresolved.csv \\
      --out-dir output/c2_evidence \\
      --cms-plan-attributes data/plan_attributes_py2026.csv
  python c2/c2_network_evidence.py --plans output/c2a_network/c2a_unresolved.csv \\
      --out-dir output/c2_evidence --no-fetch
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PLAN_YEAR = 2026          # reject documents labelled for any other year
FETCH_DELAY_S = 1.0       # seconds between requests to the same host
FETCH_TIMEOUT_S = 30
MAX_PDF_BYTES = 10 * 1024 * 1024   # 10 MB cap on PDF downloads

# Network type keywords for document search (ordered: most specific first)
NET_TYPE_PATTERNS = [
    ("PPO",  re.compile(r"\bPPO\b",              re.I)),
    ("EPO",  re.compile(r"\bEPO\b",              re.I)),
    ("HMO",  re.compile(r"\bHMO\b",              re.I)),
    ("POS",  re.compile(r"(?<![A-Z])POS(?![A-Z])", re.I)),
]

PLAN_YEAR_RE = re.compile(r"\b(20\d\d)\b")

# CMS Plan Attributes column names (verified against the actual file header).
# We detect the real column names at load time; these are fallback candidates.
CMS_PLAN_ID_CANDIDATES   = ["PlanId", "plan_id", "PLAN_ID", "hios_plan_identifier"]
CMS_PLAN_TYPE_CANDIDATES = ["PlanType", "plan_type", "PLAN_TYPE", "metal_level"]
CMS_PLAN_YEAR_CANDIDATES = ["BusinessYear", "business_year", "PLAN_YEAR"]

# Output columns (all plans, enriched)
OUT_COLS = [
    "brand", "source_index", "plan_name", "plan_id", "plan_id_type",
    "hios_issuer_id", "hios_state", "hios_product_id",
    "plan_market_type", "state", "rate_file_url",
    "network_type", "is_ppo", "matched_token", "evidence_type",
    "confidence", "source_url", "quote",
]


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

class DiskCache:
    """Simple URL → file cache. Each entry is a JSON envelope."""

    def __init__(self, cache_dir: Path) -> None:
        self.dir = cache_dir
        self.dir.mkdir(parents=True, exist_ok=True)

    def _key(self, url: str) -> Path:
        safe = re.sub(r"[^\w\-.]", "_", url)[-180:]
        import hashlib
        h = hashlib.md5(url.encode()).hexdigest()[:8]
        return self.dir / f"{h}_{safe}.cache"

    def get(self, url: str) -> bytes | None:
        p = self._key(url)
        if p.exists():
            env = json.loads(p.read_bytes())
            if "error" in env:
                return None
            import base64
            return base64.b64decode(env["b64"])
        return None

    def set(self, url: str, data: bytes) -> None:
        import base64
        p = self._key(url)
        p.write_text(json.dumps({"b64": base64.b64encode(data).decode()}))

    def set_error(self, url: str, err: str) -> None:
        p = self._key(url)
        p.write_text(json.dumps({"error": err}))

    def has(self, url: str) -> bool:
        return self._key(url).exists()


# ---------------------------------------------------------------------------
# robots.txt checker
# ---------------------------------------------------------------------------

_robots_cache: dict[str, urllib.robotparser.RobotFileParser] = {}


def robots_allowed(url: str, user_agent: str = "*") -> bool:
    parsed = urllib.parse.urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    if base not in _robots_cache:
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(f"{base}/robots.txt")
        try:
            rp.read()
        except Exception:
            # If robots.txt is unreachable, assume allowed
            _robots_cache[base] = None
            return True
        _robots_cache[base] = rp
    rp = _robots_cache[base]
    if rp is None:
        return True
    return rp.can_fetch(user_agent, url)


# ---------------------------------------------------------------------------
# Polite fetcher
# ---------------------------------------------------------------------------

_last_fetch: dict[str, float] = defaultdict(float)


def polite_fetch(url: str, cache: DiskCache,
                 max_bytes: int | None = None) -> bytes | None:
    """Fetch URL with caching, robots.txt check and per-host delay."""
    cached = cache.get(url)
    if cached is not None:
        return cached

    if not robots_allowed(url):
        log(f"    robots.txt disallows: {url}")
        cache.set_error(url, "robots_disallowed")
        return None

    host = urllib.parse.urlparse(url).netloc
    elapsed = time.time() - _last_fetch[host]
    if elapsed < FETCH_DELAY_S:
        time.sleep(FETCH_DELAY_S - elapsed)

    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "hilabs-pipeline/1.0 (price-transparency-research)"},
        )
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as r:
            if max_bytes:
                data = r.read(max_bytes)
            else:
                data = r.read()
        _last_fetch[host] = time.time()
        cache.set(url, data)
        return data
    except Exception as e:
        log(f"    fetch error {url}: {e}")
        cache.set_error(url, str(e))
        _last_fetch[host] = time.time()
        return None


# ---------------------------------------------------------------------------
# CMS Plan Attributes source
# ---------------------------------------------------------------------------

def detect_col(header: list[str], candidates: list[str]) -> str | None:
    header_set = {h.strip(): h.strip() for h in header}
    for c in candidates:
        if c in header_set:
            return c
    # Case-insensitive fallback
    lower = {h.lower(): h for h in header}
    for c in candidates:
        if c.lower() in lower:
            return lower[c.lower()]
    return None


def load_cms_plan_attributes(path: str) -> dict[str, str]:
    """
    Load CMS Plan Attributes CSV. Returns dict: hios_plan_id_prefix -> plan_type.
    We use the first 10 characters of the HIOS plan ID (issuer+state+product)
    as the join key since the rate file only has product-level IDs.
    """
    if not os.path.exists(path):
        log(f"  CMS Plan Attributes not found: {path}")
        return {}

    log(f"  loading CMS Plan Attributes from {path}")
    result: dict[str, str] = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames or []
        id_col   = detect_col(header, CMS_PLAN_ID_CANDIDATES)
        type_col = detect_col(header, CMS_PLAN_TYPE_CANDIDATES)
        year_col = detect_col(header, CMS_PLAN_YEAR_CANDIDATES)

        if not id_col or not type_col:
            log(f"  CMS file columns: {header}")
            log(f"  Could not detect id_col={id_col} or type_col={type_col}. "
                f"Pass actual column names via --cms-id-col and --cms-type-col.")
            return {}

        for row in reader:
            year = int(row.get(year_col, 0) or 0) if year_col else 0
            if year_col and year and year != PLAN_YEAR:
                continue
            plan_id = str(row.get(id_col, "")).strip()
            plan_type = str(row.get(type_col, "")).strip()
            if len(plan_id) >= 10:
                result[plan_id[:10]] = plan_type

    log(f"  loaded {len(result)} CMS plan type entries")
    return result


def lookup_cms(row: dict, cms_index: dict[str, str]) -> dict | None:
    """Try to resolve network type from CMS Plan Attributes."""
    hios_product = row.get("hios_product_id", "").strip()
    if not hios_product or not cms_index:
        return None
    plan_type = cms_index.get(hios_product[:10])
    if not plan_type:
        return None
    net, is_ppo = normalise_plan_type(plan_type)
    return {
        "network_type": net,
        "is_ppo": "" if is_ppo is None else str(is_ppo).lower(),
        "evidence_type": "cms_plan_attributes",
        "source_url": "cms_plan_attributes",
        "quote": f"PlanType={plan_type}",
        "confidence": "high",
    }


def normalise_plan_type(raw: str) -> tuple[str, bool | None]:
    t = raw.strip().upper()
    if t in ("PPO", "PREFERRED PROVIDER ORGANIZATION"):
        return "PPO", True
    if t in ("HMO", "HEALTH MAINTENANCE ORGANIZATION"):
        return "HMO", False
    if t in ("EPO", "EXCLUSIVE PROVIDER ORGANIZATION"):
        return "EPO", False
    if t in ("POS", "POINT OF SERVICE"):
        return "POS", False
    return t, None


# ---------------------------------------------------------------------------
# SBC document source
# ---------------------------------------------------------------------------

def build_sbc_url(row: dict, sbc_patterns: dict[str, str]) -> str | None:
    """
    Build the SBC PDF URL for a plan using the payer-specific URL pattern.
    Patterns are provided by the user in config (never invented here).
    Pattern variables: {hios_issuer_id}, {hios_product_id}, {plan_id}, {state}
    """
    brand = row.get("brand", "").lower()
    pattern = sbc_patterns.get(brand)
    if not pattern:
        return None
    try:
        url = pattern.format(
            hios_issuer_id=row.get("hios_issuer_id", ""),
            hios_product_id=row.get("hios_product_id", ""),
            plan_id=row.get("plan_id", ""),
            state=row.get("state", "").lower(),
        )
        return url
    except KeyError:
        return None


def extract_pdf_text(data: bytes) -> str:
    """Extract text from PDF bytes using pypdf."""
    try:
        import pypdf
        import io
        reader = pypdf.PdfReader(io.BytesIO(data))
        return "\n".join(p.extract_text() or "" for p in reader.pages[:10])
    except Exception as e:
        return ""


def check_plan_year(text: str) -> bool:
    """Return True if the document mentions the expected plan year."""
    years = {int(m.group(1)) for m in PLAN_YEAR_RE.finditer(text)}
    if not years:
        return True   # no year found, don't reject
    return PLAN_YEAR in years


def search_network_type(text: str) -> tuple[str | None, str | None]:
    """
    Search document text for network type evidence.
    Returns (network_type, verbatim_sentence) or (None, None).
    """
    sentences = re.split(r"(?<=[.!?])\s+|\n", text)
    for net, pat in NET_TYPE_PATTERNS:
        for sent in sentences:
            if pat.search(sent):
                quote = sent.strip()[:300]
                return net, quote
    return None, None


def lookup_sbc(row: dict, sbc_patterns: dict[str, str],
               cache: DiskCache) -> dict | None:
    """Try to resolve network type from the plan's SBC PDF."""
    url = build_sbc_url(row, sbc_patterns)
    if not url:
        return None

    data = polite_fetch(url, cache, max_bytes=MAX_PDF_BYTES)
    if data is None:
        return None

    # Detect if it's a PDF
    if not data[:4] == b"%PDF":
        log(f"    not a PDF: {url}")
        return None

    text = extract_pdf_text(data)
    if not text:
        return None

    if not check_plan_year(text):
        log(f"    wrong plan year in: {url}")
        return None

    net, quote = search_network_type(text)
    if net is None:
        return None

    is_ppo = net == "PPO"
    return {
        "network_type": net,
        "is_ppo": str(is_ppo).lower(),
        "evidence_type": "sbc_document",
        "source_url": url,
        "quote": quote or "",
        "confidence": "high" if quote else "medium",
    }


# ---------------------------------------------------------------------------
# Model fallback (stub)
# ---------------------------------------------------------------------------

def model_fallback(row: dict, model_fn=None) -> dict | None:
    """
    Stub. Only wired if the user supplies a model function.
    The model must return a verbatim quote checked against the document text.
    """
    if model_fn is None:
        return None
    raise NotImplementedError(
        "model_fallback is a stub. Wire a model function and implement quote verification."
    )


# ---------------------------------------------------------------------------
# Main resolution loop
# ---------------------------------------------------------------------------

def resolve_plan(row: dict, cms_index: dict, sbc_patterns: dict,
                 cache: DiskCache, no_fetch: bool) -> dict:
    """Try evidence sources in order. Return enriched row."""
    result = {
        "network_type": "unknown",
        "is_ppo": "",
        "evidence_type": "unknown",
        "source_url": "",
        "quote": "",
        "confidence": "unknown",
    }

    # Source 1: CMS Plan Attributes (no network needed)
    ev = lookup_cms(row, cms_index)
    if ev:
        result.update(ev)
        return {**row, **result}

    if no_fetch:
        return {**row, **result}

    # Source 2: SBC document
    ev = lookup_sbc(row, sbc_patterns, cache)
    if ev:
        result.update(ev)
        return {**row, **result}

    # Source 3: model fallback (stub)
    ev = model_fallback(row)
    if ev:
        result.update(ev)
        return {**row, **result}

    return {**row, **result}


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def write_csv(path: Path, rows: list[dict], cols: list[str] | None = None) -> None:
    if not rows:
        return
    cols = cols or list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--plans", required=True,
                    help="c2a_unresolved.csv (unknown-type plans)")
    ap.add_argument("--out-dir", default="output/c2_evidence")
    ap.add_argument("--cms-plan-attributes", default="",
                    help="Path to CMS Plan Attributes CSV (optional)")
    ap.add_argument("--cms-id-col", default="",
                    help="Override HIOS plan ID column name in CMS file")
    ap.add_argument("--cms-type-col", default="",
                    help="Override plan type column name in CMS file")
    ap.add_argument("--sbc-patterns", default="",
                    help="Path to JSON file mapping brand -> SBC URL pattern")
    ap.add_argument("--no-fetch", action="store_true",
                    help="Skip all network fetches (CMS file and robots.txt still used)")
    ap.add_argument("--cache-dir", default="",
                    help="Cache directory (default: <out-dir>/cache)")
    args = ap.parse_args(argv)

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    cache = DiskCache(Path(args.cache_dir) if args.cache_dir else out / "cache")

    # Override CMS column names if provided
    if args.cms_id_col:
        CMS_PLAN_ID_CANDIDATES.insert(0, args.cms_id_col)
    if args.cms_type_col:
        CMS_PLAN_TYPE_CANDIDATES.insert(0, args.cms_type_col)

    # Load CMS Plan Attributes
    cms_index: dict[str, str] = {}
    if args.cms_plan_attributes:
        cms_index = load_cms_plan_attributes(args.cms_plan_attributes)

    # Load SBC URL patterns
    sbc_patterns: dict[str, str] = {}
    if args.sbc_patterns and os.path.exists(args.sbc_patterns):
        with open(args.sbc_patterns, encoding="utf-8") as f:
            sbc_patterns = json.load(f)
        log(f"Loaded SBC URL patterns for: {list(sbc_patterns.keys())}")
    elif not args.no_fetch:
        log("No --sbc-patterns file provided. SBC document lookup will be skipped.")
        log("Ask the user for the SBC URL pattern per payer (see AGENT_CONTEXT Q9).")

    # Read unresolved plans
    with open(args.plans, newline="", encoding="utf-8") as f:
        plans = list(csv.DictReader(f))
    log(f"Resolving {len(plans)} unknown-type plans ...")

    resolved_rows = []
    counts = {"cms_plan_attributes": 0, "sbc_document": 0,
              "model_fallback": 0, "unknown": 0}

    for i, row in enumerate(plans):
        if i % 100 == 0:
            log(f"  {i}/{len(plans)} ...")
        enriched = resolve_plan(row, cms_index, sbc_patterns, cache, args.no_fetch)
        resolved_rows.append(enriched)
        counts[enriched.get("evidence_type", "unknown")] += 1

    # Separate still-unknown vs resolved
    still_unknown = [r for r in resolved_rows if r["network_type"] == "unknown"]
    resolved      = [r for r in resolved_rows if r["network_type"] != "unknown"]

    cols = list(resolved_rows[0].keys()) if resolved_rows else OUT_COLS
    # Ensure OUT_COLS are included
    for c in OUT_COLS:
        if c not in cols:
            cols.append(c)

    write_csv(out / "c2_evidence_resolved.csv",   resolved,      cols)
    write_csv(out / "c2_evidence_unresolved.csv",  still_unknown, cols)
    write_csv(out / "c2_evidence_all.csv",         resolved_rows, cols)

    # Needs-source list: new plan codes with no document mapping
    needs_source = sorted({
        r.get("matched_token", "")
        for r in still_unknown
        if r.get("matched_token") and r.get("evidence_type") == "unknown"
    })

    summary = {
        "run_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "input_plans": len(plans),
        "resolved": len(resolved),
        "still_unknown": len(still_unknown),
        "by_evidence_type": counts,
        "cms_plan_attributes_loaded": len(cms_index),
        "sbc_patterns_loaded": list(sbc_patterns.keys()),
        "no_fetch": args.no_fetch,
        "needs_source": needs_source,
        "open_questions": [
            "Q9: What is the SBC URL pattern per payer (ambetter, fidelis, healthnet, qualchoice)?",
            "Q10: May provider directories be scraped?",
            "Q14: Excel row limit handling for the final file?",
        ],
    }

    (out / "c2_evidence_summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    return summary


if __name__ == "__main__":
    main()
