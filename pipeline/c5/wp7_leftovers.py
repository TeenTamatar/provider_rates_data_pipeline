"""pipeline/c5/wp7_leftovers.py – WP7: Unverified leftover queue, allowlisted web fetcher, and Gemini extraction.

Requirements:
- Generates c5_leftovers.csv containing all unverified targets ordered by tin_size / leverage.
- If web_fetch_allowlist is empty: logs message that human must add domains, writes leftover queue as baseline.
- If allowlist has domains: executes capped lookup loop (prioritizing large TINs, respecting llm_call_cap).
- Uses PageFetcher for static allowlisted HTML and robots.txt compliance.
- Uses GeminiExtractor for structured JSON address extraction (temp 0).
- Applies deterministic quote_check: rejects quotes not found verbatim in page text.
- Generates audit logs: c5_lookup_log.csv and c5_llm_log.csv.
- Accepted lookups become claims; re-evaluates sites, candidates, and labels for affected targets.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from .common import (
    SCHEMA_CLAIMS,
    SCHEMA_LEFTOVERS,
    SCHEMA_LLM_LOG,
    SCHEMA_LOOKUP_LOG,
    count_independent_families,
    get_logger,
    is_po_box,
)
from .gemini_client import GeminiExtractor
from .page_fetcher import PageFetcher, get_domain
from .wp3_sites import normalize_address, run_wp3
from .wp5_candidates import run_wp5
from .wp6_choose import run_wp6

log = get_logger("c5.wp7")


def normalize_for_quote_check(text: str) -> str:
    """Normalize text by abbreviating street types, collapsing whitespace, and stripping punctuation."""
    norm_text = normalize_address(text or "")
    norm_text = re.sub(r"[^\w\s]", " ", norm_text)
    return re.sub(r"\s+", " ", norm_text).strip().upper()


def quote_check(page_text: str, candidate_quote: str) -> bool:
    """Verify that candidate_quote appears verbatim within page_text.

    Returns True only if the normalized quote tokens appear as a contiguous
    subsequence within the normalized page text.
    """
    if not page_text or not candidate_quote:
        return False
    norm_page = normalize_for_quote_check(page_text)
    norm_quote = normalize_for_quote_check(candidate_quote)
    if not norm_quote:
        return False
    return norm_quote in norm_page


def validate_extracted_address(
    page_text: str,
    addr: dict[str, Any],
    target_name: str,
) -> tuple[bool, Optional[str]]:
    """Validate an extracted address candidate against page text and provider name.

    Returns:
        is_valid: bool
        rejection_reason: None or 'quote_not_found', 'provider_mismatch', 'malformed_json'
    """
    # 1. Check required fields
    street = str(addr.get("street") or "").strip()
    city = str(addr.get("city") or "").strip()
    state = str(addr.get("state") or "").strip()
    quote = str(addr.get("quote") or "").strip()

    if not street or not city or not state or not quote:
        return False, "malformed_json"

    # 2. Quote check: quote must appear verbatim in page text
    if not quote_check(page_text, quote):
        return False, "quote_not_found"

    # 3. Provider match check
    prov_match = addr.get("provider_match")
    if prov_match is not None and str(prov_match).strip():
        pm_norm = normalize_for_quote_check(str(prov_match))
        tn_norm = normalize_for_quote_check(target_name)
        # Verify provider match appears in page and has token overlap with target
        if pm_norm not in normalize_for_quote_check(page_text):
            return False, "provider_mismatch"
        pm_tokens = set(pm_norm.split())
        tn_tokens = set(tn_norm.split())
        if tn_tokens and not (pm_tokens & tn_tokens):
            return False, "provider_mismatch"

    return True, None


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
    """Build leftover queue, execute capped lookup loop if allowlisted, and return updated outputs."""
    log.info("=== WP7: Leftover queue and allowlisted web lookup ===")

    fetch_date = str(cfg.get("run_date") or date.today().isoformat())
    allowlist = cfg.get("web_fetch_allowlist", [])
    llm_enabled = bool(cfg.get("llm_enabled", False))
    web_fetch_enabled = bool(cfg.get("web_fetch_enabled", False))

    unverified_addrs = addresses[addresses["label"] == "unverified"].copy()
    tin_sizes = targets.groupby("tin")["npi"].nunique().to_dict()
    tgt_dict = targets.set_index(["tin", "npi"]).to_dict(orient="index")

    best_cands = {}
    if not candidates.empty:
        for (tin, npi), grp in candidates.groupby(["tin", "npi"]):
            top = grp.sort_values(by="score", ascending=False).iloc[0]
            best_cands[(str(tin), str(npi))] = top

    rows: list[dict[str, Any]] = []
    for _, row in unverified_addrs.iterrows():
        tin = str(row["tin"])
        npi = str(row["npi"])
        tgt_info = tgt_dict.get((tin, npi), {})
        best_cand = best_cands.get((tin, npi))
        if best_cand is not None:
            addr_str = f"{best_cand.get('address_line', '')}, {best_cand.get('city', '')}, {best_cand.get('state', '')} {best_cand.get('zip5', '')}".strip(", ")
            reason = "single_weak_source" if best_cand.get("n_independent_families", 0) < 2 else "stale_or_unmatched"
        else:
            addr_str = ""
            reason = "no_candidate_site_found"

        rows.append({
            "tin": tin,
            "npi": npi,
            "business_name": tgt_info.get("business_name", ""),
            "entity_type": tgt_info.get("entity_type", "unknown"),
            "specialty_code": tgt_info.get("specialty_code", ""),
            "tin_size": tin_sizes.get(tin, 1),
            "ppo_scope": row.get("ppo_scope", "inclusive_only"),
            "best_candidate_address": addr_str,
            "reason_unverified": reason,
            "lookup_url": "",
            "lookup_address": "",
            "lookup_source": "",
        })

    df_leftovers = pd.DataFrame(rows) if rows else pd.DataFrame(columns=SCHEMA_LEFTOVERS)
    if not df_leftovers.empty:
        df_leftovers = df_leftovers.sort_values(by=["tin_size", "tin", "npi"], ascending=[False, True, True])

    # Initialize lookup and LLM logs
    lookup_log_rows: list[dict[str, Any]] = []
    llm_log_rows: list[dict[str, Any]] = []

    stats: dict[str, Any] = {
        "pages_fetched": 0,
        "refusals_by_reason": {},
        "calls_used": 0,
        "calls_accepted": 0,
        "rejected_by_reason": {},
        "accepted_by_source_family": {},
        "labels_upgraded": {"unverified_to_probable": 0, "probable_to_verified": 0},
    }

    # Case 1: Allowlist is empty
    if not allowlist:
        log.info("  web_fetch_allowlist is empty. Human must add allowlisted domains to config/c5.yaml to perform lookups.")
        log.info(f"  Leftover queue preserved: {len(df_leftovers):,} unverified targets.")
        _write_logs_and_leftovers(cfg, df_leftovers, lookup_log_rows, llm_log_rows)
        return df_leftovers, addresses, candidates, stats

    # Case 2: Allowlist is populated and LLM is enabled
    log.info(f"  Allowlist active: {allowlist}. Running capped lookup step...")
    fetcher = PageFetcher(cfg)
    try:
        extractor = GeminiExtractor(cfg, client=client)
    except Exception as exc:
        log.error(f"Cannot initialize GeminiExtractor: {exc}")
        _write_logs_and_leftovers(cfg, df_leftovers, lookup_log_rows, llm_log_rows)
        return df_leftovers, addresses, candidates, stats

    # Collect lookup candidates from targets or pre-filled leftovers
    new_claims_rows: list[dict[str, Any]] = []
    affected_npis: set[str] = set()

    for idx, row in df_leftovers.iterrows():
        if extractor.calls_made >= extractor.call_cap:
            log.info("  Reached LLM call cap limit.")
            break

        url = str(row.get("lookup_url") or "").strip()
        if not url:
            continue

        stats["pages_fetched"] += 1
        page_text, log_entry = fetcher.fetch(url, fetch_date=fetch_date)
        lookup_log_rows.append(log_entry)

        if not page_text:
            reason = log_entry.get("refusal_reason", "unknown")
            stats["refusals_by_reason"][reason] = stats["refusals_by_reason"].get(reason, 0) + 1
            continue

        # Extract addresses via Gemini
        target_name = str(row.get("business_name") or f"Provider {row['npi']}")
        tin = str(row["tin"])
        npi = str(row["npi"])

        extracted_list, llm_log_entry = extractor.extract_addresses(
            target_name=target_name,
            target_tin=tin,
            page_text=page_text,
            target_or_tin=f"{tin}:{npi}",
        )
        llm_log_rows.append(llm_log_entry)
        stats["calls_used"] = extractor.calls_made

        if not extracted_list:
            r_code = llm_log_entry.get("reason_code", "no_address_found")
            stats["rejected_by_reason"][r_code] = stats["rejected_by_reason"].get(r_code, 0) + 1
            continue

        # Validate with quote check
        accepted_for_target = 0
        for cand_addr in extracted_list:
            is_valid, reject_reason = validate_extracted_address(page_text, cand_addr, target_name)
            if not is_valid:
                stats["rejected_by_reason"][reject_reason] = stats["rejected_by_reason"].get(reject_reason, 0) + 1
                continue

            # Accepted address becomes a claim
            raw_addr = f"{cand_addr['street']}, {cand_addr['city']}, {cand_addr['state']} {cand_addr['zip']}".strip()
            fam = log_entry["source_family"]
            cid = f"web_{hash(npi + url + raw_addr) & 0xffffffffffff:012x}"

            new_claims_rows.append({
                "claim_id": cid,
                "npi": npi,
                "tin": tin,
                "raw_address": raw_addr,
                "source": url,
                "source_family": fam,
                "source_date": fetch_date,
                "claim_type": "web",
                "is_po_box": is_po_box(raw_addr),
                "is_billing_only": False,
                "npi_status": "active",
                "addr1": cand_addr.get("street", ""),
                "addr2": cand_addr.get("unit", ""),
                "city": cand_addr.get("city", ""),
                "state": cand_addr.get("state", ""),
                "zip": cand_addr.get("zip", ""),
            })
            accepted_for_target += 1
            affected_npis.add(npi)
            stats["accepted_by_source_family"][fam] = stats["accepted_by_source_family"].get(fam, 0) + 1

        if accepted_for_target > 0:
            stats["calls_accepted"] += 1

    # Ingest new claims and re-evaluate affected targets
    if new_claims_rows and claims is not None and sites is not None and tin_sites is not None:
        log.info(f"  Ingesting {len(new_claims_rows)} newly extracted web claims...")
        df_new_claims = pd.DataFrame(new_claims_rows)
        all_claims = pd.concat([claims, df_new_claims], ignore_index=True)

        # Rerun WP3 (parse & geocode)
        updated_claims, updated_sites = run_wp3(cfg, all_claims, dry_run=True)
        # Rerun WP5 (candidates & scoring)
        updated_candidates = run_wp5(cfg, targets, updated_claims, updated_sites, tin_sites)
        # Rerun WP6 (selection & labelling)
        _, updated_addresses = run_wp6(cfg, targets, updated_claims, updated_candidates)

        # Calculate upgraded labels
        prev_labels = addresses.set_index(["tin", "npi"])["label"].to_dict()
        new_labels = updated_addresses.set_index(["tin", "npi"])["label"].to_dict()

        for k, prev_l in prev_labels.items():
            new_l = new_labels.get(k, prev_l)
            if prev_l == "unverified" and new_l == "probable":
                stats["labels_upgraded"]["unverified_to_probable"] += 1
            elif prev_l in ("unverified", "probable") and new_l == "verified":
                stats["labels_upgraded"]["probable_to_verified"] += 1

        addresses = updated_addresses
        candidates = updated_candidates

        # Recompute leftovers
        df_leftovers = addresses[addresses["label"] == "unverified"].copy()

    _write_logs_and_leftovers(cfg, df_leftovers, lookup_log_rows, llm_log_rows)
    log.info("WP7 DONE")
    return df_leftovers, addresses, candidates, stats


def _write_logs_and_leftovers(
    cfg: dict,
    leftovers: pd.DataFrame,
    lookup_log_rows: list[dict],
    llm_log_rows: list[dict],
) -> None:
    """Persist leftover queue and audit log CSVs."""
    p = cfg["paths"]
    # Write leftovers
    out_leftovers = p["c5_leftovers"]
    Path(out_leftovers).parent.mkdir(parents=True, exist_ok=True)
    leftovers.to_csv(out_leftovers, index=False)

    # Write lookup log
    df_lookup = pd.DataFrame(lookup_log_rows) if lookup_log_rows else pd.DataFrame(columns=SCHEMA_LOOKUP_LOG)
    lookup_path = p.get("c5_lookup_log", "output/c5/c5_lookup_log.csv")
    df_lookup.to_csv(lookup_path, index=False)

    # Write LLM log
    df_llm = pd.DataFrame(llm_log_rows) if llm_log_rows else pd.DataFrame(columns=SCHEMA_LLM_LOG)
    llm_path = p.get("c5_llm_log", "output/c5/c5_llm_log.csv")
    df_llm.to_csv(llm_path, index=False)
