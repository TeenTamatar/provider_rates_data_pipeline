"""pipeline/c5/common.py – Shared config loading, logging, schemas, and helpers.

All C5 modules import from here so thresholds live only in config/c5.yaml.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import yaml

# ── Repo layout ───────────────────────────────────────────────────────────────
PIPELINE_DIR = Path(__file__).parent.parent.parent.resolve()   # provider_rates_data_pipeline/
REPO_ROOT    = PIPELINE_DIR.parent                            # HiLabs/
CONFIG_PATH  = PIPELINE_DIR / "config" / "c5.yaml"


# ── Logging ───────────────────────────────────────────────────────────────────
def get_logger(name: str = "c5") -> logging.Logger:
    """Return a logger that prints timestamped messages to stderr."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                                               datefmt="%H:%M:%S"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return logger


# ── Config loading ────────────────────────────────────────────────────────────
def load_config(path: Path = CONFIG_PATH) -> dict:
    """Load config/c5.yaml, resolve paths relative to repo root, inject run_date."""
    # Load environment variables from .env if present
    env_file = PIPELINE_DIR / ".env"
    if env_file.is_file():
        try:
            import dotenv
            dotenv.load_dotenv(env_file)
        except Exception:
            pass

    with open(path) as f:
        cfg = yaml.safe_load(f)

    # Inject run_date if not set
    if not cfg.get("run_date"):
        cfg["run_date"] = date.today().isoformat()

    # Resolve all path values to absolute strings
    paths = cfg.get("paths", {})
    for key, rel in paths.items():
        abs_path = (PIPELINE_DIR / rel).resolve()
        paths[key] = str(abs_path)

    cfg["paths"] = paths
    return cfg


def config_hash(cfg: dict) -> str:
    """SHA-256 of the canonical JSON representation of the config."""
    canonical = json.dumps(cfg, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def write_config_snapshot(cfg: dict, out_path: str) -> None:
    """Write config + hash to a YAML snapshot file."""
    snap = dict(cfg)
    snap["_hash"] = config_hash(cfg)
    snap["_written_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(out_path, "w") as f:
        yaml.safe_dump(snap, f, default_flow_style=False, allow_unicode=True)


# ── Path helpers ──────────────────────────────────────────────────────────────
def resolve(cfg: dict, key: str) -> Path:
    """Return the absolute Path for a config path key."""
    return Path(cfg["paths"][key])


def require_file(path: str | Path, label: str) -> Path:
    """Return Path if file exists, else raise FileNotFoundError with a clear message."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"Required input not found [{label}]: {p}")
    return p


# ── Identifier helpers ────────────────────────────────────────────────────────
def fmt_npi(npi: Any) -> str:
    """Return NPI as a zero-padded 10-character string."""
    return str(npi).strip().zfill(10)


def fmt_tin(tin: Any) -> str:
    """Return TIN as 'XX-XXXXXXX' (with dash).  Input may be 9 digits or already dashed."""
    s = str(tin).strip()
    s = re.sub(r"[^0-9]", "", s)   # strip non-digits
    if len(s) == 9:
        return f"{s[:2]}-{s[2:]}"
    return str(tin).strip()        # already formatted or unusual; return as-is


# ── County extraction ─────────────────────────────────────────────────────────
_COUNTY_RE = re.compile(r"\b(?:PPO|DIRECT NETWORK)[:\s]+(.+?)\s+County\b", re.I)


def extract_county_label(business_name: str | None) -> str:
    """Pull 'Sacramento' from 'PPO: Sacramento County'.  Returns '' if not found."""
    m = _COUNTY_RE.search(business_name or "")
    return m.group(1).strip().lower() if m else ""


# ── Source family mapping ─────────────────────────────────────────────────────
def source_to_family(source: str, cfg: dict) -> str:
    """Map a claim source string to its canonical family name.
    Only different families count as independent evidence.
    """
    for family, sources in cfg.get("source_families", {}).items():
        if source in sources:
            return family
    # Fallback: use the source itself (counts as its own family)
    return source


# ── PO box detection ──────────────────────────────────────────────────────────
_PO_BOX_RE = re.compile(
    r"\b(P\.?\s*O\.?\s*BOX|POST\s*OFFICE\s*BOX|PO\s+BOX)\b", re.I
)


def is_po_box(addr: str | None) -> bool:
    return bool(_PO_BOX_RE.search(addr or ""))


# ── Output schemas (column order contracts) ───────────────────────────────────
SCHEMA_TARGETS = [
    "tin", "npi", "business_name", "entity_type", "specialty_code",
    "county_label", "ppo_scope", "n_ppo_codes",
]

SCHEMA_CLAIMS = [
    "claim_id", "npi", "tin", "raw_address", "source", "source_family",
    "source_date", "claim_type", "is_po_box", "is_billing_only",
    "npi_status", "addr1", "addr2", "city", "state", "zip",
]

SCHEMA_SITES = [
    "site_id", "normalized_address", "address_line", "unit", "city",
    "state", "zip5", "lat", "lon", "county", "geocode_matched",
    "geocode_quality",
]

SCHEMA_TIN_SITES = [
    "tin", "site_id", "evidence_types", "n_supporting_npis",
]

SCHEMA_CANDIDATES = [
    "tin", "npi", "site_id", "address", "address_line", "unit", "city", "state",
    "zip5", "lat", "lon", "county", "county_fips",
    "n_independent_families", "families",
    "s_families", "s_tin_fit", "s_freshness", "s_geocode",
    "s_county", "s_in_state",
    "score", "rank",
    "freshest_source_date", "newest_source_date", "evidence", "claim_ids", "ppo_scope",
]

SCHEMA_STALE = [
    "claim_id", "npi", "site_id", "reason", "contradicting_claim_id", "distance_km",
]

SCHEMA_ADDRESSES = [
    "tin", "npi", "address", "address_line", "unit", "city", "state", "zip5",
    "lat", "lon", "county", "county_fips", "site_id",
    "label", "score",
    "n_independent_families", "newest_source_date", "freshest_source_date",
    "source_list", "evidence", "claim_ids", "ppo_scope",
]

SCHEMA_LEFTOVERS = [
    "tin", "npi", "business_name", "entity_type", "specialty_code",
    "tin_size", "tin_priority", "ppo_scope", "best_candidate_address", "reason_unverified",
    "lookup_url", "lookup_url_method", "lookup_address", "lookup_source",
]

SCHEMA_LOOKUP_LOG = [
    "url", "domain", "source_family", "status", "refusal_reason", "cache_hit", "fetched_at",
]

SCHEMA_LLM_LOG = [
    "target_or_tin", "prompt_version", "status", "reason_code", "tokens", "call_time",
]

SCHEMA_DOMAIN_PROBE = [
    "domain", "url", "status", "detail", "probe_date",
]

SCHEMA_EXTRACTION_LOG = [
    "page_id", "url", "domain", "content_hash", "tier_resolved",
    "targets_in_page", "targets_resolved", "llm_called", "input_chars", "reject_reasons",
]

SCHEMA_DOMAIN_YIELD = [
    "domain", "pages", "tier0_hits", "tier1_hits", "llm_calls", "accepted", "accept_rate", "stopped_reason",
]

SCHEMA_SUGGESTED_DOMAINS = [
    "domain", "n_urls", "n_targets_covered", "example_url",
]


def count_independent_families(families: list[str]) -> int:
    """Calculate the number of truly independent source families.

    Rules:
    - nppes_self and nppes_org count together under the NPPES family umbrella.
    - web_aggregator never counts as independent of NPPES (aggregators commonly copy NPPES).
    - payer counts as its own family.
    - web (organization websites) counts as its own family.
    - medicare (CMS DAC) counts as its own family.
    """
    fams = set(f for f in families if f)
    has_nppes = bool(fams & {"nppes_self", "nppes_org"})

    independent_groups = set()
    for f in fams:
        if f in {"nppes_self", "nppes_org"}:
            independent_groups.add("nppes")
        elif f == "web_aggregator":
            if not has_nppes:
                independent_groups.add("web_aggregator")
        else:
            independent_groups.add(f)

    return len(independent_groups)
