"""tests/test_c5.py – Unit tests for C5 Address Mapping pipeline.

Tests required by specification:
1. Address normalization and PO-box detection
2. Source family independence
3. TIN site set rules (org NPI, consensus, medicare group overlap)
4. Stale rules (fresher contradiction from another family, distance > 100km)
5. Labelling rules (verified, probable, unverified)
6. Quote-check verification (verbatim check on web text)
7. Geocoding cache idempotency (second lookup makes zero calls)
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pandas as pd
import pytest

from pipeline.c5.common import (
    fmt_npi,
    fmt_tin,
    is_po_box,
    source_to_family,
)
from pipeline.c5.wp3_sites import (
    geocode_unique_addresses,
    normalize_address,
)
from pipeline.c5.wp4_tin_sites import run_wp4
from pipeline.c5.wp5_candidates import haversine_km, run_wp5
from pipeline.c5.wp6_choose import run_wp6
from pipeline.c5.wp7_leftovers import quote_check


# ── 1. Address normalization & PO Box detection ──────────────────────────────
def test_address_normalization():
    assert normalize_address("123 Main Street") == "123 MAIN ST"
    assert normalize_address("456 Ocean Boulevard, Suite 200") == "456 OCEAN BLVD, SUITE 200"
    assert normalize_address("789 Oak Road") == "789 OAK RD"
    assert normalize_address("101 Pine Avenue") == "101 PINE AVE"
    assert normalize_address("55 Parkway Drive") == "55 PKWY DR"


def test_po_box_detection():
    assert is_po_box("P.O. Box 1234") is True
    assert is_po_box("PO Box 500") is True
    assert is_po_box("Post Office Box 999") is True
    assert is_po_box("P O BOX 45") is True
    assert is_po_box("123 Main Street, Suite 400") is False
    assert is_po_box("500 Boxwood Terrace") is False
    assert is_po_box("") is False
    assert is_po_box(None) is False


# ── 2. Source family independence ────────────────────────────────────────────
def test_source_family_independence():
    cfg = {
        "source_families": {
            "nppes_self": ["nppes_primary", "nppes_secondary", "nppes_endpoint"],
            "nppes_org": ["nppes_org_primary", "nppes_org_secondary"],
            "medicare": ["cms_dac"],
        }
    }
    # Two records from NPPES for same NPI belong to the SAME family
    fam_primary = source_to_family("nppes_primary", cfg)
    fam_secondary = source_to_family("nppes_secondary", cfg)
    fam_endpoint = source_to_family("nppes_endpoint", cfg)
    assert fam_primary == fam_secondary == fam_endpoint == "nppes_self"

    # CMS DAC belongs to medicare family (independent from nppes_self)
    fam_dac = source_to_family("cms_dac", cfg)
    assert fam_dac == "medicare"
    assert fam_dac != fam_primary


# ── 3. TIN site sets (org NPI, consensus, medicare group overlap) ─────────────
def test_tin_sites_rules(tmp_path):
    cfg = {
        "site_consensus_min_npis": 2,
        "tin_group_overlap_min": 0.5,
        "paths": {"c5_tin_sites": str(tmp_path / "tin_sites.parquet")},
    }
    targets = pd.DataFrame([
        {"tin": "95-1234567", "npi": "1111111111", "entity_type": "organization"},
        {"tin": "95-1234567", "npi": "2222222222", "entity_type": "individual"},
        {"tin": "95-1234567", "npi": "3333333333", "entity_type": "individual"},
        {"tin": "95-9999999", "npi": "4444444444", "entity_type": "individual"},
    ])
    claims = pd.DataFrame([
        # Org site: site_org
        {"claim_id": "c1", "npi": "1111111111", "site_id": "site_org", "is_po_box": False, "npi_status": "active", "source": "nppes_primary"},
        # Individual consensus on site_consensus: NPIs 2222222222 & 3333333333 (>= 2)
        {"claim_id": "c2", "npi": "2222222222", "site_id": "site_consensus", "is_po_box": False, "npi_status": "active", "source": "nppes_primary"},
        {"claim_id": "c3", "npi": "3333333333", "site_id": "site_consensus", "is_po_box": False, "npi_status": "active", "source": "nppes_primary"},
        # Single individual on site_solo (< 2 consensus)
        {"claim_id": "c4", "npi": "4444444444", "site_id": "site_solo", "is_po_box": False, "npi_status": "active", "source": "nppes_primary"},
    ])

    tin_sites = run_wp4(cfg, targets, claims)
    t1_sites = tin_sites[tin_sites["tin"] == "95-1234567"]
    t1_site_ids = set(t1_sites["site_id"].dropna())

    assert "site_org" in t1_site_ids
    assert "site_consensus" in t1_site_ids
    # Org support evidence tag
    org_ev = t1_sites[t1_sites["site_id"] == "site_org"]["evidence_types"].iloc[0]
    assert "org_npi_supported" in org_ev

    # Solo site for TIN 95-9999999 should not meet consensus threshold
    t2_sites = tin_sites[tin_sites["tin"] == "95-9999999"]
    assert len(t2_sites["site_id"].dropna()) == 0


# ── 4. Stale rules ────────────────────────────────────────────────────────────
def test_stale_rules(tmp_path):
    cfg = {
        "stale_distance_km": 100,
        "fresh_months": 24,
        "verified_min_independent_families": 2,
        "run_date": "2026-10-18",
        "paths": {
            "c5_stale": str(tmp_path / "stale.parquet"),
            "c5_addresses": str(tmp_path / "addresses.csv"),
        },
    }
    targets = pd.DataFrame([
        {"tin": "95-1111111", "npi": "1000000001", "ppo_scope": "strict"},
        {"tin": "95-2222222", "npi": "2000000002", "ppo_scope": "strict"},
    ])

    # Contradiction: claim c1 (NPPES 2020) at site_old, contradicted by c2 (CMS DAC 2025) at site_new
    claims = pd.DataFrame([
        {"claim_id": "c1", "npi": "1000000001", "site_id": "site_old", "source_family": "nppes_self", "source_date": "2020-01-01", "is_po_box": False, "npi_status": "active"},
        {"claim_id": "c2", "npi": "1000000001", "site_id": "site_new", "source_family": "medicare", "source_date": "2025-06-01", "is_po_box": False, "npi_status": "active"},
    ])

    # Candidates: candidate for 2000000002 is 150 km away (> stale_distance_km=100)
    candidates = pd.DataFrame([
        {
            "tin": "95-2222222", "npi": "2000000002", "site_id": "site_far",
            "distance_to_tin_site_km": 150.0, "score": 0.8, "n_independent_families": 2,
            "freshest_source_date": "2026-01-01", "is_stale": False, "s_tin_fit": 0.0,
            "address_line": "Far Away Rd", "unit": "", "city": "Reno", "state": "NV",
            "zip5": "89501", "lat": 39.5, "lon": -119.8, "county": "Washoe",
            "claim_ids": "c3", "families": "nppes_self",
        }
    ])

    stale_df, addrs_df = run_wp6(cfg, targets, claims, candidates)

    # c1 should be flagged as stale due to fresher contradiction
    reasons = stale_df["reason"].tolist()
    assert "fresher_contradiction" in reasons
    assert "distance_from_tin_sites" in reasons

    # Target 2000000002 whose candidate was stale should be marked unverified
    t2_addr = addrs_df[addrs_df["npi"] == "2000000002"].iloc[0]
    assert t2_addr["label"] == "unverified"


# ── 5. Labelling rules ────────────────────────────────────────────────────────
def test_labelling_rules(tmp_path):
    cfg = {
        "stale_distance_km": 100,
        "fresh_months": 24,
        "verified_min_independent_families": 2,
        "run_date": "2026-10-18",
        "paths": {
            "c5_stale": str(tmp_path / "stale.parquet"),
            "c5_addresses": str(tmp_path / "addresses.csv"),
        },
    }
    targets = pd.DataFrame([
        {"tin": "95-1000001", "npi": "1000000001", "ppo_scope": "strict"},
        {"tin": "95-1000002", "npi": "1000000002", "ppo_scope": "strict"},
        {"tin": "95-1000003", "npi": "1000000003", "ppo_scope": "strict"},
    ])
    claims = pd.DataFrame([])

    candidates = pd.DataFrame([
        # Target 1: 2 independent families, fresh -> VERIFIED
        {
            "tin": "95-1000001", "npi": "1000000001", "site_id": "s1",
            "distance_to_tin_site_km": 5.0, "score": 0.95, "n_independent_families": 2,
            "freshest_source_date": "2026-01-01", "s_tin_fit": 0.25,
            "address_line": "100 Market St", "unit": "", "city": "San Francisco", "state": "CA",
            "zip5": "94105", "lat": 37.79, "lon": -122.39, "county": "San Francisco",
            "claim_ids": "c1;c2", "families": "nppes_self;medicare",
        },
        # Target 2: 1 family, fresh + tin_fit -> PROBABLE
        {
            "tin": "95-1000002", "npi": "1000000002", "site_id": "s2",
            "distance_to_tin_site_km": 2.0, "score": 0.65, "n_independent_families": 1,
            "freshest_source_date": "2026-01-01", "s_tin_fit": 0.25,
            "address_line": "200 Mission St", "unit": "", "city": "San Francisco", "state": "CA",
            "zip5": "94105", "lat": 37.79, "lon": -122.39, "county": "San Francisco",
            "claim_ids": "c3", "families": "nppes_self",
        },
        # Target 3: 1 family, old date, no tin fit -> UNVERIFIED
        {
            "tin": "95-1000003", "npi": "1000000003", "site_id": "s3",
            "distance_to_tin_site_km": 10.0, "score": 0.25, "n_independent_families": 1,
            "freshest_source_date": "2018-01-01", "s_tin_fit": 0.0,
            "address_line": "300 Howard St", "unit": "", "city": "San Francisco", "state": "CA",
            "zip5": "94105", "lat": 37.79, "lon": -122.39, "county": "San Francisco",
            "claim_ids": "c4", "families": "nppes_self",
        },
    ])

    _, addrs = run_wp6(cfg, targets, claims, candidates)
    labels = addrs.set_index("npi")["label"].to_dict()

    assert labels["1000000001"] == "verified"
    assert labels["1000000002"] == "probable"
    assert labels["1000000003"] == "unverified"


# ── 6. Quote-check verification ──────────────────────────────────────────────
def test_quote_check():
    page_html = """
    <html>
      <body>
        <div>Welcome to Sutter Health Plaza</div>
        <p>Our office is located at 1200 K Street, Suite 300, Sacramento, CA 95814.</p>
        <div>Hours: 8am - 5pm</div>
      </body>
    </html>
    """
    # Verbatim normalized match
    assert quote_check(page_html, "1200 K Street, Suite 300, Sacramento, CA 95814") is True
    assert quote_check(page_html, "1200 K ST SUITE 300 SACRAMENTO CA 95814") is True
    assert quote_check(page_html, "1200 K St") is True

    # Hallucinated or non-verbatim address
    assert quote_check(page_html, "999 Nowhere Blvd, Los Angeles, CA 90001") is False
    assert quote_check("", "1200 K St") is False
    assert quote_check(page_html, "") is False


# ── 7. Haversine distance ─────────────────────────────────────────────────────
def test_haversine_distance():
    # San Francisco to Oakland is ~13-14 km
    d = haversine_km(37.7749, -122.4194, 37.8044, -122.2712)
    assert 10.0 < d < 20.0


# ── 8. Geocoding cache idempotency ───────────────────────────────────────────
def test_geocoding_cache_idempotency(tmp_path, monkeypatch):
    cache_path = str(tmp_path / "cache.parquet")
    unique_addrs = pd.DataFrame([
        {"raw_address": "123 Main St, Sacramento, CA 95814", "address_norm": "123 MAIN ST, SACRAMENTO, CA 95814"}
    ])

    call_count = 0

    def mock_geocode_batch(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return [{
            "_uid": "0", "matched": True, "match_address": "123 MAIN ST, SACRAMENTO, CA, 95814",
            "lat": 38.5816, "lon": -121.4944, "county_fips": "06067"
        }]

    monkeypatch.setattr("pipeline.c5.wp3_sites._geocode_batch", mock_geocode_batch)

    # First run: should call mock_geocode_batch
    res1 = geocode_unique_addresses(unique_addrs, cache_path=cache_path, batch_size=100, dry_run=False)
    assert len(res1) == 1
    assert call_count == 1
    assert Path(cache_path).is_file()

    # Second run: must hit cache and make ZERO network calls
    res2 = geocode_unique_addresses(unique_addrs, cache_path=cache_path, batch_size=100, dry_run=False)
    assert len(res2) == 1
    assert call_count == 1  # Still 1, zero additional calls!


# ── 9. Family independence: Payer vs Web Aggregator vs NPPES ─────────────────
def test_family_independence_rules():
    from pipeline.c5.common import count_independent_families

    # NPPES self + aggregator copies NPPES -> only 1 independent family
    assert count_independent_families(["nppes_self", "web_aggregator"]) == 1
    assert count_independent_families(["nppes_org", "web_aggregator"]) == 1

    # Aggregator alone -> 1
    assert count_independent_families(["web_aggregator"]) == 1

    # Payer directory is truly independent -> 2
    assert count_independent_families(["nppes_self", "payer"]) == 2

    # Organization website is independent of NPPES -> 2
    assert count_independent_families(["nppes_self", "web"]) == 2

    # Aggregator + Payer (no NPPES present) -> 2
    assert count_independent_families(["web_aggregator", "payer"]) == 2

    # NPPES + Aggregator + Payer -> 2 (aggregator subsumed by NPPES)
    assert count_independent_families(["nppes_self", "web_aggregator", "payer"]) == 2


# ── 10. Quote check rejection reasons ─────────────────────────────────────────
def test_quote_check_rejections():
    from pipeline.c5.wp7_leftovers import validate_extracted_address

    page_text = "Welcome to Pacific Urology Clinic. Dr. Jane Smith practices at 450 Sutter St, Suite 1200, San Francisco, CA 94108."

    # Valid
    valid_item = {
        "provider_match": "Dr. Jane Smith",
        "street": "450 Sutter St",
        "unit": "Suite 1200",
        "city": "San Francisco",
        "state": "CA",
        "zip": "94108",
        "quote": "450 Sutter St, Suite 1200, San Francisco, CA 94108",
    }
    is_valid, reason = validate_extracted_address(page_text, valid_item, "Jane Smith")
    assert is_valid is True
    assert reason is None

    # Rejection: quote_not_found
    quote_missing = dict(valid_item, quote="999 Ocean Ave, Suite 100, San Francisco, CA 94108")
    is_valid, reason = validate_extracted_address(page_text, quote_missing, "Jane Smith")
    assert is_valid is False
    assert reason == "quote_not_found"

    # Rejection: provider_mismatch
    provider_wrong = dict(valid_item, provider_match="Dr. Robert Johnson")
    is_valid, reason = validate_extracted_address(page_text, provider_wrong, "Jane Smith")
    assert is_valid is False
    assert reason == "provider_mismatch"

    # Rejection: malformed_json (missing street/city)
    malformed = dict(valid_item, street="", city="")
    is_valid, reason = validate_extracted_address(page_text, malformed, "Jane Smith")
    assert is_valid is False
    assert reason == "malformed_json"


# ── 11. Page Fetcher tests (allowlist, robots, redirect, size, no_content) ────
def test_page_fetcher_allowlist_and_refusals(tmp_path, monkeypatch):
    from pipeline.c5.page_fetcher import PageFetcher

    cfg = {
        "web_fetch_enabled": True,
        "web_fetch_allowlist": ["healthnet.com", "sutterhealth.org"],
        "web_fetch_domain_family": {"healthnet.com": "payer", "sutterhealth.org": "web"},
        "web_fetch_respect_robots": True,
        "web_fetch_max_bytes": 1000,
        "web_fetch_cache_dir": str(tmp_path / "web_cache"),
    }

    # 1. Empty allowlist
    cfg_empty = dict(cfg, web_fetch_allowlist=[])
    fetcher_empty = PageFetcher(cfg_empty)
    _, log_e = fetcher_empty.fetch("https://healthnet.com/providers")
    assert log_e["refusal_reason"] == "empty_allowlist"

    fetcher = PageFetcher(cfg)

    # 2. Disallowed domain
    _, log_dis = fetcher.fetch("https://unauthorized-directory.com/doc")
    assert log_dis["refusal_reason"] == "disallowed_domain"

    # 3. Robots disallowed
    monkeypatch.setattr("pipeline.c5.page_fetcher.check_robots_txt", lambda url: False)
    _, log_rob = fetcher.fetch("https://healthnet.com/private/search")
    assert log_rob["refusal_reason"] == "robots_disallowed"
    monkeypatch.setattr("pipeline.c5.page_fetcher.check_robots_txt", lambda url: True)

    # 4. Redirect to disallowed domain
    class MockRedirectSession:
        def __init__(self):
            self.max_redirects = 5
        def get(self, url, **kwargs):
            class Resp:
                status_code = 200
                url = "https://thirdparty-tracker.com/landing"
                headers = {}
                content = b"<html>Some text</html>"
                encoding = "utf-8"
            return Resp()

    monkeypatch.setattr("requests.Session", MockRedirectSession)
    _, log_red = fetcher.fetch("https://healthnet.com/redirect")
    assert "redirect_disallowed" in str(log_red["refusal_reason"])

    # 5. Oversized page
    class MockOversizedSession:
        def __init__(self):
            self.max_redirects = 5
        def get(self, url, **kwargs):
            class Resp:
                status_code = 200
                url = "https://healthnet.com/huge"
                headers = {"Content-Length": "5000"}
                content = b"A" * 5000
                encoding = "utf-8"
            return Resp()

    monkeypatch.setattr("requests.Session", MockOversizedSession)
    _, log_over = fetcher.fetch("https://healthnet.com/huge")
    assert log_over["refusal_reason"] == "oversized"

    # 6. No usable content (e.g. JS empty shell)
    class MockEmptySession:
        def __init__(self):
            self.max_redirects = 5
        def get(self, url, **kwargs):
            class Resp:
                status_code = 200
                url = "https://healthnet.com/spa"
                headers = {}
                content = b"<html><head><script src='app.js'></script></head><body><div id='root'></div></body></html>"
                encoding = "utf-8"
            return Resp()

    monkeypatch.setattr("requests.Session", MockEmptySession)
    _, log_noc = fetcher.fetch("https://healthnet.com/spa")
    assert log_noc["refusal_reason"] == "no_content"


def test_page_fetcher_cache_idempotency(tmp_path, monkeypatch):
    from pipeline.c5.page_fetcher import PageFetcher

    cfg = {
        "web_fetch_enabled": True,
        "web_fetch_allowlist": ["healthnet.com"],
        "web_fetch_domain_family": {"healthnet.com": "payer"},
        "web_fetch_respect_robots": False,
        "web_fetch_max_bytes": 50000,
        "web_fetch_cache_dir": str(tmp_path / "web_cache"),
    }
    fetcher = PageFetcher(cfg)

    call_count = 0
    html_content = b"<html><body><div>Doctors Office: 100 Main St, Sacramento, CA 95814</div></body></html>"

    class MockSession:
        def __init__(self):
            self.max_redirects = 5
        def get(self, url, **kwargs):
            nonlocal call_count
            call_count += 1
            class Resp:
                status_code = 200
                url = "https://healthnet.com/doc"
                headers = {}
                content = html_content
                encoding = "utf-8"
            return Resp()

    monkeypatch.setattr("requests.Session", MockSession)

    text1, log1 = fetcher.fetch("https://healthnet.com/doc", fetch_date="2026-10-18")
    assert "100 Main St" in text1
    assert call_count == 1
    assert log1["cache_hit"] is False

    # Second call must hit cache with ZERO network calls
    text2, log2 = fetcher.fetch("https://healthnet.com/doc", fetch_date="2026-10-18")
    assert text2 == text1
    assert call_count == 1  # Still 1!
    assert log2["cache_hit"] is True


# ── 12. Gemini Backend Mocked Client Tests ────────────────────────────────────
def test_gemini_backend_missing_key(monkeypatch):
    from pipeline.c5.gemini_client import GeminiExtractor

    cfg = {
        "llm_enabled": True,
        "llm_provider": "gemini",
        "gemini_api_key_env": "TEST_GEMINI_NONEXISTENT_KEY",
        "paths": {"prompt_template": "prompts/c5_extract_v1.txt"},
    }
    monkeypatch.delenv("TEST_GEMINI_NONEXISTENT_KEY", raising=False)
    with pytest.raises(ValueError, match="API key not found"):
        GeminiExtractor(cfg)


def test_gemini_backend_mocked_responses(tmp_path):
    from pipeline.c5.gemini_client import GeminiExtractor

    cfg = {
        "llm_enabled": True,
        "llm_provider": "gemini",
        "llm_model": "gemini-2.5-flash",
        "llm_call_cap": 2,
        "llm_max_retries": 2,
        "llm_requests_per_minute": 1000,
        "llm_cache_dir": str(tmp_path / "llm_cache"),
        "paths": {"prompt_template": "prompts/c5_extract_v1.txt"},
    }

    # 1. Valid JSON extraction
    class MockValidModels:
        def generate_content(self, model, contents, config):
            class Response:
                text = json.dumps([{
                    "provider_match": "Dr. Sarah Lee",
                    "street": "1200 K St",
                    "unit": "Suite 100",
                    "city": "Sacramento",
                    "state": "CA",
                    "zip": "95814",
                    "quote": "1200 K St, Suite 100, Sacramento, CA 95814",
                }])
                usage_metadata = type("Usage", (), {"total_token_count": 150})()
            return Response()

    mock_client = type("Client", (), {"models": MockValidModels()})()
    extractor = GeminiExtractor(cfg, client=mock_client)

    addrs, log_entry = extractor.extract_addresses("Sarah Lee", "95-1234567", "Some text")
    assert len(addrs) == 1
    assert addrs[0]["street"] == "1200 K St"
    assert log_entry["status"] == "accepted"
    assert log_entry["tokens"] == 150

    # 2. Cache test: second call hits disk cache with zero client calls
    mock_client_dead = type("Client", (), {"models": None})()
    extractor_cached = GeminiExtractor(cfg, client=mock_client_dead)
    addrs_c, log_c = extractor_cached.extract_addresses("Sarah Lee", "95-1234567", "Some text")
    assert len(addrs_c) == 1
    assert log_c["cache_hit"] is True

    # 3. Malformed JSON
    class MockMalformedModels:
        def generate_content(self, model, contents, config):
            class Response:
                text = "This is not json at all!"
                usage_metadata = None
            return Response()

    mock_malformed = type("Client", (), {"models": MockMalformedModels()})()
    extractor_bad = GeminiExtractor(cfg, client=mock_malformed)
    addrs_m, log_m = extractor_bad.extract_addresses("Other Provider", "95-9999999", "Other text")
    assert len(addrs_m) == 0
    assert "api_error" in str(log_m["reason_code"]) or "malformed" in str(log_m["reason_code"])

    # 4. Call cap enforcement
    extractor.calls_made = 2  # reaches cap=2
    addrs_cap, log_cap = extractor.extract_addresses("Cap Provider", "95-0000000", "Text")
    assert len(addrs_cap) == 0
    assert log_cap["reason_code"] == "call_cap_reached"


# ── 13. Tiered Extraction & Domain Probe Unit Tests ──────────────────────────
def test_subdomain_matching_and_redirects():
    from pipeline.c5.page_fetcher import is_allowed_domain

    allowlist = ["healthnet.com", "search.dca.ca.gov"]

    # Subdomain matching enabled
    assert is_allowed_domain("https://www.healthnet.com/doc", allowlist, match_subdomains=True) is True
    assert is_allowed_domain("https://sub.search.dca.ca.gov/search", allowlist, match_subdomains=True) is True
    assert is_allowed_domain("https://unauthorized.com/doc", allowlist, match_subdomains=True) is False

    # Subdomain matching disabled
    assert is_allowed_domain("https://www.healthnet.com/doc", allowlist, match_subdomains=False) is False
    assert is_allowed_domain("https://healthnet.com/doc", allowlist, match_subdomains=False) is True


def test_probe_outcomes_and_gate(tmp_path, monkeypatch):
    from pipeline.c5.page_fetcher import PageFetcher, probe_single_url, run_domain_probe

    cfg = {
        "web_fetch_enabled": True,
        "web_fetch_allowlist": ["healthnet.com", "faildomain.com"],
        "web_fetch_match_subdomains": True,
        "probe_urls": {
            "healthnet.com": ["https://healthnet.com/doc1", "https://healthnet.com/doc2"],
            "faildomain.com": ["https://faildomain.com/bad"],
        },
        "probe_min_ok_share": 0.5,
        "paths": {"c5_domain_probe": str(tmp_path / "probe.csv")},
    }

    # Mock responses for probe_single_url
    def mock_probe_single_url(url, fetcher):
        if "bad" in url:
            return "login_or_bot_block", "CAPTCHA detected"
        return "ok", "Readable text containing address pattern"

    monkeypatch.setattr("pipeline.c5.page_fetcher.probe_single_url", mock_probe_single_url)
    fetcher = PageFetcher(cfg)
    df_probe, domain_status, unprobed = run_domain_probe(cfg, fetcher)

    assert domain_status["healthnet.com"] == "passed"
    assert domain_status["faildomain.com"] == "disabled_by_probe"
    assert len(unprobed) == 0
    assert Path(tmp_path / "probe.csv").is_file()


def test_unprobed_domain_instructions(tmp_path):
    from pipeline.c5.page_fetcher import run_domain_probe

    cfg = {
        "web_fetch_enabled": True,
        "web_fetch_allowlist": ["healthnet.com", "search.dca.ca.gov"],
        "probe_urls": {},  # empty probe_urls
        "probe_min_ok_share": 0.5,
        "paths": {"c5_domain_probe": str(tmp_path / "probe.csv")},
    }
    df_probe, domain_status, unprobed = run_domain_probe(cfg)
    assert set(unprobed) == {"healthnet.com", "search.dca.ca.gov"}
    assert domain_status["healthnet.com"] == "unprobed"
    assert df_probe[df_probe["status"] == "unprobed"].shape[0] == 2


def test_template_filling_and_search_disabled():
    from pipeline.c5.wp7_leftovers import apply_url_templates

    templates = {
        "npi_lookup": "https://healthnet.com/provider/{npi}",
        "slug_lookup": "https://healthnet.com/find/{city_slug}/{name_slug}",
        "missing_ph": "https://healthnet.com/{missing_placeholder}/{npi}",
    }

    # 1. Complete row
    row_full = {
        "npi": "1234567890",
        "tin": "95-1234567",
        "business_name": "Jane Smith MD",
        "city": "Sacramento",
        "zip5": "95814",
    }
    urls = apply_url_templates(templates, row_full)
    assert "https://healthnet.com/provider/1234567890" in urls
    assert "https://healthnet.com/find/sacramento/jane-smith-md" in urls
    # Missing placeholder is skipped
    assert not any("missing_placeholder" in u for u in urls)

    # 2. Missing city skips slug_lookup template
    row_no_city = dict(row_full, city="")
    urls_no_city = apply_url_templates(templates, row_no_city)
    assert "https://healthnet.com/provider/1234567890" in urls_no_city
    assert not any("find" in u for u in urls_no_city)


def test_tier0_confirm_and_proximity():
    from pipeline.c5.wp7_leftovers import tier0_confirm_target

    cand = {"address_line": "1200 K Street", "city": "Sacramento", "zip5": "95814"}

    # Target name nearby -> Match
    page_near = "Dr. Jane Smith welcomes patients to our downtown Sacramento clinic located at 1200 K Street, Suite 300, 95814."
    matched, quote = tier0_confirm_target(page_near, cand, "Jane Smith", proximity_chars=400)
    assert matched is True

    # Target name far away (>400 chars) -> No match
    filler = "A" * 600
    page_far = f"Dr. Jane Smith {filler} Clinic address is 1200 K Street, Sacramento, 95814."
    matched_far, _ = tier0_confirm_target(page_far, cand, "Jane Smith", proximity_chars=400)
    assert matched_far is False


def test_tier1_structured_extraction():
    from pipeline.c5.wp7_leftovers import tier1_structured_extract

    html = """
    <html>
      <head>
        <script type="application/ld+json">
        {
          "@context": "https://schema.org",
          "@type": "Physician",
          "name": "Dr. Sarah Lee",
          "address": {
            "@type": "PostalAddress",
            "streetAddress": "450 Sutter St",
            "addressLocality": "San Francisco",
            "addressRegion": "CA",
            "postalCode": "94108"
          }
        }
        </script>
      </head>
      <body>
        <address>
          1200 Market Street, San Francisco, CA 94102
        </address>
      </body>
    </html>
    """
    addrs = tier1_structured_extract(html)
    assert len(addrs) >= 2
    types = [a["source_type"] for a in addrs]
    assert "json_ld" in types
    assert "address_tag" in types
    assert any("450 Sutter St" in a["street"] for a in addrs)
    assert any("1200 Market Street" in a["street"] for a in addrs)


def test_validation_fields_in_quote_and_proximity():
    from pipeline.c5.wp7_leftovers import validate_extracted_address_tiered

    page_text = "Welcome to Sutter Health. Dr. John Davis practices at 800 J Street, Sacramento, CA 95814."

    # Valid item
    item_valid = {
        "street": "800 J Street",
        "unit": "",
        "city": "Sacramento",
        "state": "CA",
        "zip": "95814",
        "quote": "800 J Street, Sacramento, CA 95814",
    }
    valid, reason = validate_extracted_address_tiered(page_text, item_valid, "John Davis")
    assert valid is True

    # Fields not in quote
    item_bad_fields = dict(item_valid, street="999 Elm St")
    valid_bf, reason_bf = validate_extracted_address_tiered(page_text, item_bad_fields, "John Davis")
    assert valid_bf is False
    assert reason_bf == "fields_not_in_quote"

    # Name not near address
    page_far = "Dr. John Davis " + ("word " * 200) + "Practices at 800 J Street, Sacramento, CA 95814."
    valid_far, reason_far = validate_extracted_address_tiered(page_far, item_valid, "John Davis", proximity_chars=200)
    assert valid_far is False
    assert reason_far == "name_not_near_address"


def test_gemini_batched_tier2_and_unknown_key(tmp_path):
    from pipeline.c5.gemini_client import GeminiExtractor

    cfg = {
        "llm_enabled": True,
        "llm_provider": "gemini",
        "llm_model": "gemini-2.5-flash",
        "llm_call_cap": 5,
        "llm_max_input_chars": 6000,
        "llm_cache_dir": str(tmp_path / "llm_cache"),
        "paths": {"prompt_template_v2": "prompts/c5_extract_v2.txt"},
    }

    class MockBatchModels:
        def generate_content(self, model, contents, config):
            class Response:
                text = json.dumps([
                    {
                        "target_key": "95-100:10001",
                        "provider_match": "Dr. Alice",
                        "street": "100 Pine St",
                        "unit": "",
                        "city": "San Francisco",
                        "state": "CA",
                        "zip": "94111",
                        "quote": "Dr. Alice at 100 Pine St, San Francisco, CA 94111",
                    },
                    {
                        "target_key": "UNKNOWN_KEY_999",  # Should be rejected
                        "provider_match": "Dr. Bob",
                        "street": "200 Pine St",
                        "unit": "",
                        "city": "San Francisco",
                        "state": "CA",
                        "zip": "94111",
                        "quote": "Dr. Bob at 200 Pine St",
                    },
                ])
                usage_metadata = type("Usage", (), {"total_token_count": 250})()
            return Response()

    mock_client = type("Client", (), {"models": MockBatchModels()})()
    extractor = GeminiExtractor(cfg, client=mock_client)

    targets = [
        {"target_key": "95-100:10001", "name": "Alice", "entity_type": "individual"},
        {"target_key": "95-100:10002", "name": "Charlie", "entity_type": "individual"},
    ]
    addrs, log_entry = extractor.extract_batched_addresses(targets, "Sample trimmed text", "hash123")
    assert len(addrs) == 1
    assert addrs[0]["target_key"] == "95-100:10001"
    assert log_entry["status"] == "accepted"


def test_web_claim_never_verifies_alone(tmp_path):
    from pipeline.c5.common import count_independent_families

    # web alone -> 1 family (cannot reach verified threshold of 2)
    assert count_independent_families(["web"]) == 1
    assert count_independent_families(["web_aggregator"]) == 1
    # web + nppes_self -> 2 families
    assert count_independent_families(["web", "nppes_self"]) == 2
    # web_aggregator + nppes_self -> 1 family (aggregator not independent of nppes)
    assert count_independent_families(["web_aggregator", "nppes_self"]) == 1


# ── State Sources Tests (14 - 20) ──────────────────────────────────────────────

def test_normalize_license_number():
    from pipeline.c5.wp2_state_sources import normalize_license_number

    prefix, patterns = normalize_license_number("A-12345")
    assert prefix == "A"
    assert "12345" in patterns
    assert "A12345" in patterns

    prefix, patterns = normalize_license_number(" C 09876 ")
    assert prefix == "C"
    assert "9876" in patterns

    prefix, patterns = normalize_license_number("20A-54321")
    assert prefix == "20A"
    assert "54321" in patterns


def test_norm_name_and_last_name():
    from pipeline.c5.wp2_state_sources import norm_name, norm_last_name

    assert norm_name("Acme Health, Inc.") == "ACMEHEALTH"
    assert norm_name("Care Provider LLC") == "CAREPROVIDER"
    assert norm_last_name("Smith-Jones") == "SMITHJONES"
    assert norm_last_name("O'Connor") == "OCONNOR"
    assert norm_last_name("") == ""


def test_dca_matching_and_exclusions(tmp_path):
    import pandas as pd
    from pipeline.c5.wp2_state_sources import load_dca_claims

    dca_dir = tmp_path / "dca"
    dca_dir.mkdir()
    dca_file = dca_dir / "MedicalBoard_sample.tsv"

    header = "Agency Code\tAgency Name\tLicTypeCode\tLicense Type\tLicense Number\tIndiv/Org\tOrg/Last Name\tFirst Name\tMiddle Name\tSuffix\tAddress Line 1\tAddress Line 2\tCity\tCounty\tState\tZip\tCountry\tOriginal Issue Date\tExpiration Date\tLicense Status"
    rows = [
        # Valid match (future expiry -> fresh)
        "200\tMedical Board\t2001\tPhysician\tA10001\tI\tSmith\tAlice\tM\t\t100 Main St\tSte 100\tSacramento\tSacramento\tCA\t95814\tUSA\t01-01-2010\t12-31-2027\tRenewed / Current",
        # PO Box (should be excluded from sites)
        "200\tMedical Board\t2001\tPhysician\tA10002\tI\tJones\tBob\t\t\tPO Box 999\t\tSacramento\tSacramento\tCA\t95814\tUSA\t01-01-2010\t12-31-2027\tRenewed / Current",
        # Non-CA (should be excluded)
        "200\tMedical Board\t2001\tPhysician\tA10003\tI\tTaylor\tCarol\t\t\t456 Elm St\t\tReno\tWashoe\tNV\t89501\tUSA\t01-01-2010\t12-31-2027\tRenewed / Current",
        # Ambiguous match (2 entries with same license number)
        "200\tMedical Board\t2001\tPhysician\tA10004\tI\tDavis\tDan\t\t\t789 Pine St\t\tFresno\tFresno\tCA\t93721\tUSA\t01-01-2010\t12-31-2027\tRenewed / Current",
        "200\tMedical Board\t2001\tPhysician\tA10004\tI\tDavis\tDan\t\t\t789 Oak St\t\tFresno\tFresno\tCA\t93721\tUSA\t01-01-2010\t12-31-2027\tRenewed / Current",
    ]
    dca_file.write_text(header + "\n" + "\n".join(rows))

    # Mini NPPES main CSV
    nppes_file = tmp_path / "nppes_mini.csv"
    lic_headers = []
    for i in range(1, 16):
        lic_headers.append(f'"Provider License Number_{i}"')
        lic_headers.append(f'"Provider License Number State Code_{i}"')
    nppes_hdr = '"NPI","Provider Last Name (Legal Name)","Provider First Name",' + ",".join(lic_headers)

    def make_nppes_row(npi, last, first, lic_num, lic_st):
        slots = [f'"{lic_num}"', f'"{lic_st}"'] + ['""', '""'] * 14
        return f'"{npi}","{last}","{first}",' + ",".join(slots)

    nppes_rows = [
        make_nppes_row("1111111111", "Smith", "Alice", "A10001", "CA"),
        make_nppes_row("2222222222", "Jones", "Bob", "A10002", "CA"),
        make_nppes_row("3333333333", "Taylor", "Carol", "A10003", "CA"),
        make_nppes_row("4444444444", "Davis", "Dan", "A10004", "CA"),
    ]
    nppes_file.write_text(nppes_hdr + "\n" + "\n".join(nppes_rows))

    targets = pd.DataFrame([
        {"npi": "1111111111", "tin": "99-1111111", "entity_type": "individual"},
        {"npi": "2222222222", "tin": "99-1111111", "entity_type": "individual"},
        {"npi": "3333333333", "tin": "99-1111111", "entity_type": "individual"},
        {"npi": "4444444444", "tin": "99-1111111", "entity_type": "individual"},
    ])

    cfg = {
        "sources": {"dca_license_bulk": {"local_dir": str(dca_dir), "boards": ["MedicalBoard"]}},
        "dca_po_box_policy": "exclude_from_sites",
        "dca_freshness_rule": "license_status_and_expiry",
    }

    claims, stats = load_dca_claims(cfg, targets, str(nppes_file))
    # Alice matches (usable address); Bob matches (PO Box flagged); Carol excluded by NV (non-CA); Dan ambiguous
    assert len(claims) == 2
    assert set(claims["npi"]) == {"1111111111", "2222222222"}
    alice_claim = claims[claims["npi"] == "1111111111"].iloc[0]
    bob_claim = claims[claims["npi"] == "2222222222"].iloc[0]
    assert alice_claim["source"] == "dca_license_bulk"
    assert alice_claim["source_family"] == "state_license"
    assert not alice_claim["is_po_box"]
    assert bool(bob_claim["is_po_box"])
    assert stats["matched"] == 2
    assert stats["excluded_pobox"] == 1
    assert stats["ambiguous"] == 1


def test_cdph_matching_priority(tmp_path):
    import pandas as pd
    from pipeline.c5.wp2_state_sources import load_cdph_claims

    cdph_dir = tmp_path / "cdph"
    cdph_dir.mkdir()
    cdph_file = cdph_dir / "health_facility_locations.csv"

    header = "FACID,FACNAME,BUSINESS_NAME,FAC_FDR,FAC_STATUS_TYPE_CODE,ADDRESS,CITY,ZIP,COUNTY_NAME,NPI,LATITUDE,LONGITUDE,DATA_DATE,LICENSE_STATUS_DESCRIPTION"
    rows = [
        # Row 1: Matches on NPI
        "FAC001,Mercy Hospital,MERCY GENERAL,General Acute Care Hospital,OPEN,123 Hospital Way,Sacramento,95819,Sacramento,1234567890,38.5,-121.4,10/16/2026,LICENSED",
        # Row 2: Matches on Name + ZIP
        "FAC002,Sutter Health Care Center,SUTTER HEALTH,Clinic,OPEN,456 Clinic Blvd,Roseville,95661,Placer,,38.7,-121.2,10/16/2026,LICENSED",
        # Row 3: Matches on Name + City
        "FAC003,Kaiser Permanente Center,KAISER,Hospital,OPEN,789 Kaiser Way,Oakland,94611,Alameda,,37.8,-122.2,10/16/2026,LICENSED",
    ]
    cdph_file.write_text(header + "\n" + "\n".join(rows))

    targets = pd.DataFrame([
        {"npi": "1234567890", "tin": "88-1234567", "entity_type": "organization", "business_name": "Mercy Hospital"},
        {"npi": "2345678901", "tin": "88-1234567", "entity_type": "organization", "business_name": "Sutter Health Care Center"},
        {"npi": "3456789012", "tin": "88-1234567", "entity_type": "organization", "business_name": "Kaiser Permanente Center"},
    ])

    existing_claims = pd.DataFrame([
        {"npi": "2345678901", "zip": "95661", "city": "Roseville"},
        {"npi": "3456789012", "zip": "99999", "city": "Oakland"},
    ])

    cfg = {
        "sources": {"cdph_facility": {"local_dir": str(cdph_dir)}},
        "facility_match_order": ["npi", "name_zip", "name_city"],
    }

    claims, stats = load_cdph_claims(cfg, targets, existing_claims)
    assert len(claims) == 3
    methods = dict(zip(claims["npi"], claims["match_method"]))
    assert methods["1234567890"] == "cdph_npi"
    assert methods["2345678901"] == "cdph_name_zip"
    assert methods["3456789012"] == "cdph_name_city"
    assert stats["matched_by_method"]["cdph_npi"] == 1
    assert stats["matched_by_method"]["cdph_name_zip"] == 1
    assert stats["matched_by_method"]["cdph_name_city"] == 1


def test_state_source_family_independence():
    from pipeline.c5.common import count_independent_families

    assert count_independent_families(["state_license"]) == 1
    assert count_independent_families(["state_facility"]) == 1
    assert count_independent_families(["state_license", "nppes_self"]) == 2
    assert count_independent_families(["state_facility", "nppes_self"]) == 2
    assert count_independent_families(["state_license", "state_facility"]) == 2
    assert count_independent_families(["state_license", "web"]) == 2
    assert count_independent_families(["state_license", "web_aggregator"]) == 2


def test_strict_vs_loose_labels():
    from pipeline.c5.wp6_choose import _compute_label, _compute_loose_label

    # Strict: requires >= 2 independent families + is_fresh
    assert _compute_label(False, 2, True) == "verified"
    assert _compute_label(False, 1, True, s_tin_fit=0.0) == "unverified"
    assert _compute_label(False, 1, False, s_tin_fit=0.0) == "unverified"
    assert _compute_label(True, 2, True) == "unverified"

    # Loose: 1 fresh family + s_tin_fit >= 0.25 -> verified
    assert _compute_loose_label(False, 1, True, 0.30, 0.25) == "verified"
    assert _compute_loose_label(False, 1, True, 0.10, 0.25) == "probable"
    assert _compute_loose_label(False, 0, False, 0.30, 0.25) == "probable"
    assert _compute_loose_label(True, 2, True, 0.50, 0.25) == "unverified"


def test_claim_id_determinism(tmp_path):
    import pandas as pd
    from pipeline.c5.wp2_state_sources import load_dca_claims

    dca_dir = tmp_path / "dca"
    dca_dir.mkdir()
    dca_file = dca_dir / "MedicalBoard_sample.tsv"
    row = "200\tMedical Board\t2001\tPhysician\tA10001\tI\tSmith\tAlice\tM\t\t100 Main St\tSte 100\tSacramento\tSacramento\tCA\t95814\tUSA\t01-01-2010\t12-31-2027\tRenewed / Current"
    header = "Agency Code\tAgency Name\tLicTypeCode\tLicense Type\tLicense Number\tIndiv/Org\tOrg/Last Name\tFirst Name\tMiddle Name\tSuffix\tAddress Line 1\tAddress Line 2\tCity\tCounty\tState\tZip\tCountry\tOriginal Issue Date\tExpiration Date\tLicense Status"
    dca_file.write_text(header + "\n" + row)

    nppes_file = tmp_path / "nppes_mini.csv"
    lic_headers = []
    for i in range(1, 16):
        lic_headers.append(f'"Provider License Number_{i}"')
        lic_headers.append(f'"Provider License Number State Code_{i}"')
    nppes_hdr = '"NPI","Provider Last Name (Legal Name)","Provider First Name",' + ",".join(lic_headers)
    slots = ['"A10001"', '"CA"'] + ['""', '""'] * 14
    nppes_row = '"1111111111","Smith","Alice",' + ",".join(slots)
    nppes_file.write_text(nppes_hdr + "\n" + nppes_row)

    targets = pd.DataFrame([
        {"npi": "1111111111", "tin": "99-1111111", "entity_type": "individual"},
    ])
    cfg = {
        "sources": {"dca_license_bulk": {"local_dir": str(dca_dir), "boards": ["MedicalBoard"]}},
        "dca_po_box_policy": "exclude_from_sites",
        "dca_freshness_rule": "license_status_and_expiry",
    }

    claims1, _ = load_dca_claims(cfg, targets, str(nppes_file))
    claims2, _ = load_dca_claims(cfg, targets, str(nppes_file))
    assert claims1.iloc[0]["claim_id"] == claims2.iloc[0]["claim_id"]
    assert len(claims1.iloc[0]["claim_id"]) == 16
    assert claims1.iloc[0]["source"] == "dca_license_bulk"
