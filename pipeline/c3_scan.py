"""C3 scan: stream every downloaded rate file once per section and count the checks C3 needs.

For each rate file listed in output/c1/c1_rate_files.csv and present in Data/rate_files/:

  pass 0  header scalars (reporting entity, last_updated_on, version, plan fields if any)
  pass 1  provider_references -> group and member tables; NPI, TIN, network_name and required-field checks
  pass 2  in_network          -> price table (one row per price x provider group); item, rate and price checks

Writes to output/c3/scan/:
  <label>_members.parquet  provider_group_id, tin_type, tin, npi
  <label>_groups.parquet   provider_group_id, network_name (;-joined), n_entries
  <label>_prices.parquet   item_index, billing_code_type, billing_code, additional_information,
                           negotiation_arrangement, negotiated_type, billing_class, setting, service_code,
                           billing_code_modifier, negotiated_rate, expiration_date, provider_group_id
  <label>_stats.json       every counter (numerators and denominators for the compliance metrics)

Nothing is expanded to one row per NPI x price. Rules: LOGIC.md section 3C.
Run pipeline/c3_reference.py first: the HCPCS check (M9) reads output/c3/ref/hcpcs.parquet.

Usage:
    python pipeline/c3_scan.py                     # all downloaded rate files
    python pipeline/c3_scan.py --only healthnet-ca # one file (label = brand-state part of the name)
"""
import argparse
import concurrent.futures as cf
import csv
import datetime as dt
import json
import os
import re
import sys
import time
from collections import Counter

import duckdb
import ijson

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_RATE_TABLE = os.path.join(ROOT, "output", "c1", "c1_rate_files.csv")
DEFAULT_RATE_DIR = os.path.join(ROOT, "Data", "rate_files")
DEFAULT_OUT = os.path.join(ROOT, "output", "c3", "scan")

# Required fields per declared schema version (CMS price-transparency-guide, tags v1.0.0 / v2.0.0).
REQ = {
    "1": {"root": ["reporting_entity_name", "reporting_entity_type", "last_updated_on", "in_network"],
          "ref": ["provider_group_id"], "provider": ["npi", "tin"], "tin": ["type", "value"],
          "item": ["negotiation_arrangement", "name", "billing_code_type", "billing_code_type_version",
                   "billing_code", "negotiated_rates", "description"],
          "rate": ["negotiated_prices"],
          "price": ["negotiated_type", "billing_class", "negotiated_rate", "expiration_date"]},
    "2": {"root": ["reporting_entity_name", "reporting_entity_type", "last_updated_on", "in_network", "version"],
          "ref": ["provider_group_id", "provider_groups", "network_name"], "provider": ["npi", "tin"],
          "tin": ["type", "value"],
          "item": ["negotiation_arrangement", "name", "billing_code_type", "billing_code_type_version",
                   "billing_code", "negotiated_rates", "description"],
          "rate": ["provider_references", "negotiated_prices"],
          "price": ["negotiated_type", "billing_class", "negotiated_rate", "expiration_date", "setting"]},
}
ENUM = {
    "negotiation_arrangement": {"ffs", "bundle", "capitation"},
    "billing_code_type": {"CPT", "HCPCS", "ICD", "MS-DRG", "R-DRG", "S-DRG", "APS-DRG", "AP-DRG", "APR-DRG",
                          "APC", "NDC", "HIPPS", "LOCAL", "EAPG", "CDT", "RC", "CSTM-ALL"},
    "negotiated_type": {"negotiated", "derived", "fee schedule", "percentage", "per diem"},
    "billing_class": {"1": {"professional", "institutional"}, "2": {"professional", "institutional", "both"}},
    "setting": {"inpatient", "outpatient", "both"},
    "tin.type": {"ein", "npi"},
    "plan_id_type": {"ein", "hios"},
    "plan_market_type": {"group", "individual"},
}
DOLLAR_TYPES = {"negotiated", "fee schedule", "derived", "per diem"}
# CMS place-of-service code set (cms.gov, read 2026-10-18): assigned codes only.
POS = {f"{i:02d}" for i in list(range(1, 28)) + [31, 32, 33, 34, 41, 42] + list(range(49, 59)) +
       [60, 61, 62, 65, 66, 71, 72, 81, 99]} | {"CSTM-00"}
# IRS valid EIN prefixes (irs.gov "How EINs are assigned and valid EIN prefixes", read 2026-10-18).
EIN_PREFIX = {1, 2, 3, 4, 5, 6, 10, 11, 12, 13, 14, 15, 16, 20, 21, 22, 23, 24, 25, 26, 27, 30, 31, 32, 33, 34, 35,
              36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48, 50, 51, 52, 53, 54, 55, 56, 57, 58, 59, 60, 61,
              62, 63, 64, 65, 66, 67, 68, 71, 72, 73, 74, 75, 76, 77, 80, 81, 82, 83, 84, 85, 86, 87, 88, 90, 91,
              92, 93, 94, 95, 98, 99}
CODE_RE = {
    "CPT": re.compile(r"^(\d{5}|\d{4}[FTU])$"),
    "HCPCS": re.compile(r"^([A-V]\d{4}|\d{5}|\d{4}[FTU])$"),  # Level II, or Level I (CPT) codes
    "RC": re.compile(r"^\d{4}$"),
    "MS-DRG": re.compile(r"^\d{3}$"), "R-DRG": re.compile(r"^\d{3}$"), "S-DRG": re.compile(r"^\d{3}$"),
    "APS-DRG": re.compile(r"^\d{3}$"), "AP-DRG": re.compile(r"^\d{3}$"), "APR-DRG": re.compile(r"^\d{3}(-?\d)?$"),
    "HIPPS": re.compile(r"^[A-Z0-9]{5}$"),
    "NDC": re.compile(r"^(\d{11}|\d{4,5}-\d{3,4}-\d{1,2})$"),
    "APC": re.compile(r"^\d{4,5}$"),
    "CDT": re.compile(r"^D\d{4}$"),
    "ICD": re.compile(r"^[A-Z0-9][A-Z0-9.]{2,7}$"),
}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
EIN_RE = re.compile(r"^(\d{2})-?(\d{7})$")
_date_ok = {}


def date_ok(s):
    if s not in _date_ok:
        ok = isinstance(s, str) and bool(DATE_RE.match(s))
        if ok:
            try:
                dt.date.fromisoformat(s)
            except ValueError:
                ok = False
        _date_ok[s] = ok
    return _date_ok[s]


def npi_valid(v):
    """CMS NPI rule: 10 digits, first digit 1 or 2, Luhn check digit with constant 24 (prefix 80840).
    The 15-digit card form 80840 + NPI is accepted. Returns 'zero', 'valid' or 'invalid'."""
    s = str(v).strip()
    if s.endswith(".0"):
        s = s[:-2]
    if s == "0":
        return "zero"
    if len(s) == 15 and s.startswith("80840"):
        s = s[5:]
    if len(s) != 10 or not s.isdigit() or s[0] not in "12":
        return "invalid"
    total = 24
    for i, ch in enumerate(reversed(s[:9])):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            d = d - 9 if d > 9 else d
        total += d
    return "valid" if (10 - total % 10) % 10 == int(s[9]) else "invalid"


def tin_valid(tin_type, value):
    """EIN: 9 digits (NN-NNNNNNN or NNNNNNNNN) with an IRS prefix; 8 digits = lost leading zero (padded,
    flagged). type npi: a valid NPI. Returns (status, normalized)."""
    s = str(value).strip()
    if tin_type == "npi":
        return ("valid" if npi_valid(s) == "valid" else "invalid"), s
    if s.isdigit() and len(s) == 8:
        s9, status = "0" + s, "padded"
    else:
        m = EIN_RE.match(s)
        if not m:
            return "invalid", s
        s9, status = m.group(1) + m.group(2), "valid"
    if s9 == "000000000" or int(s9[:2]) not in EIN_PREFIX:
        return "invalid", s
    return status, s9


def present(obj, key):
    v = obj.get(key)
    if v is None:
        return False
    if isinstance(v, str):
        return len(v) > 0
    if isinstance(v, list):
        return key == "network_name" or len(v) > 0  # network_name may be [] (no minItems in the schema)
    return True


def read_header(path):
    head = {}
    with open(path, "rb") as f:
        for prefix, event, value in ijson.parse(f):
            if prefix and "." not in prefix and event in ("string", "number", "boolean", "null"):
                head[prefix] = value
            elif prefix in ("provider_references", "in_network") and event == "start_array":
                head.setdefault("_first_array", prefix)
                break
    return {k: (str(v) if not isinstance(v, (str, int, float, type(None))) else v) for k, v in head.items()}


def scan(job):
    label, path, out_dir, run_date, hcpcs = job
    t0 = time.time()
    c = Counter()
    head = read_header(path)
    ver = str(head.get("version", "1")).split(".")[0] or "1"
    ver = ver if ver in REQ else "2"
    req = REQ[ver]
    c["root_objects"] = 1
    root_missing = [k for k in req["root"] if k not in ("in_network",) and not present(head, k)]
    c["root_complete"] = int(not root_missing and date_ok(head.get("last_updated_on")))
    for k in ("plan_id_type", "plan_market_type"):
        if k in head:
            c["enum_values"] += 1
            c["enum_valid"] += head[k] in ENUM[k]
    if head.get("plan_id_type") == "ein":
        c["cond_applies"] += 1
        c["cond_met"] += present(head, "plan_sponsor_name")

    # ---- pass 1: provider references
    gids = set()
    npi_cache = {}
    tin_cache = {}
    mpath = os.path.join(out_dir, f"{label}_members.csv")
    gpath = os.path.join(out_dir, f"{label}_groups.csv")
    with open(path, "rb") as src, open(mpath, "w", newline="", encoding="utf-8") as mf, \
            open(gpath, "w", newline="", encoding="utf-8") as gf:
        mw, gw = csv.writer(mf), csv.writer(gf)
        for ref in ijson.items(src, "provider_references.item", use_float=True):
            c["ref_objects"] += 1
            gid = ref.get("provider_group_id")
            ok = all(present(ref, k) for k in req["ref"]) and isinstance(gid, int) and \
                isinstance(ref.get("network_name", []), list)
            if ver == "1" and not (present(ref, "provider_groups") or present(ref, "location")):
                ok = False
            c["ref_complete"] += ok
            if ref.get("location") and not ref.get("provider_groups"):
                c["ref_remote_location"] += 1
            nn = [str(x) for x in (ref.get("network_name") or []) if str(x).strip()]
            c["groups_with_network_name"] += bool(nn)
            gids.add(gid)
            n_entries = 0
            for pg in ref.get("provider_groups") or []:
                n_entries += 1
                c["provider_objects"] += 1
                tin = pg.get("tin") or {}
                npis = pg.get("npi") if isinstance(pg.get("npi"), list) else []
                p_ok = present(pg, "npi") and present(pg, "tin") and all(isinstance(n, (int, float)) for n in npis)
                c["provider_complete"] += p_ok
                c["tin_objects"] += 1
                t_ok = all(present(tin, k) for k in req["tin"])
                c["tin_complete"] += t_ok
                ttype, tval = tin.get("type", ""), tin.get("value", "")
                c["enum_values"] += 1
                c["enum_valid"] += ttype in ENUM["tin.type"]
                if ttype == "ein":
                    c["cond_applies"] += 1
                    c["cond_met"] += present(tin, "business_name")
                key = (ttype, tval)
                if key not in tin_cache:
                    tin_cache[key] = tin_valid(ttype, tval)
                status, tnorm = tin_cache[key]
                c["tin_entries"] += 1
                c[f"tin_{status}"] += 1
                for n in npis:
                    if n not in npi_cache:
                        npi_cache[n] = npi_valid(n)
                    st = npi_cache[n]
                    c["npi_entries"] += 1
                    c[f"npi_{st}"] += 1
                    npi_text = str(int(n)) if isinstance(n, (int, float)) and st != "invalid" else str(n)
                    mw.writerow([gid, ttype, tnorm, npi_text])
            gw.writerow([gid, ";".join(nn), n_entries])
    c["distinct_npis"] = sum(1 for v in npi_cache.values() if v == "valid")
    c["distinct_tins"] = len({v[1] for v in tin_cache.values()})

    # ---- pass 2: in_network
    ppath = os.path.join(out_dir, f"{label}_prices.csv")
    code_items = Counter()
    with open(path, "rb") as src, open(ppath, "w", newline="", encoding="utf-8") as pf:
        pw = csv.writer(pf)
        for i, item in enumerate(ijson.items(src, "in_network.item", use_float=True)):
            c["item_objects"] += 1
            arr, ctype, code = item.get("negotiation_arrangement", ""), item.get("billing_code_type", ""), \
                str(item.get("billing_code", "")).strip()
            c["item_complete"] += all(present(item, k) for k in req["item"])
            for k, v in (("negotiation_arrangement", arr), ("billing_code_type", ctype)):
                c["enum_values"] += 1
                c["enum_valid"] += v in ENUM[k]
            rx = CODE_RE.get(ctype)
            code_ok = bool(code) and (rx.match(code) if rx else True)
            if code_ok and ctype == "HCPCS" and code[0].isalpha():
                code_ok = code in hcpcs
            c["items_code_valid"] += bool(code_ok and present(item, "description"))
            if not code_ok:
                c[f"invalid_code_{ctype}"] += 1
            code_items[(ctype, code, arr)] += 1
            if arr in ("bundle", "capitation"):
                c["guide_list_applies"] += 1
                c["guide_list_met"] += present(item, "bundled_codes" if arr == "bundle" else "covered_services")
            for rate in item.get("negotiated_rates") or []:
                c["rate_objects"] += 1
                refs = rate.get("provider_references") or []
                inline = rate.get("provider_groups") or []
                r_ok = all(present(rate, k) for k in req["rate"]) if ver == "2" else \
                    (present(rate, "negotiated_prices") and (bool(refs) or bool(inline)))
                c["rate_complete"] += r_ok
                for g in refs:
                    c["refs_used"] += 1
                    c["refs_resolved"] += g in gids
                targets = list(refs)
                if inline:
                    c["inline_group_sets"] += 1
                    targets.append(-1)  # inline providers: kept as one pseudo group per file (rare)
                for price in rate.get("negotiated_prices") or []:
                    c["price_objects"] += 1
                    ntype, bclass, setting = price.get("negotiated_type", ""), price.get("billing_class", ""), \
                        price.get("setting", "")
                    rv = price.get("negotiated_rate")
                    exp = price.get("expiration_date", "")
                    numeric = isinstance(rv, (int, float)) and not isinstance(rv, bool)
                    c["price_complete"] += all(present(price, k) for k in req["price"]) and numeric and date_ok(exp)
                    for k, v in (("negotiated_type", ntype), ("billing_class", bclass)):
                        c["enum_values"] += 1
                        allowed = ENUM[k][ver] if k == "billing_class" else ENUM[k]
                        c["enum_valid"] += v in allowed
                    if ver == "2" or "setting" in price:
                        c["enum_values"] += 1
                        c["enum_valid"] += setting in ENUM["setting"]
                    sc = [str(s) for s in (price.get("service_code") or [])]
                    if bclass == "professional":
                        c["cond_applies"] += 1
                        c["cond_met"] += bool(sc)
                    for s in sc:
                        c["pos_codes"] += 1
                        c["pos_valid"] += s in POS
                    c["dollar_type"] += ntype in DOLLAR_TYPES
                    if ntype == "percentage":
                        c["pct_prices"] += 1
                        c["pct_whole"] += numeric and 1 < rv <= 100
                    if numeric:
                        c["rate_le_zero"] += rv <= 0
                    c["contract_current"] += date_ok(exp) and exp >= run_date
                    c["exp_9999"] += exp == "9999-12-31"
                    mods = ";".join(map(str, price.get("billing_code_modifier") or []))
                    tail = [ntype, bclass, setting, ";".join(sc), mods, rv if numeric else "", exp]
                    headp = [i, ctype, code, price.get("additional_information") or "", arr]
                    for g in targets:
                        pw.writerow(headp + tail + [g])
                        c["price_group_rows"] += 1
    c["distinct_codes"] = len(code_items)
    c["codes_in_2plus_items"] = sum(1 for v in code_items.values() if v > 1)

    # ---- CSV -> Parquet
    con = duckdb.connect()
    con.execute(f"""copy (select * from read_csv('{mpath}', header=false, all_varchar=true,
                    names=['provider_group_id','tin_type','tin','npi'])) to
                    '{os.path.join(out_dir, label + "_members.parquet")}' (format parquet)""")
    con.execute(f"""copy (select * from read_csv('{gpath}', header=false, all_varchar=true,
                    names=['provider_group_id','network_name','n_entries'])) to
                    '{os.path.join(out_dir, label + "_groups.parquet")}' (format parquet)""")
    names = ["item_index", "billing_code_type", "billing_code", "additional_information", "negotiation_arrangement",
             "negotiated_type", "billing_class", "setting", "service_code", "billing_code_modifier",
             "negotiated_rate", "expiration_date", "provider_group_id"]
    con.execute(f"""copy (select * replace (try_cast(negotiated_rate as double) as negotiated_rate)
                    from read_csv('{ppath}', header=false, all_varchar=true, names={names})) to
                    '{os.path.join(out_dir, label + "_prices.parquet")}' (format parquet)""")
    for p in (mpath, gpath, ppath):
        os.remove(p)
    stats = {"label": label, "file": os.path.basename(path), "bytes": os.path.getsize(path), "header": head,
             "schema_version_declared": head.get("version"), "schema_major_used": ver,
             "counts": dict(c), "seconds": round(time.time() - t0, 1)}
    with open(os.path.join(out_dir, f"{label}_stats.json"), "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=1, default=str)
    return label, stats["seconds"], c["price_group_rows"], c["npi_entries"]


def label_of(name):
    m = re.search(r"_([a-z0-9]+(?:-[a-z0-9]+)?)_in-network", name)
    return m.group(1) if m else os.path.splitext(name)[0]


def load_hcpcs():
    p = os.path.join(ROOT, "output", "c3", "ref", "hcpcs.parquet")
    return {r[0] for r in duckdb.sql(f"select code from '{p}'").fetchall()}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rate-table", default=DEFAULT_RATE_TABLE)
    ap.add_argument("--rate-dir", default=DEFAULT_RATE_DIR)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--run-date", default=dt.date.today().isoformat())
    ap.add_argument("--only", nargs="*", help="labels to scan, e.g. healthnet-ca ambetter-oh")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    with open(args.rate_table, encoding="utf-8") as f:
        names = [r["rate_file_name"] for r in csv.DictReader(f)]
    hcpcs = load_hcpcs()
    jobs = []
    for n in names:
        p = os.path.join(args.rate_dir, n)
        if os.path.exists(p) and (not args.only or label_of(n) in args.only):
            jobs.append((label_of(n), p, args.out, args.run_date, hcpcs))
    # Largest first so the long files start early.
    jobs.sort(key=lambda j: -os.path.getsize(j[1]))
    print(f"scanning {len(jobs)} rate file(s) with {args.workers} workers", file=sys.stderr)
    with cf.ProcessPoolExecutor(max_workers=min(args.workers, len(jobs))) as pool:
        for label, secs, rows, npis in pool.map(scan, jobs):
            print(f"{label}: {secs}s, {rows:,} price x group rows, {npis:,} NPI entries", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
