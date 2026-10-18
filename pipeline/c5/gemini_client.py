"""pipeline/c5/gemini_client.py – Google Gemini extraction backend.

Features:
- Official google-genai SDK (v2.31.0).
- Strict environment-variable-only API key handling (never logged or written).
- Structured JSON schema enforcement with Pydantic.
- Exponential backoff retry loop (separate retry counting).
- RPM rate limiting and call cap enforcement.
- Model, SDK version, prompt version recorded in disk cache.
- Pure external fetcher text only; no built-in web search or grounding.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import pydantic
from google import genai
from google.genai import types

from .common import get_logger

log = get_logger("c5.gemini")

SDK_VERSION = getattr(genai, "__version__", "2.31.0")
PROMPT_VERSION = "v1"


class ExtractedAddress(pydantic.BaseModel):
    provider_match: Optional[str] = None
    street: str
    unit: str = ""
    city: str
    state: str
    zip: str
    quote: str


class BatchedExtractedAddress(pydantic.BaseModel):
    target_key: str
    provider_match: Optional[str] = None
    street: str
    unit: str = ""
    city: str
    state: str
    zip: str
    quote: str


class GeminiExtractor:
    """Pluggable Gemini model backend for structured address extraction."""

    def __init__(self, cfg: dict, client: Optional[Any] = None):
        self.cfg = cfg
        self.enabled = bool(cfg.get("llm_enabled", False))
        self.provider = str(cfg.get("llm_provider", "gemini")).lower()
        if self.provider != "gemini":
            raise ValueError(f"Unsupported llm_provider '{self.provider}'. Only 'gemini' is supported.")

        self.model = str(cfg.get("llm_model", "gemini-2.5-flash"))
        self.temperature = float(cfg.get("llm_temperature", 0.0))
        self.call_cap = int(cfg.get("llm_call_cap", 300))
        self.max_retries = int(cfg.get("llm_max_retries", 3))
        self.rpm = int(cfg.get("llm_requests_per_minute", 15))
        self.delay_between_calls = 60.0 / max(self.rpm, 1)
        self.timeout = int(cfg.get("llm_timeout_seconds", 60))
        self.max_input_chars = int(cfg.get("llm_max_input_chars", 6000))

        self.cache_dir = Path(cfg.get("llm_cache_dir", "output/c5/llm_cache"))
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.calls_made = 0
        self.retries_count = 0
        self.last_call_time = 0.0

        # Load prompt templates
        prompt_path = cfg.get("paths", {}).get("prompt_template", "prompts/c5_extract_v1.txt")
        if not Path(prompt_path).is_file():
            prompt_path = Path(__file__).parent.parent.parent / "prompts" / "c5_extract_v1.txt"
        with open(prompt_path, "r", encoding="utf-8") as f:
            self.prompt_template = f.read()

        prompt_v2_path = cfg.get("paths", {}).get("prompt_template_v2", "prompts/c5_extract_v2.txt")
        if not Path(prompt_v2_path).is_file():
            prompt_v2_path = Path(__file__).parent.parent.parent / "prompts" / "c5_extract_v2.txt"
        if Path(prompt_v2_path).is_file():
            with open(prompt_v2_path, "r", encoding="utf-8") as f:
                self.prompt_template_v2 = f.read()
        else:
            self.prompt_template_v2 = ""

        # Initialize client
        if client is not None:
            self.client = client
        elif self.enabled:
            key_var = cfg.get("gemini_api_key_env", "GEMINI_API_KEY")
            api_key = os.environ.get(key_var)
            if not api_key:
                raise ValueError(
                    f"llm_enabled is True but API key not found in environment variable '{key_var}'. "
                    f"Please export {key_var} or update .env before running."
                )
            self.client = genai.Client(api_key=api_key)
        else:
            self.client = None

    def _cache_path(self, target_name: str, target_tin: str, page_text: str) -> Path:
        content_hash = hashlib.sha256(f"{target_name}|{target_tin}|{page_text}".encode()).hexdigest()
        return self.cache_dir / f"{self.model}_{PROMPT_VERSION}_{content_hash}.json"

    def _cache_path_v2(self, content_hash: str, sorted_keys: list[str]) -> Path:
        key = hashlib.sha256(f"{content_hash}|v2|{','.join(sorted_keys)}|{self.model}".encode()).hexdigest()
        return self.cache_dir / f"{self.model}_v2_{key}.json"

    def extract_addresses(
        self,
        target_name: str,
        target_tin: str,
        page_text: str,
        target_or_tin: str = "",
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Extract addresses for a target provider from page text (v1 single-target)."""
        target_or_tin = target_or_tin or f"{target_tin}:{target_name}"
        log_entry: dict[str, Any] = {
            "target_or_tin": target_or_tin,
            "prompt_version": PROMPT_VERSION,
            "status": "rejected",
            "reason_code": None,
            "tokens": 0,
            "call_time": datetime.utcnow().isoformat(),
        }

        # 1. Check disk cache
        cache_file = self._cache_path(target_name, target_tin, page_text)
        if cache_file.is_file():
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                log_entry["status"] = cached.get("status", "accepted")
                log_entry["reason_code"] = cached.get("reason_code")
                log_entry["tokens"] = cached.get("tokens", 0)
                log_entry["cache_hit"] = True
                return cached.get("addresses", []), log_entry
            except Exception as e:
                log.warning(f"Cache read error: {e}")

        # 2. Check call cap
        if self.calls_made >= self.call_cap:
            log.warning(f"LLM call cap reached ({self.call_cap}). Halting further model calls.")
            log_entry["reason_code"] = "call_cap_reached"
            return [], log_entry

        if not self.enabled or self.client is None:
            log_entry["reason_code"] = "llm_disabled"
            return [], log_entry

        # 3. Format prompt
        prompt = (
            self.prompt_template
            .replace("{target_name}", target_name)
            .replace("{target_tin}", target_tin)
            .replace("{page_text}", page_text[:100000])
        )

        config = types.GenerateContentConfig(
            temperature=self.temperature,
            response_mime_type="application/json",
            response_schema=list[ExtractedAddress],
        )

        # 4. Rate limit check
        now = time.time()
        elapsed = now - self.last_call_time
        if elapsed < self.delay_between_calls:
            time.sleep(self.delay_between_calls - elapsed)

        # 5. Execute with exponential backoff
        for attempt in range(self.max_retries + 1):
            try:
                self.last_call_time = time.time()
                self.calls_made += 1

                response = self.client.models.generate_content(
                    model=self.model,
                    contents=prompt,
                    config=config,
                )

                tokens = 0
                if hasattr(response, "usage_metadata") and response.usage_metadata:
                    tokens = getattr(response.usage_metadata, "total_token_count", 0) or 0
                log_entry["tokens"] = tokens

                raw_json = response.text
                if not raw_json or not raw_json.strip():
                    log_entry["reason_code"] = "empty_response"
                    self._save_cache(cache_file, log_entry, [])
                    return [], log_entry

                parsed = json.loads(raw_json)
                if not isinstance(parsed, list):
                    log_entry["reason_code"] = "malformed_json"
                    self._save_cache(cache_file, log_entry, [])
                    return [], log_entry

                validated_items = []
                for item in parsed:
                    valid_obj = ExtractedAddress.model_validate(item)
                    validated_items.append(valid_obj.model_dump())

                log_entry["status"] = "accepted"
                log_entry["reason_code"] = None
                self._save_cache(cache_file, log_entry, validated_items)
                return validated_items, log_entry

            except Exception as exc:
                if attempt < self.max_retries:
                    self.retries_count += 1
                    backoff = (2 ** attempt) * 1.5
                    log.warning(f"Gemini call error on attempt {attempt+1}/{self.max_retries+1}: {exc}. Backing off {backoff:.1f}s...")
                    time.sleep(backoff)
                else:
                    log.error(f"Gemini call failed after {self.max_retries+1} attempts: {exc}")
                    log_entry["reason_code"] = f"api_error:{type(exc).__name__}"
                    self._save_cache(cache_file, log_entry, [])
                    return [], log_entry

        return [], log_entry

    def extract_batched_addresses(
        self,
        targets: list[dict[str, str]],
        trimmed_page_text: str,
        content_hash: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Extract addresses for a batch of target providers from trimmed text (Tier 2)."""
        sorted_keys = sorted(t["target_key"] for t in targets)
        target_keys_str = ";".join(sorted_keys)
        log_entry: dict[str, Any] = {
            "target_or_tin": target_keys_str[:100],
            "prompt_version": "v2",
            "status": "rejected",
            "reason_code": None,
            "tokens": 0,
            "input_chars": len(trimmed_page_text),
            "call_time": datetime.utcnow().isoformat(),
        }

        # 1. Check disk cache
        cache_file = self._cache_path_v2(content_hash, sorted_keys)
        if cache_file.is_file():
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                log_entry["status"] = cached.get("status", "accepted")
                log_entry["reason_code"] = cached.get("reason_code")
                log_entry["tokens"] = cached.get("tokens", 0)
                log_entry["cache_hit"] = True
                return cached.get("addresses", []), log_entry
            except Exception as e:
                log.warning(f"Cache read error: {e}")

        # 2. Check call cap
        if self.calls_made >= self.call_cap:
            log.warning(f"LLM call cap reached ({self.call_cap}). Halting Tier 2 calls.")
            log_entry["reason_code"] = "call_cap_reached"
            return [], log_entry

        if not self.enabled or self.client is None:
            log_entry["reason_code"] = "llm_disabled"
            return [], log_entry

        # 3. Format prompt
        targets_json_str = json.dumps(targets, indent=2)
        prompt = (
            self.prompt_template_v2
            .replace("{targets_json}", targets_json_str)
            .replace("{page_text}", trimmed_page_text[:self.max_input_chars])
        )
        log_entry["input_chars"] = len(prompt)

        config = types.GenerateContentConfig(
            temperature=self.temperature,
            response_mime_type="application/json",
            response_schema=list[BatchedExtractedAddress],
        )

        # 4. Rate limit check
        now = time.time()
        elapsed = now - self.last_call_time
        if elapsed < self.delay_between_calls:
            time.sleep(self.delay_between_calls - elapsed)

        # 5. Execute with backoff
        for attempt in range(self.max_retries + 1):
            try:
                self.last_call_time = time.time()
                self.calls_made += 1

                response = self.client.models.generate_content(
                    model=self.model,
                    contents=prompt,
                    config=config,
                )

                tokens = 0
                if hasattr(response, "usage_metadata") and response.usage_metadata:
                    tokens = getattr(response.usage_metadata, "total_token_count", 0) or 0
                log_entry["tokens"] = tokens

                raw_json = response.text
                if not raw_json or not raw_json.strip():
                    log_entry["reason_code"] = "empty_response"
                    self._save_cache(cache_file, log_entry, [])
                    return [], log_entry

                parsed = json.loads(raw_json)
                if not isinstance(parsed, list):
                    log_entry["reason_code"] = "malformed_json"
                    self._save_cache(cache_file, log_entry, [])
                    return [], log_entry

                valid_keys = set(sorted_keys)
                validated_items = []
                for item in parsed:
                    if str(item.get("target_key")) not in valid_keys:
                        log.warning(f"Rejecting unknown target key {item.get('target_key')}")
                        continue
                    valid_obj = BatchedExtractedAddress.model_validate(item)
                    validated_items.append(valid_obj.model_dump())

                log_entry["status"] = "accepted" if validated_items else "rejected"
                log_entry["reason_code"] = None if validated_items else "no_addresses_found"
                self._save_cache(cache_file, log_entry, validated_items)
                return validated_items, log_entry

            except Exception as exc:
                if attempt < self.max_retries:
                    self.retries_count += 1
                    backoff = (2 ** attempt) * 1.5
                    log.warning(f"Tier 2 Gemini call error attempt {attempt+1}/{self.max_retries+1}: {exc}. Backing off {backoff:.1f}s...")
                    time.sleep(backoff)
                else:
                    log.error(f"Tier 2 Gemini call failed after {self.max_retries+1} attempts: {exc}")
                    log_entry["reason_code"] = f"api_error:{type(exc).__name__}"
                    self._save_cache(cache_file, log_entry, [])
                    return [], log_entry

        return [], log_entry

    def _save_cache(self, path: Path, log_entry: dict, addresses: list[dict]) -> None:
        try:
            data = dict(log_entry)
            data["addresses"] = addresses
            data["model_name"] = self.model
            data["sdk_version"] = SDK_VERSION
            data["prompt_version"] = log_entry.get("prompt_version", PROMPT_VERSION)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            log.warning(f"Failed to write LLM cache {path}: {e}")
