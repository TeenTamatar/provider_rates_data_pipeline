"""Shared rate-file scan: read every rate file once and write the tables C2b, C3 and C4 use.

Each rate file is read as two independent streaming jobs, run in parallel:

  refs   header + provider_references -> members, groups; NPI, TIN, network_name and required-field checks
  items  header + in_network          -> prices (one row per price x provider group), referenced group counts;
                                         item, rate and price checks

Writes to output/scan/ (Parquet, written in bounded batches; nothing is expanded to one row per NPI x price):
  <label>_members.parquet  provider_group_id, member_index, tin_type, tin (normalized EIN), tin_raw,
                           business_name, npi
  <label>_groups.parquet   provider_group_id, network_name (non-empty names, ;-joined), network_name_raw,
                           n_entries, n_members, n_tins, n_npis, n_tin_npi_pairs, member_signature, business_names
  <label>_prices.parquet   item_index, rate_index, price_index, billing_code_type, billing_code,
                           additional_information, negotiation_arrangement, negotiated_type, billing_class, setting,
                           service_code, billing_code_modifier, negotiated_rate, expiration_date, provider_group_id
  <label>_refs.parquet     provider_group_id, n (how many rate entries reference it)
  <label>_stats.json       every counter (numerators and denominators for the C3 compliance metrics)

Larger payers: sources may be local `.json` or `.json.gz`, or streamed straight from their URL (`--stream`);
provider lists hosted elsewhere (`location` URLs, schema v1) are fetched; caches are bounded; the two passes of one
file run as separate jobs. Rules: LOGIC.md sections 3.5 (C2b) and 3C (C3).

Usage (after C1; the HCPCS check uses output/c3/ref/hcpcs.parquet when c3_reference.py has run):
    python pipeline/rate_scan.py                       # every rate file in Data/rate_files/
    python pipeline/rate_scan.py --only healthnet-ca   # one file (label = brand-state part of the name)
    python pipeline/rate_scan.py --stream              # stream from the URLs in c1_rate_files.csv, no download
"""
import argparse
import concurrent.futures as cf
import contextlib
import csv
import datetime as dt
import gzip
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
from collections import Counter

import duckdb
import ijson
import pyarrow as pa
import pyarrow.parquet as pq

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_RATE_TABLE = os.path.join(ROOT, "output", "c1", "c1_rate_files.csv")
DEFAULT_RATE_DIR = os.path.join(ROOT, "Data", "rate_files")
DEFAULT_OUT = os.path.join(ROOT, "output", "scan")
HCPCS_PATH = os.path.join(ROOT, "output", "c3", "ref", "hcpcs.parquet")
BATCH = 200_000        # rows per Parquet write
CACHE_LIMIT = 2_000_000  # NPI/TIN validity cache entries before it is cleared

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
    # CPT: 5 digits, or 4 digits + F (Category II), T (Category III), U (PLA) or M (administrative MAAA).
    "CPT": re.compile(r"^(\d{5}|\d{4}[FTUM])$"),
    "HCPCS": re.compile(r"^([A-V]\d{4}|\d{5}|\d{4}[FTUM])$"),  # Level II, or Level I (CPT) codes
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

S = pa.string()
SCHEMAS = {
    "members": pa.schema([("provider_group_id", S), ("member_index", pa.int32()), ("tin_type", S), ("tin", S),
                          ("tin_raw", S), ("business_name", S), ("npi", S)]),
    "groups": pa.schema([("provider_group_id", S), ("network_name", S), ("network_name_raw", S),
                         ("n_entries", pa.int32()), ("n_members", pa.int32()), ("n_tins", pa.int32()),
                         ("n_npis", pa.int32()), ("n_tin_npi_pairs", pa.int32()), ("member_signature", S),
                         ("business_names", S)]),
    "prices": pa.schema([("item_index", pa.int64()), ("rate_index", pa.int32()), ("price_index", pa.int32()),
                         ("billing_code_type", S), ("billing_code", S), ("additional_information", S),
                         ("negotiation_arrangement", S), ("negotiated_type", S), ("billing_class", S), ("setting", S),
                         ("service_code", S), ("billing_code_modifier", S), ("negotiated_rate", pa.float64()),
                         ("expiration_date", S), ("provider_group_id", S)]),
    "refs": pa.schema([("provider_group_id", S), ("n", pa.int64())]),
}


class BatchWriter:
    """Append rows and write them to one Parquet file in batches, so memory stays bounded."""

    def __init__(self, path, schema):
        self.path, self.schema = path, schema
        self.cols = [[] for _ in schema]
        self.writer = pq.ParquetWriter(path, schema)

    def add(self, row):
        for col, v in zip(self.cols, row):
            col.append(v)
        if len(self.cols[0]) >= BATCH:
            self.flush()

    def flush(self):
        if self.cols[0]:
            self.writer.write_table(pa.table(self.cols, schema=self.schema))
            self.cols = [[] for _ in self.schema]

    def close(self):
        self.flush()
        self.writer.close()


@contextlib.contextmanager
def open_source(src):
    """A binary stream over a local file, a .gz file, or a URL (gzip by suffix or Content-Encoding)."""
    if src.startswith(("http://", "https://")):
        req = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            gz = src.split("?")[0].endswith(".gz") or resp.headers.get("Content-Encoding") == "gzip"
            yield gzip.GzipFile(fileobj=resp) if gz else resp
    elif src.endswith(".gz"):
        with gzip.open(src, "rb") as f:
            yield f
    else:
        with open(src, "rb") as f:
            yield f


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


def read_header(src):
    head = {}
    with open_source(src) as f:
        for prefix, event, value in ijson.parse(f):
            if prefix and "." not in prefix and event in ("string", "number", "boolean", "null"):
                head[prefix] = value
            elif prefix in ("provider_references", "in_network") and event == "start_array":
                head.setdefault("_first_array", prefix)
                break
    return {k: (str(v) if not isinstance(v, (str, int, float, type(None))) else v) for k, v in head.items()}


def schema_major(head):
    ver = str(head.get("version", "1")).split(".")[0] or "1"
    return ver if ver in REQ else "2"


_remote = {}


def remote_groups(url):
    """provider_groups from a provider reference hosted at `location` (schema v1); cached per process."""
    if url not in _remote:
        with open_source(url) as f:
            _remote[url] = json.load(f).get("provider_groups") or []
    return _remote[url]


def scan_refs(label, src, out_dir):
    c = Counter()
    head = read_header(src)
    ver = schema_major(head)
    req = REQ[ver]
    c["root_objects"] = 1
    root_missing = [k for k in req["root"] if k != "in_network" and not present(head, k)]
    c["root_complete"] = int(not root_missing and date_ok(head.get("last_updated_on")))
    for k in ("plan_id_type", "plan_market_type"):
        if k in head:
            c["enum_values"] += 1
            c["enum_valid"] += head[k] in ENUM[k]
    if head.get("plan_id_type") == "ein":
        c["cond_applies"] += 1
        c["cond_met"] += present(head, "plan_sponsor_name")
    npi_cache, tin_cache = {}, {}
    mw = BatchWriter(os.path.join(out_dir, f"{label}_members.parquet"), SCHEMAS["members"])
    gw = BatchWriter(os.path.join(out_dir, f"{label}_groups.parquet"), SCHEMAS["groups"])
    with open_source(src) as f:
        for ref in ijson.items(f, "provider_references.item", use_float=True):
            c["ref_objects"] += 1
            gid = ref.get("provider_group_id")
            ok = all(present(ref, k) for k in req["ref"]) and isinstance(gid, int) and \
                isinstance(ref.get("network_name", []), list)
            if ver == "1" and not (present(ref, "provider_groups") or present(ref, "location")):
                ok = False
            c["ref_complete"] += ok
            members = ref.get("provider_groups") or []
            if not members and ref.get("location"):
                c["ref_remote_location"] += 1
                try:
                    members = remote_groups(ref["location"])
                except Exception:  # noqa: BLE001  an unreachable provider list is counted, not fatal
                    c["ref_remote_failed"] += 1
            raw_names = [str(x) for x in (ref.get("network_name") or [])]
            nn = [x for x in raw_names if x.strip()]
            c["groups_with_network_name"] += bool(nn)
            pairs, tins, npis_seen, names = set(), set(), set(), []
            for i, pg in enumerate(members):
                c["provider_objects"] += 1
                tin = pg.get("tin") or {}
                npis = pg.get("npi") if isinstance(pg.get("npi"), list) else []
                c["provider_complete"] += present(pg, "npi") and present(pg, "tin") and \
                    all(isinstance(n, (int, float)) for n in npis)
                c["tin_objects"] += 1
                c["tin_complete"] += all(present(tin, k) for k in req["tin"])
                ttype, tval = tin.get("type", ""), tin.get("value", "")
                tin_raw = str(tval if tval is not None else "").strip()
                name = tin.get("business_name", "") or ""
                names.append(name)
                tins.add(tin_raw)
                c["enum_values"] += 1
                c["enum_valid"] += ttype in ENUM["tin.type"]
                if ttype == "ein":
                    c["cond_applies"] += 1
                    c["cond_met"] += present(tin, "business_name")
                if len(tin_cache) > CACHE_LIMIT:
                    tin_cache.clear()
                key = (ttype, tval)
                if key not in tin_cache:
                    tin_cache[key] = tin_valid(ttype, tval)
                status, tnorm = tin_cache[key]
                c["tin_entries"] += 1
                c[f"tin_{status}"] += 1
                for n in npis:
                    if len(npi_cache) > CACHE_LIMIT:
                        npi_cache.clear()
                    if n not in npi_cache:
                        npi_cache[n] = npi_valid(n)
                    st = npi_cache[n]
                    c["npi_entries"] += 1
                    c[f"npi_{st}"] += 1
                    npi_text = str(int(n)) if isinstance(n, (int, float)) and st != "invalid" else str(n)
                    mw.add((str(gid), i, ttype, tnorm, tin_raw, name, npi_text))
                    pairs.add((tin_raw, npi_text))
                    npis_seen.add(npi_text)
            # Hash of the sorted (TIN, NPI) pairs: groups listing exactly the same providers share it (C2b).
            signature = hashlib.sha1("|".join(f"{t}:{n}" for t, n in sorted(pairs)).encode()).hexdigest()[:16]
            gw.add((str(gid), ";".join(nn), ";".join(raw_names), len(members), len(members), len(tins),
                    len(npis_seen), len(pairs), signature, " | ".join(dict.fromkeys(names))))
    mw.close()
    gw.close()
    return head, ver, c


def scan_items(label, src, out_dir, run_date, hcpcs):
    c = Counter()
    head = read_header(src)
    ver = schema_major(head)
    req = REQ[ver]
    code_items, ref_counts = Counter(), Counter()
    pw = BatchWriter(os.path.join(out_dir, f"{label}_prices.parquet"), SCHEMAS["prices"])
    with open_source(src) as f:
        for i, item in enumerate(ijson.items(f, "in_network.item", use_float=True)):
            c["item_objects"] += 1
            arr, ctype = item.get("negotiation_arrangement", ""), item.get("billing_code_type", "")
            code_raw = str(item.get("billing_code", ""))
            code = code_raw.strip()
            c["item_complete"] += all(present(item, k) for k in req["item"])
            for k, v in (("negotiation_arrangement", arr), ("billing_code_type", ctype)):
                c["enum_values"] += 1
                c["enum_valid"] += v in ENUM[k]
            rx = CODE_RE.get(ctype)
            code_ok = bool(code) and (bool(rx.match(code)) if rx else True)
            if code_ok and ctype == "HCPCS" and code[0].isalpha() and code[0] != "D" and hcpcs is not None:
                # Level II codes must be on the CMS list. D codes are CDT (ADA copyright), which CMS leaves out.
                code_ok = code in hcpcs
            c["items_code_valid"] += bool(code_ok and present(item, "description"))
            if not code_ok:
                c[f"invalid_code_{ctype}"] += 1
            code_items[(ctype, code, arr)] += 1
            if arr in ("bundle", "capitation"):
                c["guide_list_applies"] += 1
                c["guide_list_met"] += present(item, "bundled_codes" if arr == "bundle" else "covered_services")
            for r, rate in enumerate(item.get("negotiated_rates") or []):
                c["rate_objects"] += 1
                refs = rate.get("provider_references") or []
                inline = rate.get("provider_groups") or []
                r_ok = all(present(rate, k) for k in req["rate"]) if ver == "2" else \
                    (present(rate, "negotiated_prices") and (bool(refs) or bool(inline)))
                c["rate_complete"] += r_ok
                for g in refs:
                    c["refs_used"] += 1
                    ref_counts[str(g)] += 1
                targets = [str(g) for g in refs]
                if inline:
                    c["inline_group_sets"] += 1
                    targets.append("-1")  # inline providers: kept as one pseudo group per file (rare)
                for p, price in enumerate(rate.get("negotiated_prices") or []):
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
                    row = (i, r, p, ctype, code_raw, price.get("additional_information") or "", arr, ntype, bclass,
                           setting, ";".join(sc), ";".join(map(str, price.get("billing_code_modifier") or [])),
                           float(rv) if numeric else None, exp)
                    for g in targets:
                        pw.add(row + (g,))
                        c["price_group_rows"] += 1
    pw.close()
    rw = BatchWriter(os.path.join(out_dir, f"{label}_refs.parquet"), SCHEMAS["refs"])
    for g, n in ref_counts.items():
        rw.add((g, n))
    rw.close()
    c["distinct_codes"] = len(code_items)
    c["codes_in_2plus_items"] = sum(1 for v in code_items.values() if v > 1)
    return head, ver, c


def scan(job):
    label, src, out_dir, part, run_date, hcpcs = job
    t0 = time.time()
    head, ver, c = scan_refs(label, src, out_dir) if part == "refs" else \
        scan_items(label, src, out_dir, run_date, hcpcs)
    return label, part, head, ver, dict(c), round(time.time() - t0, 1)


def finalize(label, src, size, out_dir, parts):
    """Merge the two passes' counters, count resolved references and distinct identifiers, write stats."""
    head, ver = parts["refs"][0], parts["refs"][1]
    c = Counter(parts["refs"][2]) + Counter(parts["items"][2])
    t = lambda n: os.path.join(out_dir, f"{label}_{n}.parquet").replace("\\", "/")
    con = duckdb.connect()
    c["refs_resolved"] = con.execute(f"""select coalesce(sum(r.n), 0) from '{t("refs")}' r
                                         where r.provider_group_id in (select provider_group_id from '{t("groups")}')""").fetchone()[0]
    npis, tins = con.execute(f"""select count(distinct npi) filter (where npi <> '0'), count(distinct tin)
                                 from '{t("members")}'""").fetchone()
    c["distinct_npis"], c["distinct_tins"] = npis, tins
    stats = {"label": label, "file": os.path.basename(src.split("?")[0]), "source": src, "bytes": size,
             "header": head, "schema_version_declared": head.get("version"), "schema_major_used": ver,
             "counts": dict(c), "seconds": {"refs": parts["refs"][3], "items": parts["items"][3]}}
    with open(os.path.join(out_dir, f"{label}_stats.json"), "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=1, default=str)


def label_of(name):
    m = re.search(r"_([a-z0-9]+(?:-[a-z0-9]+)?)_in-network", name)
    return m.group(1) if m else os.path.splitext(name)[0]


def load_hcpcs(path=HCPCS_PATH):
    """CMS HCPCS Level II codes, or None when the reference table has not been built (format check only)."""
    if not os.path.exists(path):
        return None
    return {r[0] for r in duckdb.sql(f"select code from '{path}'").fetchall()}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rate-table", default=DEFAULT_RATE_TABLE)
    ap.add_argument("--rate-dir", default=DEFAULT_RATE_DIR)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--run-date", default=dt.date.today().isoformat())
    ap.add_argument("--only", nargs="*", help="labels to scan, e.g. healthnet-ca ambetter-oh")
    ap.add_argument("--stream", action="store_true", help="read each file from its URL instead of Data/rate_files/")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    with open(args.rate_table, encoding="utf-8") as f:
        table = list(csv.DictReader(f))
    files = []
    for r in table:
        label = label_of(r["rate_file_name"])
        if args.only and label not in args.only:
            continue
        if args.stream:
            if r["http_status"] == "200":
                files.append((label, r["rate_file_url"], int(float(r["content_length"] or 0))))
            continue
        for name in (r["rate_file_name"], r["rate_file_name"] + ".gz"):
            p = os.path.join(args.rate_dir, name)
            if os.path.exists(p):
                files.append((label, p, os.path.getsize(p)))
                break
    hcpcs = load_hcpcs()
    files.sort(key=lambda x: -x[2])  # largest first so the long files start early
    jobs = [(label, src, args.out, part, args.run_date, hcpcs) for label, src, _ in files for part in ("items", "refs")]
    print(f"scanning {len(files)} rate file(s) as {len(jobs)} jobs with {args.workers} workers"
          f"{'' if hcpcs is not None else ' (no HCPCS list: code format only)'}", file=sys.stderr)
    done = {}
    with cf.ProcessPoolExecutor(max_workers=min(args.workers, len(jobs))) as pool:
        for label, part, head, ver, counts, secs in pool.map(scan, jobs):
            done.setdefault(label, {})[part] = (head, ver, counts, secs)
            if len(done[label]) == 2:
                src, size = next((s, z) for lab, s, z in files if lab == label)
                finalize(label, src, size, args.out, done[label])
                c = Counter(done[label]["refs"][2]) + Counter(done[label]["items"][2])
                print(f"{label}: {done[label]['refs'][3]}s + {done[label]['items'][3]}s, "
                      f"{c['price_group_rows']:,} price x group rows, {c['npi_entries']:,} NPI entries",
                      file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
