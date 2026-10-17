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
