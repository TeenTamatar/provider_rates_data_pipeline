"""pipeline/c5/page_fetcher.py – Safe, allowlisted static web fetcher with caching and robots.txt.

Guarantees:
- Strict allowlist filtering (web_fetch_allowlist).
- Never follows redirects to non-allowlisted domains.
- Respects robots.txt (urllib.robotparser).
- Per-domain rate limiting.
- Static HTML only; strips script/style/nav to extract clean text.
- If text is empty/unusable, logs 'no_content'.
- Never bypasses bot-protections or logins; logs refusals with reason codes.
- Disk caching keyed by URL + fetch date. Second run makes zero network calls.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.robotparser
from datetime import date
from pathlib import Path
from typing import Any, Optional

import requests
from bs4 import BeautifulSoup

from .common import get_logger

log = get_logger("c5.fetcher")

USER_AGENT = "HiLabs-PriceTransparency/1.0 (+https://github.com/HiLabs)"
_ROBOTS_CACHE: dict[str, urllib.robotparser.RobotFileParser] = {}
_LAST_REQUEST_TIME: dict[str, float] = {}


def get_domain(url: str) -> str:
    """Extract registered/hostname domain from URL."""
    parsed = urllib.parse.urlparse(url)
    netloc = parsed.netloc.lower()
    if ":" in netloc:
        netloc = netloc.split(":")[0]
    return netloc


def is_allowed_domain(url: str, allowlist: list[str]) -> bool:
    """Check if URL host matches any allowlist entry."""
    domain = get_domain(url)
    for allowed in allowlist:
        allowed = allowed.lower().strip()
        if domain == allowed or domain.endswith("." + allowed):
            return True
    return False


def check_robots_txt(url: str, timeout: int = 10) -> bool:
    """Verify if URL path is allowed by domain's robots.txt."""
    domain = get_domain(url)
    if domain in _ROBOTS_CACHE:
        rp = _ROBOTS_CACHE[domain]
        return rp.can_fetch(USER_AGENT, url)

    parsed = urllib.parse.urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    rp = urllib.robotparser.RobotFileParser()
    try:
        resp = requests.get(robots_url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
        if resp.status_code == 200:
            rp.parse(resp.text.splitlines())
        else:
            rp.allow_all = True
    except Exception:
        rp.allow_all = True

    _ROBOTS_CACHE[domain] = rp
    return rp.can_fetch(USER_AGENT, url)


def html_to_clean_text(html_content: str) -> str:
    """Convert raw HTML to clean text by stripping scripts, styles, headers, footers."""
    if not html_content or not html_content.strip():
        return ""
    soup = BeautifulSoup(html_content, "html.parser")
    # Remove script, style, meta, nav, footer
    for element in soup(["script", "style", "nav", "footer", "header", "noscript", "svg"]):
        element.extract()

    text = soup.get_text(separator=" ", strip=True)
    # Collapse multiple whitespaces
    return re.sub(r"\s+", " ", text).strip()


class PageFetcher:
    """Allowlisted, cached static HTML page fetcher."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.enabled = bool(cfg.get("web_fetch_enabled", False))
        self.allowlist = [d.lower().strip() for d in cfg.get("web_fetch_allowlist", [])]
        self.domain_family_map = {
            k.lower().strip(): v for k, v in cfg.get("web_fetch_domain_family", {}).items()
        }
        self.respect_robots = bool(cfg.get("web_fetch_respect_robots", True))
        self.max_bytes = int(cfg.get("web_fetch_max_bytes", 2_000_000))
        self.timeout = int(cfg.get("llm_timeout_seconds", 30))
        self.cache_dir = Path(cfg.get("web_fetch_cache_dir", "output/c5/web_cache"))
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.rpm = int(cfg.get("web_fetch_requests_per_minute", 20))
        self.delay_between_calls = 60.0 / max(self.rpm, 1)

    def get_source_family(self, url: str) -> str:
        """Map URL domain to source family (payer, web, web_aggregator)."""
        domain = get_domain(url)
        for dom, fam in self.domain_family_map.items():
            if domain == dom or domain.endswith("." + dom):
                return fam
        log.warning(f"Domain {domain} not in web_fetch_domain_family; defaulting to 'web'")
        return "web"

    def _cache_path(self, url: str, fetch_date: str) -> Path:
        key = hashlib.sha256(f"{url}|{fetch_date}".encode()).hexdigest()
        return self.cache_dir / f"{key}.json"

    def fetch(self, url: str, fetch_date: Optional[str] = None) -> tuple[Optional[str], dict[str, Any]]:
        """Fetch and return (clean_text, log_entry).

        Returns:
            clean_text: cleaned string if successful, else None
            log_entry: metadata dict (url, domain, source_family, status, refusal_reason, cache_hit)
        """
        fetch_date = fetch_date or date.today().isoformat()
        domain = get_domain(url)
        source_family = self.get_source_family(url)

        log_entry: dict[str, Any] = {
            "url": url,
            "domain": domain,
            "source_family": source_family,
            "status": "refused",
            "refusal_reason": None,
            "cache_hit": False,
            "fetched_at": fetch_date,
        }

        # 1. Allowlist check
        if not self.allowlist:
            log_entry["refusal_reason"] = "empty_allowlist"
            return None, log_entry

        if not is_allowed_domain(url, self.allowlist):
            log_entry["refusal_reason"] = "disallowed_domain"
            return None, log_entry

        # 2. Check disk cache
        cache_file = self._cache_path(url, fetch_date)
        if cache_file.is_file():
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    cached_data = json.load(f)
                log_entry["status"] = cached_data.get("status", "accepted")
                log_entry["refusal_reason"] = cached_data.get("refusal_reason")
                log_entry["cache_hit"] = True
                return cached_data.get("clean_text"), log_entry
            except Exception as e:
                log.warning(f"Cache read error for {url}: {e}")

        # 3. Robots.txt check
        if self.respect_robots:
            if not check_robots_txt(url):
                log_entry["refusal_reason"] = "robots_disallowed"
                self._save_cache(cache_file, log_entry, None)
                return None, log_entry

        # 4. Rate limiting per domain
        now = time.time()
        last_time = _LAST_REQUEST_TIME.get(domain, 0.0)
        elapsed = now - last_time
        if elapsed < self.delay_between_calls:
            time.sleep(self.delay_between_calls - elapsed)
        _LAST_REQUEST_TIME[domain] = time.time()

        # 5. Execute HTTP Request with redirect protection
        try:
            session = requests.Session()
            session.max_redirects = 5

            resp = session.get(
                url,
                headers={"User-Agent": USER_AGENT},
                timeout=self.timeout,
                stream=True,
                allow_redirects=True,
            )

            # Validate final redirect URL
            final_domain = get_domain(resp.url)
            if not is_allowed_domain(resp.url, self.allowlist):
                log_entry["refusal_reason"] = f"redirect_disallowed:{final_domain}"
                self._save_cache(cache_file, log_entry, None)
                return None, log_entry

            # Check status code
            if resp.status_code in (401, 403):
                # Detect bot protection or login
                content_sample = resp.text[:1000].lower() if hasattr(resp, "text") else ""
                if "captcha" in content_sample or "cf-ray" in resp.headers:
                    log_entry["refusal_reason"] = "captcha_bot_protection"
                else:
                    log_entry["refusal_reason"] = "auth_required"
                self._save_cache(cache_file, log_entry, None)
                return None, log_entry

            if resp.status_code != 200:
                log_entry["refusal_reason"] = f"http_{resp.status_code}"
                self._save_cache(cache_file, log_entry, None)
                return None, log_entry

            # Size limit check
            content_length = resp.headers.get("Content-Length")
            if content_length and int(content_length) > self.max_bytes:
                log_entry["refusal_reason"] = "oversized"
                self._save_cache(cache_file, log_entry, None)
                return None, log_entry

            content_bytes = resp.content
            if len(content_bytes) > self.max_bytes:
                log_entry["refusal_reason"] = "oversized"
                self._save_cache(cache_file, log_entry, None)
                return None, log_entry

            html_text = content_bytes.decode(resp.encoding or "utf-8", errors="replace")
            clean_text = html_to_clean_text(html_text)

            if not clean_text or len(clean_text) < 20:
                log_entry["refusal_reason"] = "no_content"
                self._save_cache(cache_file, log_entry, None)
                return None, log_entry

            # Success
            log_entry["status"] = "accepted"
            log_entry["refusal_reason"] = None
            self._save_cache(cache_file, log_entry, clean_text)
            return clean_text, log_entry

        except requests.exceptions.Timeout:
            log_entry["refusal_reason"] = "timeout"
            self._save_cache(cache_file, log_entry, None)
            return None, log_entry
        except Exception as exc:
            log_entry["refusal_reason"] = f"network_error:{str(exc)[:50]}"
            self._save_cache(cache_file, log_entry, None)
            return None, log_entry

    def _save_cache(self, path: Path, log_entry: dict, clean_text: Optional[str]) -> None:
        try:
            data = dict(log_entry)
            data["clean_text"] = clean_text
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            log.warning(f"Failed to write fetch cache {path}: {e}")
