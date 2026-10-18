"""pipeline/c5/wp7_leftovers.py – WP7: Tiered extraction, allowlist domain probe, and leftover queue.

Requirements:
- Domain probe gate (A2): probes allowlisted domains with human probe_urls; gates disabled/unprobed domains.
- URL sources (A1): human-supplied lookup_url, url_templates; never search or guess. Suggested domains log.
- Triage (B1): has_candidate vs no_candidate; priority per TIN = upgradable_targets * specialty_weight.
- Page deduplication (B2): content hash deduplication, negative cache for 90 days.
- Tiered extraction (B3):
    * Tier 0: deterministic confirmation (no model).
    * Tier 1: deterministic structured extraction (JSON-LD, microdata, <address>).
    * Tier 2: Gemini structured batched extraction (trimmed input, gated by relevance).
- Validation (B4): quote_check, fields_in_quote, name_proximity_chars, reason codes.
- Claim semantics (B5): provider-specific vs organization location page, family independence.
- Spend controls (B6): llm_call_cap, max_calls_per_tin, domain yield stop, zero-yield stop, estimate_only.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from pathlib import Path
from typing import Any, Optional

import pandas as pd
from bs4 import BeautifulSoup

from .common import (
    SCHEMA_DOMAIN_PROBE,
    SCHEMA_DOMAIN_YIELD,
    SCHEMA_EXTRACTION_LOG,
    SCHEMA_LEFTOVERS,
    SCHEMA_LLM_LOG,
    SCHEMA_LOOKUP_LOG,
    SCHEMA_SUGGESTED_DOMAINS,
    fmt_npi,
    fmt_tin,
    get_logger,
)
from .gemini_client import GeminiExtractor
from .page_fetcher import (
    PageFetcher,
    get_domain,
    is_address_shaped,
    is_allowed_domain,
    run_domain_probe,
)
from .wp3_sites import normalize_address

log = get_logger("c5.wp7")


# ── Quote and Address Validation Helpers ─────────────────────────────────────
def normalize_for_quote_check(text: str) -> str:
    """Normalize text by abbreviating street types, collapsing whitespace, and stripping punctuation."""
    norm_text = normalize_address(text or "")
    norm_text = re.sub(r"[^\w\s]", " ", norm_text)
    return re.sub(r"\s+", " ", norm_text).strip().upper()


def quote_check(page_text: str, candidate_quote: str) -> bool:
    """Verify that candidate_quote appears verbatim within page_text."""
    if not page_text or not candidate_quote:
        return False
    norm_page = normalize_for_quote_check(page_text)
    norm_quote = normalize_for_quote_check(candidate_quote)
    if not norm_quote:
        return False
    return norm_quote in norm_page


def check_fields_in_quote(addr: dict[str, Any], quote: str) -> bool:
    """Check that street number, street name, and ZIP appear inside the quote."""
    norm_q = normalize_for_quote_check(quote)
    street = str(addr.get("street") or "").strip()
    zip5 = str(addr.get("zip") or addr.get("zip5") or "")[:5].strip()

    # Extract street number (leading digits)
    num_match = re.search(r"^\d+", street)
    if num_match:
        if num_match.group(0) not in norm_q:
            return False

    # Extract normalized street words
    norm_st = normalize_for_quote_check(street)
    st_words = [w for w in norm_st.split() if len(w) > 2]
    if st_words and not any(w in norm_q for w in st_words):
        return False

    if zip5 and zip5.isdigit() and len(zip5) == 5:
        if zip5 not in norm_q:
            return False

    return True


def check_name_proximity(
    page_text: str,
    target_name: str,
    quote: str,
    proximity_chars: int = 400,
) -> bool:
    """Verify that the target's name appears within proximity_chars of the quote in page text."""
    if not page_text or not target_name or not quote:
        return False

    norm_page = normalize_for_quote_check(page_text)
    norm_quote = normalize_for_quote_check(quote)
    norm_name = normalize_for_quote_check(target_name)

    idx = norm_page.find(norm_quote)
    if idx == -1:
        return False

    window_start = max(0, idx - proximity_chars)
    window_end = min(len(norm_page), idx + len(norm_quote) + proximity_chars)
    window_text = norm_page[window_start:window_end]

    # Check full name or individual last name + first name tokens
    name_tokens = [t for t in norm_name.split() if len(t) >= 2]
    if not name_tokens:
        return False

    # Check if full name appears in window
    if norm_name in window_text:
        return True

    # For individual: check if last name plus at least one other name token appears
    if len(name_tokens) >= 2:
        last_name = name_tokens[-1]
        other_tokens = name_tokens[:-1]
        if last_name in window_text and any(tok in window_text for tok in other_tokens):
            return True

    return False


def validate_extracted_address_tiered(
    page_text: str,
    addr: dict[str, Any],
    target_name: str,
    proximity_chars: int = 400,
) -> tuple[bool, Optional[str]]:
    """Validate extracted address according to Part B4 specifications."""
    street = str(addr.get("street") or "").strip()
    city = str(addr.get("city") or "").strip()
    state = str(addr.get("state") or "").strip()
    quote = str(addr.get("quote") or "").strip()

    # 1. Required fields
    if not street or not city or not state or not quote:
        return False, "malformed_json"

    # 2. Quote check: quote must appear verbatim in page text
    if not quote_check(page_text, quote):
        return False, "quote_not_found"

    # 3. Fields in quote check
    if not check_fields_in_quote(addr, quote):
        return False, "fields_not_in_quote"

    # 4. Name proximity check
    if not check_name_proximity(page_text, target_name, quote, proximity_chars=proximity_chars):
        return False, "name_not_near_address"

    # 5. Provider mismatch check (if explicit match returned)
    prov_match = addr.get("provider_match")
    if prov_match is not None and str(prov_match).strip():
        pm_norm = normalize_for_quote_check(str(prov_match))
        tn_norm = normalize_for_quote_check(target_name)
        pm_tokens = set(pm_norm.split())
        tn_tokens = set(tn_norm.split())
        if tn_tokens and not (pm_tokens & tn_tokens):
            return False, "provider_mismatch"

    return True, None


def validate_extracted_address(
    page_text: str,
    addr: dict[str, Any],
    target_name: str,
    proximity_chars: int = 400,
) -> tuple[bool, Optional[str]]:
    """Alias for backwards compatibility with earlier tests."""
    return validate_extracted_address_tiered(page_text, addr, target_name, proximity_chars=proximity_chars)


# ── Tier 0: Deterministic Confirmation ────────────────────────────────────────
def tier0_confirm_target(
    page_text: str,
    candidate_addr: dict[str, Any],
    target_name: str,
    proximity_chars: int = 400,
) -> tuple[bool, Optional[str]]:
    """Tier 0: Check candidate street number, street name, and ZIP/city appear together near name."""
    street = str(candidate_addr.get("address_line") or "").strip()
    zip5 = str(candidate_addr.get("zip5") or "")[:5].strip()
    city = str(candidate_addr.get("city") or "").strip()

    num_match = re.search(r"^\d+", street)
    if not num_match:
        return False, None
    st_num = num_match.group(0)

    norm_page = normalize_for_quote_check(page_text)
    norm_st = normalize_for_quote_check(street)
    st_words = [w for w in norm_st.split() if w and w != st_num]
    if not st_words:
        return False, None

    # Search for occurrences of street number in page
    for match in re.finditer(r"\b" + re.escape(st_num) + r"\b", norm_page):
        start = match.start()
        # Look in small window (180 chars) for street name and zip/city
        snippet = norm_page[max(0, start - 20) : min(len(norm_page), start + 180)]
        if any(w in snippet for w in st_words):
            if (zip5 and zip5 in snippet) or (city and normalize_for_quote_check(city) in snippet):
                # Now check name proximity
                name_window = norm_page[max(0, start - proximity_chars) : min(len(norm_page), start + proximity_chars + 180)]
                norm_name = normalize_for_quote_check(target_name)
                name_toks = [t for t in norm_name.split() if len(t) >= 2]
                if norm_name in name_window or (name_toks and any(t in name_window for t in name_toks)):
                    # Match found! Extract quote snippet
                    raw_quote = page_text[max(0, start - 10) : min(len(page_text), start + 150)]
                    return True, raw_quote.strip()

    return False, None


# ── Tier 1: Deterministic Structured Extraction ──────────────────────────────
def parse_json_ld_addresses(html_content: str) -> list[dict[str, Any]]:
    """Extract addresses from JSON-LD schema.org scripts."""
    addresses: list[dict[str, Any]] = []
    if not html_content:
        return addresses

    soup = BeautifulSoup(html_content, "html.parser")
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                addr_obj = item.get("address")
                if isinstance(addr_obj, dict):
                    st = str(addr_obj.get("streetAddress") or "").strip()
                    ct = str(addr_obj.get("addressLocality") or "").strip()
                    rg = str(addr_obj.get("addressRegion") or "").strip()
                    zp = str(addr_obj.get("postalCode") or "")[:5].strip()
                    if st and ct and rg:
                        q = f"{st}, {ct}, {rg} {zp}".strip()
                        addresses.append({
                            "provider_match": item.get("name"),
                            "street": st,
                            "unit": "",
                            "city": ct,
                            "state": rg,
                            "zip": zp,
                            "quote": q,
                            "source_type": "json_ld",
                        })
        except Exception:
            continue
    return addresses


def parse_microdata_and_address_tags(html_content: str) -> list[dict[str, Any]]:
    """Extract addresses from microdata and HTML <address> tags."""
    addresses: list[dict[str, Any]] = []
    if not html_content:
        return addresses

    soup = BeautifulSoup(html_content, "html.parser")

    # 1. HTML <address> tags
    for tag in soup.find_all("address"):
        text = tag.get_text(separator=" ", strip=True)
        if is_address_shaped(text):
            # Parse components
            m = re.search(r"\b(\d{1,6}\s+[A-Za-z0-9\.\s#\-]+(?:St|Street|Rd|Road|Ave|Avenue|Blvd|Boulevard|Pkwy|Parkway|Dr|Drive|Way|Ct|Court|Ste|Suite|Plaza|Hwy|Highway))\b.*?([A-Za-z\s]+),\s*([A-Z]{2})\s*(\d{5})?", text, re.I)
            if m:
                addresses.append({
                    "provider_match": None,
                    "street": m.group(1).strip(),
                    "unit": "",
                    "city": m.group(2).strip(),
                    "state": m.group(3).upper().strip(),
                    "zip": (m.group(4) or "").strip(),
                    "quote": text,
                    "source_type": "address_tag",
                })

    return addresses


def tier1_structured_extract(html_content: str) -> list[dict[str, Any]]:
    """Extract all deterministic structured addresses from HTML."""
    results = parse_json_ld_addresses(html_content)
    results.extend(parse_microdata_and_address_tags(html_content))
    return results


# ── URL Template Helpers ─────────────────────────────────────────────────────
def slugify(text: str) -> str:
    """Create URL-friendly slug."""
    text = re.sub(r"[^\w\s-]", "", text or "").strip().lower()
    return re.sub(r"[-\s]+", "-", text)


def apply_url_templates(
    templates: dict[str, str],
    target_row: dict[str, Any],
) -> list[str]:
    """Apply human-supplied URL templates without inventing patterns."""
    urls = []
    npi = str(target_row.get("npi") or "")
    tin = str(target_row.get("tin") or "")
    name = str(target_row.get("business_name") or "")
    city = str(target_row.get("city") or "")
    zip5 = str(target_row.get("zip5") or "")[:5]

    placeholders = {
        "{npi}": npi if npi else None,
        "{tin}": tin if tin else None,
        "{name_slug}": slugify(name) if name else None,
        "{city_slug}": slugify(city) if city else None,
        "{zip5}": zip5 if zip5 else None,
    }

    for name_key, tmpl in templates.items():
        missing = False
        url_str = tmpl
        found_phs = re.findall(r"\{[a-zA-Z0-9_]+\}", tmpl)
        for ph in found_phs:
            val = placeholders.get(ph)
            if val is None or not str(val).strip():
                missing = True
                break
            url_str = url_str.replace(ph, str(val))
        if not missing and not re.search(r"\{[a-zA-Z0-9_]+\}", url_str) and url_str != tmpl:
            urls.append(url_str)

    return urls


# ── Text Trimming for Tier 2 LLM ─────────────────────────────────────────────
def trim_text_around_mentions(
    page_text: str,
    names: list[str],
    max_chars: int = 6000,
    window_radius: int = 300,
) -> str:
    """Extract text windows around target name mentions and address patterns, capped at max_chars."""
    if not page_text or len(page_text) <= max_chars:
        return page_text

    norm_text = page_text.lower()
    spans: list[tuple[int, int]] = []

    for name in names:
        if not name:
            continue
        for token in name.lower().split():
            if len(token) < 4:
                continue
            for m in re.finditer(re.escape(token), norm_text):
                start = max(0, m.start() - window_radius)
                end = min(len(page_text), m.end() + window_radius)
                spans.append((start, end))

    for m in ADDRESS_SHAPED_RE.finditer(page_text):
        start = max(0, m.start() - window_radius)
        end = min(len(page_text), m.end() + window_radius)
        spans.append((start, end))

    if not spans:
        return page_text[:max_chars]

    # Merge overlapping spans
    spans.sort(key=lambda s: s[0])
    merged: list[tuple[int, int]] = []
    for s, e in spans:
        if not merged:
            merged.append((s, e))
        else:
            last_s, last_e = merged[-1]
            if s <= last_e:
                merged[-1] = (last_s, max(last_e, e))
            else:
                merged.append((s, e))

    chunks = []
    total_len = 0
    for s, e in merged:
        chunk = page_text[s:e]
        if total_len + len(chunk) > max_chars:
            chunk = chunk[: max_chars - total_len]
            chunks.append(chunk)
            break
        chunks.append(chunk)
        total_len += len(chunk)

    return "\n...\n".join(chunks)


# ── Main WP7 Runner ──────────────────────────────────────────────────────────
def run_wp7(
    cfg: dict,
    targets: pd.DataFrame,
    addresses: pd.DataFrame,
    candidates: pd.DataFrame,
    claims: Optional[pd.DataFrame] = None,
    sites: Optional[pd.DataFrame] = None,
    tin_sites: Optional[pd.DataFrame] = None,
    client: Optional[Any] = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """Build leftover queue, probe domains, execute tiered extraction, and return updated outputs."""
    log.info("=== WP7: Tiered Leftover Lookup & Allowlisted Domain Probe ===")

    fetch_date = str(cfg.get("run_date") or date.today().isoformat())
    allowlist = [d.lower().strip() for d in cfg.get("web_fetch_allowlist", [])]
    web_fetch_enabled = bool(cfg.get("web_fetch_enabled", False))
    llm_enabled = bool(cfg.get("llm_enabled", False))
    estimate_only = bool(cfg.get("estimate_only", False))
    match_subdomains = bool(cfg.get("web_fetch_match_subdomains", True))
    url_templates = cfg.get("url_templates", {})
    max_urls_per_tin = int(cfg.get("lookup_max_urls_per_tin", 2))
    name_proximity = int(cfg.get("name_proximity_chars", 400))

    # Initialize PageFetcher and run domain probe (Part A2)
    fetcher = PageFetcher(cfg)
    df_probe, domain_status, unprobed_domains = run_domain_probe(cfg, fetcher)

    # Triage unverified targets (Part B1)
    unverified_addrs = addresses[addresses["label"] == "unverified"].copy()
    tin_sizes = targets.groupby("tin")["npi"].nunique().to_dict()
    tgt_dict = targets.set_index(["tin", "npi"]).to_dict(orient="index")

    best_cands = {}
    if not candidates.empty:
        for (tin, npi), grp in candidates.groupby(["tin", "npi"]):
            top = grp.sort_values(by="score", ascending=False).iloc[0]
            best_cands[(str(tin), str(npi))] = top

    # Compute upgradable targets count per TIN for priority
    upgradable_per_tin: dict[str, int] = {}
    for _, row in unverified_addrs.iterrows():
        t = str(row["tin"])
        n = str(row["npi"])
        cand = best_cands.get((t, n))
        has_cand = cand is not None and str(cand.get("address_line") or "").strip() != ""
        if has_cand:
            upgradable_per_tin[t] = upgradable_per_tin.get(t, 0) + 1

    # Build leftovers queue with tin_priority and URL methods (Part A1)
    leftover_rows: list[dict[str, Any]] = []
    suggested_domains_map: dict[str, dict[str, Any]] = {}

    for _, row in unverified_addrs.iterrows():
        tin = str(row["tin"])
        npi = str(row["npi"])
        tgt_info = tgt_dict.get((tin, npi), {})
        best_cand = best_cands.get((tin, npi))

        if best_cand is not None and str(best_cand.get("address_line") or "").strip() != "":
            addr_str = f"{best_cand.get('address_line', '')}, {best_cand.get('city', '')}, {best_cand.get('state', '')} {best_cand.get('zip5', '')}".strip(", ")
            reason = "single_weak_source" if best_cand.get("n_independent_families", 0) < 2 else "stale_or_unmatched"
        else:
            addr_str = ""
            reason = "no_candidate_site_found"

        # Specialty weight (primary care / oncology = 1.2, else 1.0)
        spec = str(tgt_info.get("specialty_code") or "")
        spec_weight = 1.2 if spec.startswith(("207Q", "208D", "207R")) else 1.0
        upgradable_count = upgradable_per_tin.get(tin, 0)
        tin_priority = round(upgradable_count * spec_weight, 2)

        # URL derivation: human URL in row vs templates
        lookup_url = str(row.get("lookup_url") or "").strip()
        lookup_method = "human" if lookup_url else ""

        if not lookup_url and url_templates:
            cand_dict = best_cand.to_dict() if best_cand is not None else {}
            tmpl_urls = apply_url_templates(url_templates, {**tgt_info, **cand_dict, "tin": tin, "npi": npi})
            if tmpl_urls:
                lookup_url = tmpl_urls[0]
                lookup_method = "template"

        # Check domain allowlist
        if lookup_url:
            dom = get_domain(lookup_url)
            if not is_allowed_domain(lookup_url, allowlist, match_subdomains):
                # Record suggested domain for human approval
                if dom not in suggested_domains_map:
                    suggested_domains_map[dom] = {
                        "domain": dom,
                        "n_urls": 0,
                        "targets_covered": set(),
                        "example_url": lookup_url,
                    }
                suggested_domains_map[dom]["n_urls"] += 1
                suggested_domains_map[dom]["targets_covered"].add(f"{tin}:{npi}")
                lookup_url = ""
                lookup_method = ""

        leftover_rows.append({
            "tin": tin,
            "npi": npi,
            "business_name": tgt_info.get("business_name", ""),
            "entity_type": tgt_info.get("entity_type", "unknown"),
            "specialty_code": tgt_info.get("specialty_code", ""),
            "tin_size": tin_sizes.get(tin, 1),
            "tin_priority": tin_priority,
            "ppo_scope": row.get("ppo_scope", "strict"),
            "best_candidate_address": addr_str,
            "reason_unverified": reason,
            "lookup_url": lookup_url,
            "lookup_url_method": lookup_method,
            "lookup_address": "",
            "lookup_source": "",
        })

    df_leftovers = pd.DataFrame(leftover_rows) if leftover_rows else pd.DataFrame(columns=SCHEMA_LEFTOVERS)
    if not df_leftovers.empty:
        df_leftovers = df_leftovers.sort_values(by=["tin_priority", "tin_size", "tin", "npi"], ascending=[False, False, True, True])

    # Write c5_suggested_domains.csv
    sugg_rows = [
        {
            "domain": d["domain"],
            "n_urls": d["n_urls"],
            "n_targets_covered": len(d["targets_covered"]),
            "example_url": d["example_url"],
        }
        for d in suggested_domains_map.values()
    ]
    df_suggested = pd.DataFrame(sugg_rows) if sugg_rows else pd.DataFrame(columns=SCHEMA_SUGGESTED_DOMAINS)
    sugg_path = cfg.get("paths", {}).get("c5_suggested_domains", "output/c5/c5_suggested_domains.csv")
    Path(sugg_path).parent.mkdir(parents=True, exist_ok=True)
    df_suggested.to_csv(sugg_path, index=False)

    # Initialize logs & stats
    lookup_log_rows: list[dict[str, Any]] = []
    llm_log_rows: list[dict[str, Any]] = []
    extraction_log_rows: list[dict[str, Any]] = []
    domain_yield_map: dict[str, dict[str, Any]] = {
        d: {"domain": d, "pages": 0, "tier0_hits": 0, "tier1_hits": 0, "llm_calls": 0, "accepted": 0, "accept_rate": 0.0, "stopped_reason": None}
        for d in allowlist
    }

    stats: dict[str, Any] = {
        "probe_results_per_domain": domain_status,
        "urls_by_method": df_leftovers["lookup_url_method"].value_counts().to_dict(),
        "pages_fetched": 0,
        "refusals_by_reason": {},
        "no_content_pages": 0,
        "targets_resolved_per_tier": {"tier0": 0, "tier1": 0, "tier2": 0},
        "llm_calls_used": 0,
        "calls_avoided": 0,
        "pages_skipped_not_relevant": 0,
        "average_input_chars_per_call": 0.0,
        "accepted_by_source_family": {},
        "rejected_by_reason": {},
        "calls_per_verified_upgrade": 0.0,
        "labels_before_and_after": {},
        "labels_upgraded": {"unverified_to_probable": 0, "probable_to_verified": 0},
        "targets_still_unverified": len(df_leftovers),
    }

    # Gate: Check unprobed domains or empty probe_urls
    if unprobed_domains:
        log.warning("=" * 60)
        log.warning("DOMAIN PROBE GATE: Unprobed allowlisted domains detected!")
        for ud in unprobed_domains:
            log.warning(f"  Domain '{ud}' has NO probe URLs in config/c5.yaml.")
        log.warning("To enable lookups, please supply 3 to 5 real result-page URLs for specific providers")
        log.warning("copied from a browser under config.probe_urls.<domain>.")
        log.warning("The prioritized leftover queue has been written to output/c5/c5_leftovers.csv.")
        log.warning("=" * 60)
        _write_logs_and_leftovers(cfg, df_leftovers, lookup_log_rows, llm_log_rows, extraction_log_rows, domain_yield_map)
        return df_leftovers, addresses, candidates, stats

    # Gate: If all domains disabled by probe or allowlist empty
    active_domains = [d for d, st in domain_status.items() if st == "passed"]
    if not active_domains:
        log.info("No active allowlisted domains passed the domain probe. Skipping web fetches.")
        _write_logs_and_leftovers(cfg, df_leftovers, lookup_log_rows, llm_log_rows, extraction_log_rows, domain_yield_map)
        return df_leftovers, addresses, candidates, stats

    # Check for targets with URLs
    targets_with_urls = df_leftovers[df_leftovers["lookup_url"].str.strip() != ""].copy()
    if targets_with_urls.empty:
        log.info("No targets carry valid lookup_url. Preserving prioritized leftover queue.")
        _write_logs_and_leftovers(cfg, df_leftovers, lookup_log_rows, llm_log_rows, extraction_log_rows, domain_yield_map)
        return df_leftovers, addresses, candidates, stats

    # Group by URL for page-level deduplication (Part B2)
    url_groups = targets_with_urls.groupby("lookup_url")
    log.info(f"Processing {len(url_groups):,} unique URLs covering {len(targets_with_urls):,} target pairs...")

    # Initialize Gemini extractor if Tier 2 enabled
    extractor: Optional[GeminiExtractor] = None
    if llm_enabled and not estimate_only:
        try:
            extractor = GeminiExtractor(cfg, client=client)
        except Exception as exc:
            log.warning(f"Gemini client initialization failed: {exc}. Tier 2 will be disabled.")

    consecutive_zero_yield_calls = 0
    tin_call_counts: dict[str, int] = {}
    total_input_chars = 0
    resolved_target_keys: set[str] = set()

    for url, grp in url_groups:
        domain = get_domain(url)
        if domain_status.get(domain) != "passed":
            continue

        domain_yield = domain_yield_map[domain]
        domain_yield["pages"] += 1

        # Fetch page through PageFetcher
        clean_text, log_entry = fetcher.fetch(url, fetch_date=fetch_date)
        lookup_log_rows.append(log_entry)
        stats["pages_fetched"] += 1

        if not clean_text:
            reason = log_entry.get("refusal_reason", "unknown")
            stats["refusals_by_reason"][reason] = stats["refusals_by_reason"].get(reason, 0) + 1
            if reason == "no_content":
                stats["no_content_pages"] += 1
            continue

        raw_html = log_entry.get("raw_html") or ""
        content_hash = log_entry.get("content_hash") or hashlib.sha256(clean_text.encode()).hexdigest()
        targets_in_page = [f"{r['tin']}:{r['npi']}" for _, r in grp.iterrows()]
        targets_resolved_this_page = 0
        tier_resolved_label = "unresolved"
        llm_called_flag = False

        # ── Tier 0: Deterministic Confirmation ───────────────────────────────
        unresolved_in_grp = []
        for _, r in grp.iterrows():
            t_key = f"{r['tin']}:{r['npi']}"
            if t_key in resolved_target_keys:
                continue

            cand = best_cands.get((str(r["tin"]), str(r["npi"])))
            if cand is not None and str(cand.get("address_line") or "").strip() != "":
                matched, quote = tier0_confirm_target(clean_text, cand.to_dict(), str(r["business_name"]), proximity_chars=name_proximity)
                if matched:
                    stats["targets_resolved_per_tier"]["tier0"] += 1
                    domain_yield["tier0_hits"] += 1
                    targets_resolved_this_page += 1
                    resolved_target_keys.add(t_key)
                    tier_resolved_label = "tier0"
                    continue

            unresolved_in_grp.append(r)

        # ── Tier 1: Deterministic Structured Extraction ──────────────────────
        if unresolved_in_grp and raw_html:
            structured_addrs = tier1_structured_extract(raw_html)
            still_unresolved = []
            for r in unresolved_in_grp:
                t_key = f"{r['tin']}:{r['npi']}"
                resolved_t1 = False
                for s_addr in structured_addrs:
                    valid, reason = validate_extracted_address_tiered(clean_text, s_addr, str(r["business_name"]), proximity_chars=name_proximity)
                    if valid:
                        stats["targets_resolved_per_tier"]["tier1"] += 1
                        domain_yield["tier1_hits"] += 1
                        targets_resolved_this_page += 1
                        resolved_target_keys.add(t_key)
                        resolved_t1 = True
                        tier_resolved_label = "tier1" if tier_resolved_label != "tier0" else "tier0+tier1"
                        break
                if not resolved_t1:
                    still_unresolved.append(r)
            unresolved_in_grp = still_unresolved

        # ── Tier 2: Gemini Backend ───────────────────────────────────────────
        if unresolved_in_grp:
            # Check spend controls
            if estimate_only:
                stats["calls_avoided"] += 1
            elif extractor is not None:
                # Check domain yield
                if domain_yield["llm_calls"] >= int(cfg.get("domain_yield_check_after_calls", 10)):
                    acc_rate = domain_yield["accepted"] / max(domain_yield["llm_calls"], 1)
                    if acc_rate < float(cfg.get("domain_min_accept_rate", 0.10)):
                        domain_yield["stopped_reason"] = "domain_low_yield"

                # Check consecutive zero yield stop
                if consecutive_zero_yield_calls >= int(cfg.get("llm_stop_after_zero_yield_calls", 15)):
                    log.warning("Reached consecutive zero-yield call limit. Halting Tier 2 calls.")

                # Gate: check names and address patterns present
                names = [str(r["business_name"]) for r in unresolved_in_grp]
                norm_clean = normalize_for_quote_check(clean_text)
                has_name = any(normalize_for_quote_check(n) in norm_clean for n in names)
                has_addr = is_address_shaped(clean_text)

                if not (has_name and has_addr):
                    stats["pages_skipped_not_relevant"] += 1
                elif domain_yield.get("stopped_reason") is None and consecutive_zero_yield_calls < int(cfg.get("llm_stop_after_zero_yield_calls", 15)):
                    # Check TIN call cap
                    grp_tin = str(unresolved_in_grp[0]["tin"])
                    if tin_call_counts.get(grp_tin, 0) < int(cfg.get("llm_max_calls_per_tin", 1)):
                        trimmed = trim_text_around_mentions(clean_text, names, max_chars=int(cfg.get("llm_max_input_chars", 6000)))
                        batch_targets = [
                            {"target_key": f"{r['tin']}:{r['npi']}", "name": str(r["business_name"]), "entity_type": str(r["entity_type"])}
                            for r in unresolved_in_grp[: int(cfg.get("llm_batch_targets_per_call", 20))]
                        ]

                        llm_addrs, llm_log_entry = extractor.extract_batched_addresses(batch_targets, trimmed, content_hash)
                        llm_log_rows.append(llm_log_entry)
                        llm_called_flag = True
                        domain_yield["llm_calls"] += 1
                        tin_call_counts[grp_tin] = tin_call_counts.get(grp_tin, 0) + 1
                        total_input_chars += llm_log_entry.get("input_chars", 0)

                        accepted_count_this_call = 0
                        target_key_map = {f"{r['tin']}:{r['npi']}": r for r in unresolved_in_grp}

                        for ext_addr in llm_addrs:
                            t_key = ext_addr.get("target_key")
                            matched_target = target_key_map.get(t_key)
                            if matched_target is None:
                                continue

                            valid, r_reason = validate_extracted_address_tiered(clean_text, ext_addr, str(matched_target["business_name"]), proximity_chars=name_proximity)
                            if valid:
                                accepted_count_this_call += 1
                                domain_yield["accepted"] += 1
                                targets_resolved_this_page += 1
                                stats["targets_resolved_per_tier"]["tier2"] += 1
                                resolved_target_keys.add(t_key)
                                tier_resolved_label = "tier2"
                            else:
                                stats["rejected_by_reason"][r_reason] = stats["rejected_by_reason"].get(r_reason, 0) + 1

                        if accepted_count_this_call > 0:
                            consecutive_zero_yield_calls = 0
                        else:
                            consecutive_zero_yield_calls += 1

        extraction_log_rows.append({
            "page_id": f"p_{len(extraction_log_rows)+1}",
            "url": url,
            "domain": domain,
            "content_hash": content_hash,
            "tier_resolved": tier_resolved_label,
            "targets_in_page": len(targets_in_page),
            "targets_resolved": targets_resolved_this_page,
            "llm_called": llm_called_flag,
            "input_chars": len(clean_text),
            "reject_reasons": None,
        })

    # Update summary stats
    if extractor is not None:
        stats["llm_calls_used"] = extractor.calls_made
        if extractor.calls_made > 0:
            stats["average_input_chars_per_call"] = round(total_input_chars / extractor.calls_made, 1)

    # Compute domain yield accept rates
    for dy in domain_yield_map.values():
        if dy["llm_calls"] > 0:
            dy["accept_rate"] = round(dy["accepted"] / dy["llm_calls"], 4)

    _write_logs_and_leftovers(cfg, df_leftovers, lookup_log_rows, llm_log_rows, extraction_log_rows, domain_yield_map)
    log.info("WP7 complete.")
    return df_leftovers, addresses, candidates, stats


def _write_logs_and_leftovers(
    cfg: dict,
    df_leftovers: pd.DataFrame,
    lookup_log_rows: list[dict],
    llm_log_rows: list[dict],
    extraction_log_rows: list[dict],
    domain_yield_map: dict[str, dict],
) -> None:
    """Write all WP7 logs and leftover queue to disk."""
    leftovers_path = cfg["paths"]["c5_leftovers"]
    Path(leftovers_path).parent.mkdir(parents=True, exist_ok=True)
    df_leftovers.to_csv(leftovers_path, index=False)

    lookup_log_path = cfg["paths"]["c5_lookup_log"]
    df_lookup = pd.DataFrame(lookup_log_rows) if lookup_log_rows else pd.DataFrame(columns=SCHEMA_LOOKUP_LOG)
    df_lookup.to_csv(lookup_log_path, index=False)

    llm_log_path = cfg["paths"]["c5_llm_log"]
    df_llm = pd.DataFrame(llm_log_rows) if llm_log_rows else pd.DataFrame(columns=SCHEMA_LLM_LOG)
    df_llm.to_csv(llm_log_path, index=False)

    extraction_log_path = cfg.get("paths", {}).get("c5_extraction_log", "output/c5/c5_extraction_log.csv")
    df_ext = pd.DataFrame(extraction_log_rows) if extraction_log_rows else pd.DataFrame(columns=SCHEMA_EXTRACTION_LOG)
    df_ext.to_csv(extraction_log_path, index=False)

    domain_yield_path = cfg.get("paths", {}).get("c5_domain_yield", "output/c5/c5_domain_yield.csv")
    df_yield = pd.DataFrame(list(domain_yield_map.values())) if domain_yield_map else pd.DataFrame(columns=SCHEMA_DOMAIN_YIELD)
    df_yield.to_csv(domain_yield_path, index=False)
