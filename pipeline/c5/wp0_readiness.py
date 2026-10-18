"""pipeline/c5/wp0_readiness.py – WP0: Setup and readiness check.

Validates that all input files are present, builds the sources manifest,
writes a config snapshot, and probes 5 known NPIs in every source.
Run this before any WP that produces data.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd
import yaml

from .common import (
    SCHEMA_TARGETS,
    config_hash,
    fmt_npi,
    get_logger,
    load_config,
    require_file,
    resolve,
    write_config_snapshot,
)

log = get_logger("c5.wp0")

# ── Probe NPIs (5 from healthnet-ca targets) ──────────────────────────────────
_PROBE_COUNT = 5


def _sha256(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while buf := f.read(chunk):
            h.update(buf)
    return h.hexdigest()


def _file_entry(label: str, path: str | None, skip_hash: bool = False) -> dict:
    """Build a manifest entry for one file."""
    if path is None or not os.path.isfile(path):
        return {"label": label, "path": path, "available": False}
    st = os.stat(path)
    entry = {
        "label": label,
        "path": path,
        "available": True,
        "size_bytes": st.st_size,
        "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(timespec="seconds"),
    }
    if not skip_hash:
        log.info(f"  computing SHA-256 for {label}…")
        entry["sha256"] = _sha256(path)
    else:
        entry["sha256"] = "(skipped – file > 1 GB)"
    return entry


def build_manifest(cfg: dict) -> dict:
    """Check all source paths and return a manifest dict."""
    p = cfg["paths"]
    sources = {
        "targets":    (p["targets"],      False),
        "nppes_main": (p["nppes_main"],   True),   # 11 GB → skip hash
        "nppes_pl":   (p["nppes_pl"],     True),
        "nppes_ep":   (p["nppes_ep"],     True),
        "nppes_deact":(p["nppes_deact"],  False),
        "cms_dac":    (p["cms_dac"],      True),
        "nucc":       (p["nucc_taxonomy"],False),
    }
    manifest = {}
    gaps = []
    for label, (path, skip_hash) in sources.items():
        entry = _file_entry(label, path, skip_hash=skip_hash)
        manifest[label] = entry
        if not entry["available"]:
            gaps.append(label)
            log.warning(f"  MISSING: {label} → {path}")
        else:
            mb = entry["size_bytes"] / 1_048_576
            log.info(f"  OK  {label}: {mb:.0f} MB  ({path})")

    manifest["_gaps"] = gaps
    manifest["_run_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return manifest


def probe_npis(cfg: dict, npis: list[str]) -> dict:
    """Look up 5 NPIs in each available source. Return a report dict."""
    p = cfg["paths"]
    report: dict[str, list] = {}

    quoted = ", ".join(f"'{n}'" for n in npis)

    def _duck(sql: str, label: str) -> list:
        try:
            con = duckdb.connect()
            rows = con.execute(sql).fetchall()
            con.close()
            return rows
        except Exception as exc:
            log.warning(f"  probe failed [{label}]: {exc}")
            return []

    # NPPES main
    if os.path.isfile(p["nppes_main"]):
        sql = f"""
          SELECT "NPI",
                 "Provider First Line Business Practice Location Address" AS addr1,
                 "Provider Business Practice Location Address City Name" AS city,
                 "Provider Business Practice Location Address State Name" AS state,
                 "Entity Type Code",
                 "Last Update Date"
          FROM read_csv_auto('{p["nppes_main"]}', header=True, sample_size=5000)
          WHERE CAST("NPI" AS VARCHAR) IN ({quoted})
          LIMIT 20
        """
        rows = _duck(sql, "nppes_main")
        report["nppes_main"] = [{"npi": r[0], "addr1": r[1], "city": r[2],
                                  "state": r[3], "entity_type": r[4], "update": r[5]}
                                 for r in rows]
        log.info(f"  nppes_main probe: {len(rows)} rows for {len(npis)} NPIs")

    # NPPES pl
    if os.path.isfile(p["nppes_pl"]):
        sql = f"""
          SELECT "NPI",
                 "Provider Secondary Practice Location Address- Address Line 1" AS addr1,
                 "Provider Secondary Practice Location Address - City Name" AS city
          FROM read_csv_auto('{p["nppes_pl"]}', header=True, sample_size=5000)
          WHERE CAST("NPI" AS VARCHAR) IN ({quoted})
          LIMIT 20
        """
        rows = _duck(sql, "nppes_pl")
        report["nppes_pl"] = [{"npi": r[0], "addr1": r[1], "city": r[2]} for r in rows]
        log.info(f"  nppes_pl probe: {len(rows)} rows")

    # NPPES endpoints
    if os.path.isfile(p["nppes_ep"]):
        sql = f"""
          SELECT "NPI",
                 "Affiliation Address Line One" AS addr1,
                 "Affiliation Address City" AS city
          FROM read_csv_auto('{p["nppes_ep"]}', header=True, sample_size=5000)
          WHERE CAST("NPI" AS VARCHAR) IN ({quoted})
          LIMIT 20
        """
        rows = _duck(sql, "nppes_ep")
        report["nppes_ep"] = [{"npi": r[0], "addr1": r[1], "city": r[2]} for r in rows]
        log.info(f"  nppes_ep probe: {len(rows)} rows")

    # CMS DAC
    if os.path.isfile(p["cms_dac"]):
        sql = f"""
          SELECT "NPI", "adr_ln_1", "City/Town", "State"
          FROM read_csv_auto('{p["cms_dac"]}', header=True, sample_size=5000)
          WHERE CAST("NPI" AS VARCHAR) IN ({quoted})
          LIMIT 20
        """
        rows = _duck(sql, "cms_dac")
        report["cms_dac"] = [{"npi": r[0], "addr1": r[1], "city": r[2], "state": r[3]}
                              for r in rows]
        log.info(f"  cms_dac probe: {len(rows)} rows")

    return report


def run_wp0(cfg: dict) -> dict:
    """Execute WP0. Returns the manifest dict."""
    log.info("=== WP0: Setup and Readiness ===")
    out = Path(cfg["paths"]["c5_targets"]).parent
    out.mkdir(parents=True, exist_ok=True)

    # Config snapshot
    snap_path = cfg["paths"]["config_snapshot"]
    write_config_snapshot(cfg, snap_path)
    chash = config_hash(cfg)
    log.info(f"Config snapshot written: {snap_path}  hash={chash[:12]}…")

    # Sources manifest
    manifest = build_manifest(cfg)
    manifest_path = cfg["paths"]["sources_manifest"]
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2, default=str)
    log.info(f"Sources manifest written: {manifest_path}")

    gaps = manifest.get("_gaps", [])
    if gaps:
        log.warning(f"Missing sources: {gaps}. Pipeline will produce empty tables for those.")
    else:
        log.info("All sources present.")

    # Probe 5 NPIs
    targets_path = cfg["paths"]["targets"]
    if os.path.isfile(targets_path):
        if str(targets_path).endswith(".parquet"):
            df = pd.read_parquet(targets_path, columns=["rate_file", "npi"])
        else:
            df = pd.read_csv(targets_path)
        df = df[df["rate_file"] == cfg["target_rate_file"]]
        sample_npis = [fmt_npi(n) for n in df["npi"].unique()[:_PROBE_COUNT]]
        log.info(f"Probing NPIs: {sample_npis}")
        probe_report = probe_npis(cfg, sample_npis)
        probe_path = str(Path(cfg["paths"]["c5_summary"]).parent / "c5_wp0_probe.json")
        with open(probe_path, "w") as f:
            json.dump(probe_report, f, indent=2, default=str)
        log.info(f"Probe report written: {probe_path}")
    else:
        log.warning("Targets file not available; skipping NPI probe.")

    log.info("WP0 DONE")
    return manifest
