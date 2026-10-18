"""C3 metrics: compute the rate-file quality metrics M1-M27 and the scores (LOGIC.md section 3C).

Inputs: output/c1/c1_rate_files.csv (every rate file the index files link), output/scan/ (rate_scan.py),
output/c3/ref/ (c3_reference.py).

Outputs in output/c3/:
  c3_rate_file_metrics.csv  one row per rate file x metric: group, numerator, denominator, value, score,
                            measured, note
  c3_rate_file_scores.csv   one row per rate file: gate, group scores, file score
  c3_rate_file_rollup.csv   one row per index dataset (brand): files, reachable, mean, min and max file score
                            (the dataset score itself, with the index, is in c3_dataset_scores.csv: c3_index_metrics.py)
  c3_rate_file_summary.json run settings and counts

Usage:
    python pipeline/c3_metrics.py --run-date 2026-10-18
    python pipeline/c3_metrics.py --only healthnet-ca --no-http
"""
import argparse
import csv
import datetime as dt
import json
import math
import os
import sys
import time
import urllib.request

import duckdb

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rate_scan import label_of  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_RATE_TABLE = os.path.join(ROOT, "output", "c1", "c1_rate_files.csv")
SCAN = os.path.join(ROOT, "output", "scan")
REF = os.path.join(ROOT, "output", "c3", "ref")
DEFAULT_OUT = os.path.join(ROOT, "output", "c3")

GROUPS = {"timeliness": 0.10, "compliance": 0.45, "plausibility": 0.25, "usability": 0.20}
METRICS = [  # id, name, group
    ("M1", "Reachable", "gate"),
    ("M2", "Freshness", "timeliness"), ("M3", "Index-to-file lag", "timeliness"),
    ("M4", "Contract currency", "timeliness"),
    ("M5", "Required fields", "compliance"), ("M6", "Valid enumerations", "compliance"),
    ("M7", "Conditional requirements", "compliance"), ("M8", "Reference integrity", "compliance"),
    ("M9", "Valid billing codes", "compliance"), ("M10", "Valid NPIs", "compliance"),
    ("M11", "Valid TINs", "compliance"), ("M12", "Valid place of service", "compliance"),
    ("M13", "Dollar rate types", "compliance"),
    ("M14", "$0 / negative rates", "plausibility"), ("M15", "Outliers vs Medicare", "plausibility"),
    ("M16", "Internal outliers", "plausibility"), ("M17", "Provider validity", "plausibility"),
    ("M18", "Entity type vs billing class", "plausibility"),
    ("M19", "Duplicate rows", "usability"), ("M20", "Repeated items", "usability"),
    ("M21", "Code coverage", "usability"), ("M22", "Provider coverage (reported)", "reported"),
    ("M23", "Network named", "usability"), ("M24", "Unexplained rate conflicts", "usability"),
    ("M25", "File naming (guide)", "usability"), ("M26", "Bundle and capitation code lists (guide)", "usability"),
    ("M27", "Percentage format (guide)", "usability"),
]
CYCLE_DAYS, DECAY_DAYS = 31, 10  # R3.8
FENCE, EXTREME, MIN_PRICES = 1.5, 3.0, 10  # R3.7
# Places of service paid at the Medicare facility rate (hospital, ASC, SNF and similar settings).
FACILITY_POS = {"19", "21", "22", "23", "24", "26", "31", "34", "41", "42", "51", "52", "53", "56", "61"}
PRICE_KEY = ["billing_code_type", "billing_code", "additional_information", "negotiation_arrangement",
             "negotiated_type", "billing_class", "setting", "service_code", "billing_code_modifier"]


def curve(days):
    return 1.0 if days <= CYCLE_DAYS else math.exp(-(days - CYCLE_DAYS) / DECAY_DAYS)


def reachable(url, timeout=30):
    for method, headers in (("HEAD", {}), ("GET", {"Range": "bytes=0-0"})):
        try:
            req = urllib.request.Request(url, method=method, headers={"User-Agent": "c3-quality-check", **headers})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, ""
        except urllib.error.HTTPError as e:
            if method == "GET":
                return e.code, str(e)
        except Exception as e:  # noqa: BLE001  network errors are recorded, not raised
            if method == "GET":
                return 0, str(e)[:200]
    return 0, "no response"


class FileMetrics:
    def __init__(self, label, row, run_date, con):
        self.label, self.row, self.run_date, self.con = label, row, run_date, con
        self.rows = {}

    def put(self, mid, num=None, den=None, value=None, score=None, note="", measured=True):
        self.rows[mid] = {"numerator": num, "denominator": den, "value": value,
                          "score": None if not measured else score, "measured": measured, "note": note}

    def ratio(self, mid, num, den, invert=False, note=""):
        if not den:
            self.put(mid, num, den, measured=False, note=note or "nothing to check in this file")
            return
        v = num / den
        self.put(mid, num, den, round(v, 6), round(1 - v if invert else v, 6), note)

    def q(self, sql):
        return self.con.execute(sql).fetchall()


def compute(fm, stats, ref, hilabs_total):
    c = stats["counts"]
    head = stats["header"]
    t = lambda n: f"'{os.path.join(SCAN, fm.label + '_' + n + '.parquet')}'"
    P, G, M = t("prices"), t("groups"), t("members")
    run = dt.date.fromisoformat(fm.run_date)

    # ---- Timeliness
    lu = head.get("last_updated_on")
    try:
        d = (run - dt.date.fromisoformat(lu)).days
        fm.put("M2", d, None, d, round(curve(d), 6), f"last_updated_on {lu}")
    except (TypeError, ValueError):
        fm.put("M2", measured=False, note=f"last_updated_on missing or invalid ({lu})")
    try:
        lag = (dt.date.fromisoformat(fm.row["index_last_updated_on"]) - dt.date.fromisoformat(lu)).days
        fm.put("M3", lag, None, lag, round(curve(lag), 6),
               f"index {fm.row['index_last_updated_on']}, file {lu} (negative = file newer)")
    except (TypeError, ValueError):
        fm.put("M3", measured=False, note="index or file date missing")
    fm.ratio("M4", c.get("contract_current", 0), c.get("price_objects", 0),
             note=f"{c.get('exp_9999', 0):,} of {c.get('price_objects', 0):,} prices end 9999-12-31")

    # ---- Compliance
    parts = [("root", c.get("root_complete", 0), c.get("root_objects", 0)),
             ("provider_reference", c.get("ref_complete", 0), c.get("ref_objects", 0)),
             ("provider", c.get("provider_complete", 0), c.get("provider_objects", 0)),
             ("tin", c.get("tin_complete", 0), c.get("tin_objects", 0)),
             ("item", c.get("item_complete", 0), c.get("item_objects", 0)),
             ("negotiated_rate", c.get("rate_complete", 0), c.get("rate_objects", 0)),
             ("negotiated_price", c.get("price_complete", 0), c.get("price_objects", 0))]
    shares = [n / dn for _, n, dn in parts if dn]
    # Macro average over object types, so a broken header is not drowned out by millions of prices.
    fm.put("M5", sum(n for _, n, _ in parts), sum(dn for _, _, dn in parts), round(sum(shares) / len(shares), 6),
           round(sum(shares) / len(shares), 6),
           "average over object types: " + ", ".join(f"{k} {n:,}/{dn:,}" for k, n, dn in parts if dn) +
           f"; schema v{stats['schema_version_declared']}")
    fm.ratio("M6", c.get("enum_valid", 0), c.get("enum_values", 0))
    fm.ratio("M7", c.get("cond_met", 0), c.get("cond_applies", 0))
    fm.ratio("M8", c.get("refs_resolved", 0), c.get("refs_used", 0),
             note=f"{c.get('inline_group_sets', 0)} inline provider sets")
    bad = {k[len('invalid_code_'):]: v for k, v in c.items() if k.startswith("invalid_code_")}
    fm.ratio("M9", c.get("items_code_valid", 0), c.get("item_objects", 0), note=f"invalid by code type: {bad}")
    fm.ratio("M10", c.get("npi_valid", 0), c.get("npi_entries", 0) - c.get("npi_zero", 0),
             note=f"{c.get('npi_zero', 0):,} TIN-level entries (npi 0) not scored; {c.get('npi_invalid', 0):,} invalid")
    fm.ratio("M11", c.get("tin_valid", 0) + c.get("tin_padded", 0), c.get("tin_entries", 0),
             note=f"{c.get('tin_padded', 0):,} padded from 8 digits; {c.get('tin_invalid', 0):,} invalid")
    fm.ratio("M12", c.get("pos_valid", 0), c.get("pos_codes", 0))
    fm.ratio("M13", c.get("dollar_type", 0), c.get("price_objects", 0))

    # ---- Plausibility
    fm.ratio("M14", c.get("rate_le_zero", 0), c.get("price_objects", 0), invert=True)
    key = ", ".join(PRICE_KEY)
    # Empty key parts are NULL in the scan tables; coalesce them so equal keys join and group together.
    keyc = ", ".join(f"coalesce({k}, '') {k}" for k in PRICE_KEY)
    fm.con.execute(f"""create or replace temp table d as
                       select provider_group_id, {keyc}, negotiated_rate, count(*) n_raw
                       from {P} where negotiated_rate is not null group by all""")
    state = fm.row["state"]
    med = fm.q(f"""
        with p as (select negotiated_rate r, billing_code code, coalesce(billing_code_modifier, '') modifier,
                          case when service_code is not null and service_code <> ''
                                    and len(list_filter(string_split(service_code, ';'),
                                                        x -> x not in {tuple(FACILITY_POS)})) = 0
                               then 'facility' else 'nonfacility' end site
                   from d where billing_class = 'professional' and billing_code_type in ('CPT', 'HCPCS')
                     and negotiation_arrangement = 'ffs'
                     and negotiated_type in ('negotiated', 'fee schedule', 'derived') and negotiated_rate > 0
                     and coalesce(billing_code_modifier, '') in ('', '26', 'TC')),
             j as (select ln(p.r / case when site = 'facility' then m.facility else m.nonfacility end) x
                   from p join '{ref}/medicare_pfs.parquet' m
                     on m.code = p.code and m.modifier = p.modifier and m.state = '{state}'),
             f as (select quantile_cont(x, 0.25) q1, quantile_cont(x, 0.75) q3, median(x) med, count(*) n from j)
        select f.n, f.q1, f.q3, f.med,
               (select count(*) from j where x < f.q1 - {FENCE} * (f.q3 - f.q1) or x > f.q3 + {FENCE} * (f.q3 - f.q1)),
               (select count(*) from j where x < f.q1 - {EXTREME} * (f.q3 - f.q1) or x > f.q3 + {EXTREME} * (f.q3 - f.q1))
        from f""")[0]
    if med[0]:
        n, q1, q3, md, out, ext = med
        fm.ratio("M15", out, n, invert=True,
                 note=f"median rate = {math.exp(md):.2f}x Medicare ({state}); fences {math.exp(q1 - FENCE * (q3 - q1)):.2f}x"
                      f"-{math.exp(q3 + FENCE * (q3 - q1)):.2f}x; {ext:,} extreme")
    else:
        fm.put("M15", measured=False, note="no professional CPT/HCPCS dollar prices with a Medicare fee")
    io = fm.q(f"""
        with k as (select {key}, ln(negotiated_rate) x from d where negotiated_rate > 0),
             s as (select {key}, quantile_cont(x, 0.25) q1, quantile_cont(x, 0.75) q3 from k group by all
                   having count(*) >= {MIN_PRICES})
        select count(*),
               count(*) filter (where x < q1 - {FENCE} * (q3 - q1) or x > q3 + {FENCE} * (q3 - q1)),
               count(*) filter (where x < q1 - {EXTREME} * (q3 - q1) or x > q3 + {EXTREME} * (q3 - q1)),
               count(distinct ({key}))
        from k join s using ({key})""")[0]
    total_keys = fm.q(f"select count(distinct ({key})) from d")[0][0]
    fm.ratio("M16", io[1], io[0], invert=True,
             note=f"{io[3]:,} of {total_keys:,} price keys have >= {MIN_PRICES} prices; {io[2]:,} extreme")
    fm.con.execute(f"""create or replace temp table fnpi as
                       select distinct try_cast(npi as bigint) npi from {M} where npi <> '0'""")
    pv = fm.q(f"""select count(*), count(n.npi) filter (where not n.deactivated),
                         count(n.npi) filter (where n.deactivated), count(*) - count(n.npi)
                  from fnpi f left join '{ref}/nppes.parquet' n using (npi)""")[0]
    fm.ratio("M17", pv[1], pv[0], note=f"{pv[2]:,} deactivated, {pv[3]:,} not in NPPES")
    et = fm.q(f"""
        with g as (select m.provider_group_id, bool_and(n.entity_type = '1') type1_only
                   from {M} m left join '{ref}/nppes.parquet' n on n.npi = try_cast(m.npi as bigint)
                   where m.npi <> '0' group by 1)
        select count(*), count(*) filter (where g.type1_only)
        from d join g on g.provider_group_id = d.provider_group_id where d.billing_class = 'institutional'""")[0]
    fm.ratio("M18", et[1], et[0], invert=True, note="institutional prices on groups listing only individual NPIs")

    # ---- Usability
    raw = c.get("price_group_rows", 0)
    distinct = fm.q("select count(*) from d")[0][0]
    fm.ratio("M19", raw - distinct, raw, invert=True, note=f"{raw:,} raw price rows -> {distinct:,} distinct")
    fm.ratio("M20", c.get("codes_in_2plus_items", 0), c.get("distinct_codes", 0), invert=True)
    cov = fm.q(f"""
        with codes as (select distinct billing_code code from d where billing_code_type in ('CPT', 'HCPCS')
                       union select distinct additional_information from d
                       where regexp_matches(additional_information, '^([A-V][0-9]{{4}}|[0-9]{{5}}|[0-9]{{4}}[FTUM])$'))
        select count(*), coalesce(sum(h.weight), 0) from '{ref}/hilabs_codes.parquet' h join codes using (code)""")[0]
    fm.put("M21", round(cov[1]), round(hilabs_total), round(cov[1] / hilabs_total, 6), round(cov[1] / hilabs_total, 6),
           f"{cov[0]} of 583 HiLabs codes priced (incl. codes in additional_information)")
    pc = fm.q(f"""select count(*), count(f.npi) from spec_npi s left join fnpi f using (npi)
                  where s.practice_state = '{state}'""")[0]
    fm.ratio("M22", pc[1], pc[0], note=f"active {state} NPPES providers with a HiLabs-specialty taxonomy")
    fm.ratio("M23", c.get("groups_with_network_name", 0), c.get("ref_objects", 0))
    # Providers (TIN + NPI) in the same set of groups get the same answer for every key, so they are
    # collapsed into membership signatures (sid) and counted with their provider count (n_prov).
    cf = fm.q(f"""
        with mm as (select distinct provider_group_id g, tin || '|' || npi prov from {M}),
             ps as (select prov, list(g order by g) gl from mm group by prov having count(*) >= 2),
             sg as (select row_number() over () sid, gl, n_prov from (select gl, count(*) n_prov from ps group by gl)),
             sgg as (select sid, n_prov, unnest(gl) g from sg),
             gn as (select provider_group_id g, coalesce(network_name, '') nn from {G}),
             dk as (select provider_group_id g, hash({key}) k, negotiated_rate r from d),
             x as (select sgg.sid, sgg.n_prov, dk.k, dk.g, dk.r, gn.nn
                   from sgg join dk on dk.g = sgg.g join gn on gn.g = sgg.g),
             pk as (select sid, k, any_value(n_prov) n_prov, count(distinct r) nr, count(distinct nn) nnn
                    from x group by sid, k having count(distinct g) >= 2)
        select coalesce(sum(n_prov), 0), coalesce(sum(n_prov) filter (where nr > 1), 0),
               coalesce(sum(n_prov) filter (where nr > 1 and nnn = 1), 0) from pk""")[0]
    fm.ratio("M24", cf[2], cf[0], invert=True,
             note=f"{cf[1]:,} provider x service keys differ across groups, {cf[2]:,} with no network_name to tell them apart")
    fm.put("M25", measured=False,
           note="CMS naming standard applies only to single-plan files; files listed in an index have none")
    fm.ratio("M26", c.get("guide_list_met", 0), c.get("guide_list_applies", 0))
    fm.ratio("M27", c.get("pct_whole", 0), c.get("pct_prices", 0))


def score_file(fm):
    gate = fm.rows["M1"]["score"]
    groups = {}
    for mid, _, grp in METRICS:
        if grp not in GROUPS:  # gate, and metrics that are reported but not scored (M22)
            continue
        s = fm.rows.get(mid, {}).get("score")
        if s is not None:
            groups.setdefault(grp, []).append(s)
    gs = {g: sum(v) / len(v) for g, v in groups.items()}
    w = sum(GROUPS[g] for g in gs)
    weighted = sum(GROUPS[g] * s for g, s in gs.items()) / w if w else 0
    return gs, (gate or 0) * weighted


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rate-table", default=DEFAULT_RATE_TABLE)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--run-date", default=dt.date.today().isoformat())
    ap.add_argument("--only", nargs="*", help="labels to score, e.g. healthnet-ca")
    ap.add_argument("--no-http", action="store_true", help="use the C1 HTTP status instead of a live check")
    args = ap.parse_args()
    with open(args.rate_table, encoding="utf-8") as f:
        table = list(csv.DictReader(f))
    con = duckdb.connect()
    hilabs_total = con.execute(f"select sum(weight) from '{REF}/hilabs_codes.parquet'").fetchone()[0]
    # Active NPPES providers with a HiLabs-specialty taxonomy, by practice state (M22), built once.
    con.execute(f"""create temp table spec_npi as
                    select distinct n.npi, n.practice_state
                    from (select npi, practice_state, unnest(taxonomies) taxonomy from '{REF}/nppes.parquet'
                          where not deactivated) n
                    join '{REF}/hilabs_taxonomies.parquet' h using (taxonomy)""")
    metric_rows, score_rows = [], []
    for row in table:
        label = label_of(row["rate_file_name"])
        if args.only and label not in args.only:
            continue
        t0 = time.time()
        fm = FileMetrics(label, row, args.run_date, con)
        status, err = (int(row["http_status"] or 0), row.get("head_error", "")) if args.no_http else reachable(row["rate_file_url"])
        fm.put("M1", status, None, status, 1.0 if status in (200, 206) else 0.0, f"HTTP {status} {err}".strip())
        stats_path = os.path.join(SCAN, f"{label}_stats.json")
        if status in (200, 206) and os.path.exists(stats_path):
            with open(stats_path, encoding="utf-8") as f:
                compute(fm, json.load(f), REF, hilabs_total)
        else:
            reason = "file unreachable" if status not in (200, 206) else "reachable but not scanned"
            for mid, _, grp in METRICS[1:]:
                fm.put(mid, measured=False, note=reason)
        gs, score = score_file(fm)
        for mid, name, grp in METRICS:
            r = fm.rows.get(mid, {"measured": False, "note": "not computed"})
            metric_rows.append({"rate_file": label, "dataset": row["brand"], "metric_id": mid, "metric": name,
                                "group": grp, **{k: r.get(k) for k in ("numerator", "denominator", "value", "score",
                                                                       "measured", "note")}})
        score_rows.append({"rate_file": label, "dataset": row["brand"], "state": row["state"],
                           "gate": fm.rows["M1"]["score"],
                           **{g: (round(gs[g], 4) if g in gs else None) for g in GROUPS},
                           "file_score": round(score, 4),
                           "metrics_measured": sum(1 for r in fm.rows.values() if r["measured"]),
                           "seconds": round(time.time() - t0, 1)})
        print(f"{label}: {score:.3f} {score_rows[-1]}", file=sys.stderr, flush=True)

    os.makedirs(args.out, exist_ok=True)
    for name, rows in (("c3_rate_file_metrics.csv", metric_rows), ("c3_rate_file_scores.csv", score_rows)):
        with open(os.path.join(args.out, name), "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    ds = {}
    for r in score_rows:
        ds.setdefault(r["dataset"], []).append(r)
    ds_rows = [{"dataset": k, "rate_files": len(v), "reachable": sum(1 for r in v if r["gate"] == 1),
                "mean_file_score": round(sum(r["file_score"] for r in v) / len(v), 4),
                "min_file_score": min(r["file_score"] for r in v), "max_file_score": max(r["file_score"] for r in v)}
               for k, v in sorted(ds.items())]
    with open(os.path.join(args.out, "c3_rate_file_rollup.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(ds_rows[0]))
        w.writeheader()
        w.writerows(ds_rows)
    with open(os.path.join(args.out, "c3_rate_file_summary.json"), "w", encoding="utf-8") as f:
        json.dump({"run_date": args.run_date, "group_weights": GROUPS, "freshness": [CYCLE_DAYS, DECAY_DAYS],
                   "outlier_fence_iqr": FENCE, "extreme_iqr": EXTREME, "min_prices_per_key": MIN_PRICES,
                   "datasets": ds_rows}, f, indent=2)
    print(json.dumps(ds_rows, indent=1))


if __name__ == "__main__":
    main()
