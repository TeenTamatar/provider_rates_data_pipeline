#!/usr/bin/env python3
"""download_c5_data.py – Download every data file required for the C5 stage.

Run this ONCE from the provider_rates_data_pipeline/ directory before running
the pipeline. All files are placed under Data/ (next to this script),
which is the path the pipeline expects.

Usage:
    python download_c5_data.py               # download everything missing
    python download_c5_data.py --check       # only report which files are present/absent
    python download_c5_data.py --force       # re-download even if already present
    python download_c5_data.py --file nppes  # download only the named group

Files downloaded
────────────────
  [nppes]  NPPES main ZIP (~1.1 GB compressed / 11 GB unzipped) → V2 dir
               npidata_pfile_20050523-20261011.csv
               pl_pfile_20050523-20261011.csv
               endpoint_pfile_20050523-20261011.csv
               othername_pfile_20050523-20261011.csv   (bonus; in ZIP)

  [deact]  NPPES Deactivated NPI Report ZIP (~2.6 MB)
               NPPES Deactivated NPI Report 20261012.xlsx

  [dac]    CMS Doctors & Clinicians national file (~500 MB CSV)
               DAC_NationalDownloadableFile.csv

  [nucc]   NUCC Provider Taxonomy v26.1 (~520 KB CSV)
               nucc_taxonomy_261.csv

  [rates]  Centene rate files (~700 MB + ~240 MB)
               rate_files/2026-09-30_centene-management-company-llc_healthnet-ca_in-network.json
               rate_files/2026-09-28_centene-management-company-llc_ambetter-ca_in-network.json

NOTE: The NPPES main ZIP is ~1.1 GB compressed (11 GB unzipped). Download + unzip
takes 5–20 minutes depending on network speed. Progress is shown for every file.
This script uses the system `curl` binary to avoid macOS Python SSL certificate
issues; curl uses the macOS Keychain / system trust store.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Optional

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent.resolve()
DATA_DIR   = SCRIPT_DIR / "Data"
NPPES_DIR  = DATA_DIR / "NPPES_Data_Dissemination_October_2026_V2"
RATE_DIR   = DATA_DIR / "rate_files"
CACHE_DIR  = DATA_DIR / ".download_cache"

# ── Confirmed download URLs (HTTP 200 as of 2026-10-17) ──────────────────────
# NPPES Data Dissemination October 2026 V2 – full monthly replacement ZIP (~1.1 GB)
NPPES_MAIN_ZIP_URL = (
    "https://download.cms.gov/nppes/NPPES_Data_Dissemination_October_2026_V2.zip"
)
# NPPES Deactivated NPI Report – October 2026 ZIP (~2.6 MB)
NPPES_DEACT_ZIP_URL = (
    "https://download.cms.gov/nppes/NPPES_Deactivated_NPI_Report_101226_V2.zip"
)
# CMS Doctors & Clinicians national downloadable file – dataset mj5m-pzi6
# Direct file URL retrieved from the CMS metastore API 2026-10-17
CMS_DAC_CSV_URL = (
    "https://data.cms.gov/provider-data/sites/default/files/resources/"
    "52c3f098d7e56028a298fd297cb0b38d_1790972133/DAC_NationalDownloadableFile.csv"
)
# NUCC Provider Taxonomy Code Set v26.1 CSV (direct link from nucc.org)
NUCC_TAXONOMY_URL = (
    "https://nucc.org/images/stories/CSV/nucc_taxonomy_261.csv"
)
# CMS reference files for C3 (Medicare PFS RVU26D and the HCPCS alpha-numeric list)
CMS_RVU_ZIP_URL   = "https://www.cms.gov/files/zip/rvu26d-updated-08-26-2026.zip"
CMS_HCPCS_ZIP_URL = "https://www.cms.gov/files/zip/october-2026-alpha-numeric-hcpcs-file.zip"
# Centene rate files (C2b, C3, C4 – also needed if running full pipeline)
HEALTHNET_RATE_URL = (
    "https://www.centene.com/content/dam/centene/Centene%20Corporate/json/DOCUMENT/"
    "2026-09-30_centene-management-company-llc_healthnet-ca_in-network.json"
)
AMBETTER_RATE_URL = (
    "https://www.centene.com/content/dam/centene/Centene%20Corporate/json/DOCUMENT/"
    "2026-09-28_centene-management-company-llc_ambetter-ca_in-network.json"
)

# ── Expected file specs (local_path, description, min_bytes) ─────────────────
FILES = {
    "nppes_main": (
        NPPES_DIR / "npidata_pfile_20050523-20261011.csv",
        "NPPES main NPI file (11 GB unzipped)",
        5_000_000_000,
    ),
    "nppes_pl": (
        NPPES_DIR / "pl_pfile_20050523-20261011.csv",
        "NPPES secondary locations (117 MB)",
        50_000_000,
    ),
    "nppes_ep": (
        NPPES_DIR / "endpoint_pfile_20050523-20261011.csv",
        "NPPES endpoint file (121 MB)",
        50_000_000,
    ),
    "deact": (
        DATA_DIR / "NPPES Deactivated NPI Report 20261012.xlsx",
        "NPPES deactivated NPI report (~5.4 MB)",
        500_000,
    ),
    "dac": (
        DATA_DIR / "DAC_NationalDownloadableFile.csv",
        "CMS Doctors & Clinicians national file (~500 MB)",
        100_000_000,
    ),
    "nucc": (
        DATA_DIR / "nucc_taxonomy_261.csv",
        "NUCC provider taxonomy v26.1 (~520 KB)",
        100_000,
    ),
    "healthnet_rate": (
        RATE_DIR / "2026-09-30_centene-management-company-llc_healthnet-ca_in-network.json",
        "Health Net CA rate file (~702 MB)",
        500_000_000,
    ),
    "ambetter_rate": (
        RATE_DIR / "2026-09-28_centene-management-company-llc_ambetter-ca_in-network.json",
        "Ambetter CA rate file (~240 MB)",
        100_000_000,
    ),
}


# ── Utilities ─────────────────────────────────────────────────────────────────

def log(msg: str, *, prefix: str = "→") -> None:
    print(f"  {prefix} {msg}", flush=True)


def file_ok(path: Path, min_bytes: int) -> bool:
    return path.exists() and path.stat().st_size >= min_bytes


def curl_download(url: str, dest: Path, desc: str) -> bool:
    """Download url → dest using the system curl binary (avoids macOS SSL issues).
    Shows a live progress bar. Returns True on success.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    log(f"downloading: {desc}")
    log(f"  from: {url}")
    log(f"    to: {dest}")
    cmd = [
        "curl",
        "--location",            # follow redirects
        "--progress-bar",        # terse progress bar
        "--retry", "3",
        "--retry-delay", "5",
        "--retry-connrefused",
        "--connect-timeout", "30",
        "--max-time", "7200",    # 2-hour hard cap per file
        "--output", str(tmp),
        "--user-agent", "Mozilla/5.0 (compatible; HiLabs-C5-pipeline/1.0)",
        url,
    ]
    try:
        result = subprocess.run(cmd)
        if result.returncode != 0:
            log(f"curl exited with code {result.returncode}", prefix="✗")
            if tmp.exists():
                tmp.unlink()
            return False
        tmp.rename(dest)
        size_mb = dest.stat().st_size / 1_048_576
        log(f"saved {size_mb:,.1f} MB → {dest.name}", prefix="✓")
        return True
    except FileNotFoundError:
        log("curl not found. Install with: brew install curl", prefix="✗")
        return False
    except Exception as exc:
        log(f"unexpected error: {exc}", prefix="✗")
        if tmp.exists():
            tmp.unlink()
        return False


def extract_zip(zip_path: Path, dest_dir: Path) -> bool:
    """Extract all members of zip_path into dest_dir (flattened, no subdirs). Returns True."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    log(f"extracting {zip_path.name} → {dest_dir}")
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            members = zf.namelist()
            log(f"  archive contains {len(members)} file(s)")
            for member in members:
                # Skip directories and __MACOSX metadata
                if member.endswith("/") or "__MACOSX" in member:
                    continue
                target = dest_dir / Path(member).name  # flatten path
                if target.exists():
                    log(f"  skip (exists): {target.name}")
                    continue
                log(f"  extracting: {Path(member).name}")
                with zf.open(member) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
        log("extraction complete", prefix="✓")
        return True
    except zipfile.BadZipFile as exc:
        log(f"bad ZIP file ({exc}); the download may be incomplete", prefix="✗")
        return False
    except Exception as exc:
        log(f"extraction failed: {exc}", prefix="✗")
        return False


def download_zip_and_extract(url: str, dest_dir: Path, desc: str,
                              cache_zip: Optional[Path] = None) -> bool:
    """Download a ZIP (or reuse cache) then extract to dest_dir."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    zip_name = Path(url.split("?")[0]).name  # strip query string
    zip_path = cache_zip or (CACHE_DIR / zip_name)

    if zip_path.exists() and zip_path.stat().st_size > 100_000:
        log(f"ZIP already cached: {zip_path.name} ({zip_path.stat().st_size / 1_048_576:.0f} MB)", prefix="↩")
    else:
        ok = curl_download(url, zip_path, f"{desc} (ZIP)")
        if not ok:
            return False

    return extract_zip(zip_path, dest_dir)


# ── Group downloaders ─────────────────────────────────────────────────────────

def download_nppes(force: bool) -> bool:
    """NPPES main dissemination ZIP → npidata, pl, endpoint CSVs."""
    needed = [
        (FILES["nppes_main"][0], FILES["nppes_main"][2]),
        (FILES["nppes_pl"][0],   FILES["nppes_pl"][2]),
        (FILES["nppes_ep"][0],   FILES["nppes_ep"][2]),
    ]
    if not force and all(file_ok(p, m) for p, m in needed):
        log("NPPES main, pl and endpoint files already present", prefix="✓")
        return True

    print(f"\n  ── NPPES Data Dissemination October 2026 V2 ──")
    log("This is ~1.1 GB download that expands to ~11 GB. Please be patient.")
    ok = download_zip_and_extract(
        url=NPPES_MAIN_ZIP_URL,
        dest_dir=NPPES_DIR,
        desc="NPPES Data Dissemination October 2026 V2",
        cache_zip=CACHE_DIR / "NPPES_Data_Dissemination_October_2026_V2.zip",
    )
    if not ok:
        return False

    # Verify the three critical files landed
    missing = [p for p, m in needed if not file_ok(p, m)]
    if missing:
        log("WARNING: expected files not found after extraction:", prefix="⚠")
        for p in missing:
            log(f"  {p.name}", prefix="⚠")
        log("The ZIP may use different internal filenames. Check NPPES_DIR contents.", prefix="⚠")
        _list_dir(NPPES_DIR)
    return True


def download_deact(force: bool) -> bool:
    """NPPES Deactivated NPI Report ZIP → XLSX."""
    path, desc, min_bytes = FILES["deact"]
    if not force and file_ok(path, min_bytes):
        log(f"Deactivated NPI report already present ({path.stat().st_size / 1024:.0f} KB)", prefix="✓")
        return True

    print(f"\n  ── NPPES Deactivated NPI Report ──")
    cache_zip = CACHE_DIR / "NPPES_Deactivated_NPI_Report_101226_V2.zip"
    ok = download_zip_and_extract(
        url=NPPES_DEACT_ZIP_URL,
        dest_dir=DATA_DIR,
        desc=desc,
        cache_zip=cache_zip,
    )
    if not ok:
        return False

    # Rename whatever XLSX landed to the expected path
    if not path.exists():
        candidates = (
            sorted(DATA_DIR.glob("*[Dd]eactivat*.xlsx")) +
            sorted(DATA_DIR.glob("*[Dd]eactivat*.XLSX")) +
            sorted(DATA_DIR.glob("NPPES*Deact*.xlsx"))
        )
        if candidates:
            candidates[0].rename(path)
            log(f"renamed to: {path.name}", prefix="✓")
        else:
            log("Could not locate XLSX in deact ZIP; check Data/ for the file", prefix="⚠")
            _list_dir(DATA_DIR, pattern="*.xlsx")
    return True


def download_dac(force: bool) -> bool:
    """CMS Doctors & Clinicians national file (direct CSV download)."""
    path, desc, min_bytes = FILES["dac"]
    if not force and file_ok(path, min_bytes):
        log(f"DAC file already present ({path.stat().st_size / 1_048_576:.0f} MB)", prefix="✓")
        return True

    print(f"\n  ── CMS Doctors & Clinicians (DAC) national file ──")
    log("Dataset ID: mj5m-pzi6 | Modified: 2026-10-02")
    return curl_download(CMS_DAC_CSV_URL, path, desc)


def download_nucc(force: bool) -> bool:
    """NUCC provider taxonomy CSV."""
    path, desc, min_bytes = FILES["nucc"]
    if not force and file_ok(path, min_bytes):
        log(f"NUCC taxonomy already present ({path.stat().st_size / 1024:.0f} KB)", prefix="✓")
        return True

    print(f"\n  ── NUCC Provider Taxonomy v26.1 ──")
    ok = curl_download(NUCC_TAXONOMY_URL, path, desc)
    if not ok:
        log("primary nucc.org URL failed — the site may require browser JS.", prefix="⚠")
        log("Alternative: download manually from https://www.nucc.org/index.php/"
            "code-sets-mainmenu-41/provider-taxonomy-mainmenu-40/csv-mainmenu-57", prefix="⚠")
    return ok


def download_cms_ref(force: bool) -> bool:
    """CMS PFS RVU26D and HCPCS ZIPs -> Data/cms_ref/rvu26d/ and Data/cms_ref/hcpcs_2026_10/ (C3)."""
    ok = True
    for url, sub in [(CMS_RVU_ZIP_URL, "rvu26d"), (CMS_HCPCS_ZIP_URL, "hcpcs_2026_10")]:
        dest = DATA_DIR / "cms_ref" / sub
        if not force and dest.exists() and any(dest.iterdir()):
            log(f"cms_ref/{sub} already present", prefix="✓")
            continue
        print(f"\n  ── CMS reference: {sub} ──")
        ok = download_zip_and_extract(url, dest, f"CMS {sub}") and ok
    return ok


def download_rates(force: bool) -> bool:
    """Centene Health Net CA and Ambetter CA rate files (needed for full pipeline)."""
    hn_path,  hn_desc,  hn_min  = FILES["healthnet_rate"]
    amb_path, amb_desc, amb_min = FILES["ambetter_rate"]

    all_ok = True
    RATE_DIR.mkdir(parents=True, exist_ok=True)

    print(f"\n  ── Centene Rate Files ──")
    if not force and file_ok(hn_path, hn_min):
        log(f"Health Net CA rate file already present ({hn_path.stat().st_size / 1_048_576:.0f} MB)", prefix="✓")
    else:
        ok = curl_download(HEALTHNET_RATE_URL, hn_path, hn_desc)
        if ok:
            actual = hn_path.stat().st_size
            expected = 701_615_514
            if abs(actual - expected) > 10_000_000:
                log(f"size mismatch: got {actual:,} bytes, expected ~{expected:,}", prefix="⚠")
        all_ok = all_ok and ok

    if not force and file_ok(amb_path, amb_min):
        log(f"Ambetter CA rate file already present ({amb_path.stat().st_size / 1_048_576:.0f} MB)", prefix="✓")
    else:
        ok = curl_download(AMBETTER_RATE_URL, amb_path, amb_desc)
        if ok:
            actual = amb_path.stat().st_size
            expected = 240_380_888
            if abs(actual - expected) > 10_000_000:
                log(f"size mismatch: got {actual:,} bytes, expected ~{expected:,}", prefix="⚠")
        all_ok = all_ok and ok

    return all_ok


# ── Helpers ───────────────────────────────────────────────────────────────────

def _list_dir(directory: Path, pattern: str = "*") -> None:
    files = list(directory.glob(pattern))
    log(f"Contents of {directory}:", prefix="ℹ")
    for f in sorted(files)[:20]:
        log(f"  {f.name}  ({f.stat().st_size / 1024:.0f} KB)", prefix=" ")


# ── Check mode ────────────────────────────────────────────────────────────────

def check_all() -> None:
    print(f"\n  C5 Data File Status  (Data dir: {DATA_DIR})")
    print("  " + "─" * 70)
    header = f"  {'Group':14s}  {'Status':28s}  {'Path'}"
    print(header)
    print("  " + "─" * 70)
    all_present = True
    for key, (path, desc, min_bytes) in FILES.items():
        if file_ok(path, min_bytes):
            size_mb = path.stat().st_size / 1_048_576
            label = f"✓  {size_mb:>8,.0f} MB"
        else:
            label = "✗  MISSING"
            all_present = False
        rel = str(path).replace(str(DATA_DIR.parent) + "/", "")
        print(f"  {key:14s}  {label:28s}  {rel}")
    print("  " + "─" * 70)
    if all_present:
        print("  All files present. Pipeline ready to run.")
    else:
        print("  Some files missing. Run without --check to download them.")
    print()


# ── Main ──────────────────────────────────────────────────────────────────────

GROUP_FUNCS = {
    "nppes": download_nppes,
    "deact": download_deact,
    "dac":   download_dac,
    "nucc":  download_nucc,
    "cms_ref": download_cms_ref,
    "rates": download_rates,
}

C5_CORE_GROUPS = ["nppes", "deact", "dac", "nucc"]   # strictly needed for C5


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--check", action="store_true",
                    help="report file presence without downloading anything")
    ap.add_argument("--force", action="store_true",
                    help="re-download even if files are already present")
    ap.add_argument("--file",
                    choices=list(GROUP_FUNCS.keys()),
                    metavar="GROUP",
                    help=f"download only one group: {', '.join(GROUP_FUNCS)}")
    ap.add_argument("--all", dest="all_groups", action="store_true",
                    help="download all groups including Centene rate files")
    args = ap.parse_args()

    print(f"\n  C5 Data Downloader")
    print(f"  ─────────────────────────────────────────────────")
    print(f"  Script dir : {SCRIPT_DIR}")
    print(f"  Data dir   : {DATA_DIR}")
    print(f"  Cache dir  : {CACHE_DIR}")
    print()

    if args.check:
        check_all()
        return

    # Determine which groups to run
    if args.file:
        groups = [args.file]
    elif args.all_groups:
        groups = list(GROUP_FUNCS.keys())
    else:
        groups = C5_CORE_GROUPS   # default: only what C5 strictly needs

    print(f"  Groups to download: {', '.join(groups)}")

    results: dict[str, bool] = {}
    t0 = time.time()

    for group in groups:
        print(f"\n{'═'*66}")
        print(f"  [{group.upper()}]")
        results[group] = GROUP_FUNCS[group](force=args.force)

    elapsed = time.time() - t0
    print(f"\n{'═'*66}")
    print(f"\n  Summary ({elapsed / 60:.1f} min elapsed):")
    any_failed = False
    for group, ok in results.items():
        icon = "✓" if ok else "✗"
        print(f"    {icon}  {group}")
        if not ok:
            any_failed = True

    print()
    if not any_failed:
        print("  All downloads complete.\n")
        print("  Run a status check:")
        print("    python download_c5_data.py --check\n")
        print("  Then run the pipeline:")
        print("    python pipeline/c5_tin_npi_address.py\n")
    else:
        print("  Some downloads failed. Check errors above and retry.\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
