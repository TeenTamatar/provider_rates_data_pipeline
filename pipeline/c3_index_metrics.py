"""C3 index metrics (I1-I15) and dataset scores (LOGIC.md section 3C).

Scores each Centene index file (table of contents) and rolls it up with the scores of the rate files it links:

  dataset score = index score^0.3 x (mean rate-file score)^0.7, unreachable rate files counting 0
  (a weighted geometric mean: the index and the rate files are links in one chain, so a zero in either gives 0).

Inputs: Data/*_index.json, output/c3/c3_rate_file_scores.csv (c3_metrics.py), output/scan/ (rate_scan.py),
output/c3/ref/provider_states.parquet (c3_reference.py).

Outputs in output/c3/:
  c3_index_metrics.csv   one row per index x metric
  c3_index_scores.csv    one row per index: gate, group scores, index score
  c3_dataset_scores.csv  one row per dataset: index score, rate files, mean rate-file score, dataset score

Usage:
    python pipeline/c3_index_metrics.py --run-date 2026-10-18
"""
import argparse
import collections
import csv
import datetime as dt
import glob
import json
import os
import re
import sys

import duckdb

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from c3_metrics import curve, reachable  # noqa: E402
from rate_scan import date_ok, label_of, present, tin_valid  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "Data")
C3 = os.path.join(ROOT, "output", "c3")
# Centene publishes its index files next to the rate files they link.
CENTENE_BASE = "https://www.centene.com/content/dam/centene/Centene%20Corporate/json/DOCUMENT/"

GROUPS = {"timeliness": 0.10, "compliance": 0.45, "consistency": 0.15, "usability": 0.30}
INDEX_SHARE = 0.30  # dataset score = index^0.30 x files^0.70 (R3.14)
METRICS = [
    ("I1", "Reachable", "gate"),
    ("I2", "Freshness", "timeliness"),
    ("I3", "Required fields", "compliance"), ("I4", "Valid enumerations", "compliance"),
    ("I5", "Conditional requirements", "compliance"), ("I6", "Plan ID matches its type", "compliance"),
    ("I7", "Plan ID at the required level", "compliance"), ("I8", "Out-of-network file linked", "compliance"),
    ("I9", "Linked rate files reachable", "consistency"), ("I10", "Allowed-amount files reachable", "consistency"),
    ("I11", "State match", "consistency"), ("I12", "Plan ID consistency", "consistency"),
    ("I13", "Duplicate plan rows", "usability"), ("I14", "Unambiguous plan-to-file mapping", "usability"),
    ("I15", "Secure links", "usability"),
]
REQ = {"root": ["reporting_entity_name", "reporting_entity_type", "reporting_structure", "last_updated_on", "version"],
       "plan": ["plan_name", "plan_id_type", "plan_id", "plan_market_type", "issuer_name"],
       "file": ["description", "location"]}
STATES = set("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY "
             "NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY PR VI GU AS MP".split())
HIOS_ISSUER = re.compile(r"^\d{5}$")
HIOS_PRODUCT = re.compile(r"^\d{5}([A-Z]{2})\d{3}$")
HIOS_PLAN = re.compile(r"^\d{5}([A-Z]{2})\d{7}(-\d{2})?$")


def hios_kind(pid):
    """'plan' (14 characters, optional -NN variant), 'product' (10), 'issuer' (5) or None; plus the state."""
    for kind, rx in (("plan", HIOS_PLAN), ("product", HIOS_PRODUCT)):
        m = rx.match(pid)
        if m:
            return kind, m.group(1)
    return ("issuer", None) if HIOS_ISSUER.match(pid) else (None, None)


class Rows:
    def __init__(self):
        self.r = {}

    def put(self, mid, num=None, den=None, value=None, score=None, note="", measured=True):
        self.r[mid] = {"numerator": num, "denominator": den, "value": value,
                       "score": score if measured else None, "measured": measured, "note": note}

    def ratio(self, mid, num, den, invert=False, note=""):
        if not den:
            return self.put(mid, num, den, measured=False, note=note or "nothing to check")
        v = num / den
        self.put(mid, num, den, round(v, 6), round(1 - v if invert else v, 6), note)


def file_state(con, label):
    """Top practice state of the providers in a scanned rate file. Each NPI contributes every state it practises
    in: from the CMS Doctors and Clinicians file when the NPI is there, otherwise from NPPES primary and secondary
    locations (NPPES addresses are often stale). Also returns the top state per source family for comparison."""
    path = os.path.join(ROOT, "output", "scan", f"{label}_members.parquet")
    if not os.path.exists(path):
        return None
    ref = os.path.join(C3, "ref", "provider_states.parquet")
    q = f"""
        with f as (select distinct try_cast(npi as bigint) npi from '{path}' where npi <> '0'),
             s as (select f.npi, p.state, p.src from f join '{ref}' p using (npi)),
             indac as (select distinct npi from s where src = 'dac'),
             best as (select distinct npi, state from s
                      where src = 'dac' or npi not in (select npi from indac))
        select (select count(*) from f),
               (select count(*) from indac),
               (select list(struct_pack(st := state, n := n)) from
                  (select state, count(*) n from best group by 1 order by 2 desc limit 3)),
               (select state from s where src = 'dac' group by state order by count(distinct npi) desc limit 1),
               (select state from s where src <> 'dac' group by state order by count(distinct npi) desc limit 1),
               (select count(distinct npi) from best)"""
    n, n_dac, top3, top_dac, top_nppes, n_located = con.execute(q).fetchone()
    return {"npis": n, "in_dac": n_dac, "located": n_located, "top3": top3 or [],
            "top_dac": top_dac, "top_nppes": top_nppes}


def plan_states_by_file():
    """C1 state of the plan rows linked to each rate file URL."""
    out = collections.defaultdict(collections.Counter)
    with open(os.path.join(ROOT, "output", "c1", "c1_plans.csv"), encoding="utf-8") as f:
        for r in csv.DictReader(f):
            out[r["rate_file_url"]][r["state"]] += 1
    return out


def compute(path, run_date, con, live_status, c1_states):
    d = json.load(open(path, encoding="utf-8"))
    rows = Rows()
    brand = re.search(r"_([a-z0-9-]+)_index\.json$", os.path.basename(path)).group(1)
    url = CENTENE_BASE + os.path.basename(path)
    st, err = live_status(url)
    rows.put("I1", st, None, st, 1.0 if st in (200, 206) else 0.0, f"HTTP {st} {url} {err}".strip())
    if st not in (200, 206):
        for mid, _, _ in METRICS[1:]:
            rows.put(mid, measured=False, note="index unreachable")
        return brand, rows
    rs = d.get("reporting_structure") or []
    plans = [(i, p) for i, s in enumerate(rs) for p in (s.get("reporting_plans") or [])]
    files = [f for s in rs for f in (s.get("in_network_files") or [])]
    aas = [s["allowed_amount_file"] for s in rs if s.get("allowed_amount_file")]

    lu = d.get("last_updated_on")
    try:
        days = (dt.date.fromisoformat(run_date) - dt.date.fromisoformat(lu)).days
        rows.put("I2", days, None, days, round(curve(days), 6), f"last_updated_on {lu}")
    except (TypeError, ValueError):
        rows.put("I2", measured=False, note=f"last_updated_on missing or invalid ({lu})")

    # Compliance
    parts = [("root", int(all(present(d, k) for k in REQ["root"]) and date_ok(lu)), 1),
             ("reporting_structure", sum(present(s, "reporting_plans") for s in rs), len(rs)),
             ("plan", sum(all(present(p, k) for k in REQ["plan"]) for _, p in plans), len(plans)),
             ("file_location", sum(all(present(f, k) for k in REQ["file"]) for f in files + aas), len(files + aas))]
    shares = [n / dn for _, n, dn in parts if dn]
    rows.put("I3", sum(p[1] for p in parts), sum(p[2] for p in parts), round(sum(shares) / len(shares), 6),
             round(sum(shares) / len(shares), 6), "average over object types: " +
             ", ".join(f"{k} {n:,}/{dn:,}" for k, n, dn in parts if dn))
    ev = [(p.get("plan_id_type") in ("ein", "hios")) for _, p in plans] + \
         [(p.get("plan_market_type") in ("group", "individual")) for _, p in plans]
    rows.ratio("I4", sum(ev), len(ev))
    cond = [present(p, "plan_sponsor_name") for _, p in plans if p.get("plan_id_type") == "ein"] + \
           [bool(s.get("in_network_files") or s.get("allowed_amount_file")) for s in rs]
    rows.ratio("I5", sum(cond), len(cond))
    match, level = collections.Counter(), collections.Counter()
    for _, p in plans:
        pid, typ = str(p.get("plan_id", "")).strip(), p.get("plan_id_type")
        kind, state = hios_kind(pid)
        if typ == "ein":
            ok = tin_valid("ein", pid)[0] in ("valid", "padded")
            match["ein_ok" if ok else ("ein_holds_hios" if kind else "ein_bad")] += 1
            level["ein" if ok else f"ein_typed_{kind or 'other'}"] += 1
        elif typ == "hios":
            ok = kind is not None and (state is None or state in STATES)
            match["hios_ok" if ok else ("hios_bad_state" if kind else "hios_bad")] += 1
            level[f"hios_{kind or 'other'}"] += 1
        else:
            match["bad_type"] += 1
            level["bad_type"] += 1
    ok_match = match["ein_ok"] + match["hios_ok"]
    rows.ratio("I6", ok_match, len(plans), note=", ".join(f"{k} {v:,}" for k, v in sorted(match.items())))
    # Rule (b)(1)(i)(A): 14-digit HIOS plan ID, else 5-digit HIOS issuer ID, else EIN. Issuer-level IDs are
    # accepted (we cannot tell whether a 14-digit ID was available); 10-character product IDs are not allowed.
    ok_level = level["hios_plan"] + level["hios_issuer"] + level["ein"]
    rows.ratio("I7", ok_level, len(plans), note=", ".join(f"{k} {v:,}" for k, v in sorted(level.items())))
    rows.ratio("I8", len(aas), len(rs))

    # Consistency
    inn_urls = sorted({f["location"] for f in files if f.get("location")})
    aa_urls = sorted({a["location"] for a in aas if a.get("location")})
    inn_st = {u: live_status(u)[0] for u in inn_urls}
    aa_st = {u: live_status(u)[0] for u in aa_urls}
    rows.ratio("I9", sum(v in (200, 206) for v in inn_st.values()), len(inn_st),
               note="; ".join(f"{label_of(u) or u}: {v}" for u, v in inn_st.items() if v not in (200, 206)))
    rows.ratio("I10", sum(v in (200, 206) for v in aa_st.values()), len(aa_st))
    checked, passed, notes = 0, 0, []
    for i, s in enumerate(rs):
        sp = [p for j, p in plans if j == i]
        hios_states = collections.Counter(hios_kind(str(p.get("plan_id", "")))[1] for p in sp
                                          if p.get("plan_id_type") == "hios")
        for k in [k for k in hios_states if k not in STATES]:  # e.g. 'EP' in 25303EP000 (already an I6 failure)
            hios_states.pop(k)
        for f in s.get("in_network_files") or []:
            lab = label_of(os.path.basename(f.get("location", "")))
            fs = file_state(con, lab)
            if not fs:
                continue
            states = hios_states or c1_states[f["location"]]
            plan_state = states.most_common(1)[0][0] if states else None
            top = fs["top3"][0]["st"] if fs["top3"] else None
            checked += 1
            passed += top == plan_state
            disagree = fs["top_dac"] != fs["top_nppes"]
            if top != plan_state or disagree:
                notes.append(f"{lab}: plans {plan_state}, providers " +
                             ", ".join(f"{x['st']} {x['n']:,}" for x in fs["top3"]) +
                             f" ({fs['in_dac']:,} of {fs['npis']:,} NPIs in the Medicare file; top by Medicare "
                             f"{fs['top_dac']}, by NPPES {fs['top_nppes']})")
    rows.ratio("I11", passed, checked, note="; ".join(notes) or ("every linked file's top provider state is the plans' state"
                                                                  if checked else "no linked rate file could be read"))
    ids = collections.defaultdict(set)
    for _, p in plans:
        ids[p.get("plan_id")].add((p.get("plan_id_type"), p.get("plan_market_type"), p.get("issuer_name")))
    rows.ratio("I12", sum(len(v) == 1 for v in ids.values()), len(ids))

    # Usability
    key = lambda i, p: (json.dumps(p, sort_keys=True), tuple(sorted(f.get("location", "") for f in
                                                                   (rs[i].get("in_network_files") or []))))
    keys = [key(i, p) for i, p in plans]
    rows.ratio("I13", len(keys) - len(set(keys)), len(keys), invert=True)
    where = collections.defaultdict(set)
    for i, p in plans:
        where[json.dumps(p, sort_keys=True)].add(i)
    rows.ratio("I14", sum(len(v) == 1 for v in where.values()), len(where))
    locs = [f.get("location", "") for f in files + aas]
    rows.ratio("I15", sum(u.startswith("https://") for u in locs), len(locs))
    return brand, rows


def score(rows):
    groups = collections.defaultdict(list)
    for mid, _, g in METRICS:
        s = rows.r.get(mid, {}).get("score")
        if g in GROUPS and s is not None:
            groups[g].append(s)
    gs = {g: sum(v) / len(v) for g, v in groups.items()}
    w = sum(GROUPS[g] for g in gs)
    return gs, (rows.r["I1"]["score"] or 0) * (sum(GROUPS[g] * s for g, s in gs.items()) / w if w else 0)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-date", default=dt.date.today().isoformat())
    ap.add_argument("--index", default=os.path.join(DATA, "*_index.json"))
    args = ap.parse_args()
    con = duckdb.connect()
    cache = {}
    c1_states = plan_states_by_file()

    def live(u):
        if u not in cache:
            cache[u] = reachable(u)
        return cache[u]

    metric_rows, index_rows = [], {}
    for path in sorted(glob.glob(args.index)):
        brand, rows = compute(path, args.run_date, con, live, c1_states)
        gs, s = score(rows)
        for mid, name, g in METRICS:
            r = rows.r.get(mid, {"measured": False, "note": "not computed"})
            metric_rows.append({"index": os.path.basename(path), "dataset": brand, "metric_id": mid, "metric": name,
                                "group": g, **{k: r.get(k) for k in ("numerator", "denominator", "value", "score",
                                                                     "measured", "note")}})
        index_rows[brand] = {"dataset": brand, "index": os.path.basename(path), "gate": rows.r["I1"]["score"],
                             **{g: (round(gs[g], 4) if g in gs else None) for g in GROUPS},
                             "index_score": round(s, 4)}
        print(f"{brand}: index {s:.3f} {index_rows[brand]}", file=sys.stderr)

    with open(os.path.join(C3, "c3_rate_file_scores.csv"), encoding="utf-8") as f:
        files = collections.defaultdict(list)
        for r in csv.DictReader(f):
            files[r["dataset"]].append(float(r["file_score"]))
    ds = []
    for brand, ir in sorted(index_rows.items()):
        fs = files.get(brand, [])
        mean_files = sum(fs) / len(fs) if fs else 0.0
        ds.append({"dataset": brand, "index_score": ir["index_score"], "rate_files": len(fs),
                   "rate_files_reachable": sum(1 for x in fs if x > 0), "mean_rate_file_score": round(mean_files, 4),
                   "min_rate_file_score": round(min(fs), 4) if fs else None,
                   "dataset_score": round(ir["index_score"] ** INDEX_SHARE * mean_files ** (1 - INDEX_SHARE), 4)})
    for name, rows in (("c3_index_metrics.csv", metric_rows), ("c3_index_scores.csv", list(index_rows.values())),
                       ("c3_dataset_scores.csv", ds)):
        with open(os.path.join(C3, name), "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    print(json.dumps(ds, indent=1))


if __name__ == "__main__":
    main()
