"""C2b step 1: build the provider group tables for the in-scope rate files.

Streams the `provider_references` section of every rate file used by the
in-scope plans (California by default) and writes two normalized tables:

  c2b_groups.parquet        one row per rate file x provider group x member x NPI
                            (rate_file, provider_group_id, member_index, tin_type,
                            tin, business_name, npi, network_name)
  c2b_group_summary.parquet one row per rate file x provider group
                            (n_members, n_tins, n_npis, member_signature, ...)

`member_signature` is a hash of the group's sorted (TIN, NPI) pairs, so groups
that list exactly the same providers share a signature (used in steps 4-5).

Prices are not read here; they stay normalized by provider_group_id and are
never expanded to one row per NPI.

Usage:
    python pipeline/c2b_step1_groups.py
    python pipeline/c2b_step1_groups.py --plans output/c1/c1_plans_ca.csv --rate-dir Data/rate_files --out output/c2b
"""
import argparse
import concurrent.futures as cf
import csv
import hashlib
import json
import os
import sys
import time
from collections import Counter

import duckdb
import ijson

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PLANS = os.path.join(PROJECT_ROOT, "output", "c1", "c1_plans_ca.csv")
DEFAULT_RATE_DIR = os.path.join(PROJECT_ROOT, "Data", "rate_files")
DEFAULT_OUT = os.path.join(PROJECT_ROOT, "output", "c2b")

MEMBER_COLS = ["rate_file", "provider_group_id", "member_index", "tin_type", "tin",
               "business_name", "npi", "network_name"]
GROUP_COLS = ["rate_file", "provider_group_id", "n_members", "n_tins", "n_npis",
              "n_tin_npi_pairs", "network_name", "member_signature", "business_names"]


def short_name(file_name):
    """'2026-09-30_centene-management-company-llc_healthnet-ca_in-network.json' -> 'healthnet-ca'."""
    stem = file_name.replace("_in-network.json", "")
    return stem.split("_")[-1]


def rate_files_for(plans_path, rate_dir):
    """Unique rate files referenced by the in-scope plans, with local paths."""
    with open(plans_path, newline="", encoding="utf-8") as f:
        names = sorted({row["rate_file_name"] for row in csv.DictReader(f) if row["rate_file_name"]})
    files = []
    for name in names:
        path = os.path.join(rate_dir, name)
        if not os.path.isfile(path):
            raise SystemExit(f"Rate file not found: {path}  (download it; see handoff/DATA_SOURCES.md)")
        files.append((short_name(name), path))
    return files


def parse_groups(job):
    """Stream one rate file's provider_references into two temporary CSVs."""
    label, path, tmp_dir = job
    t0 = time.time()
    members_csv = os.path.join(tmp_dir, f"{label}_members.csv")
    groups_csv = os.path.join(tmp_dir, f"{label}_groups.csv")
    stats = Counter()
    with open(path, "rb") as src, \
            open(members_csv, "w", newline="", encoding="utf-8") as fm, \
            open(groups_csv, "w", newline="", encoding="utf-8") as fg:
        wm, wg = csv.writer(fm), csv.writer(fg)
        for ref in ijson.items(src, "provider_references.item"):
            gid = ref.get("provider_group_id")
            net = ";".join(ref.get("network_name") or [])
            pairs, tins, npis, names = set(), set(), set(), []
            members = ref.get("provider_groups") or []
            for i, member in enumerate(members):
                tin = member.get("tin") or {}
                tin_value = str(tin.get("value", "")).strip()
                name = tin.get("business_name", "") or ""
                names.append(name)
                tins.add(tin_value)
                for npi in member.get("npi") or []:
                    npi = str(npi)
                    wm.writerow([label, gid, i, tin.get("type", ""), tin_value, name, npi, net])
                    pairs.add((tin_value, npi))
                    npis.add(npi)
                    stats["member_npi_rows"] += 1
            signature = hashlib.sha1("|".join(f"{t}:{n}" for t, n in sorted(pairs)).encode()).hexdigest()[:16]
            wg.writerow([label, gid, len(members), len(tins), len(npis), len(pairs), net, signature,
                         " | ".join(dict.fromkeys(names))])
            stats["groups"] += 1
            stats["groups_with_network_name"] += bool(net)
    stats["seconds"] = round(time.time() - t0, 1)
    return label, members_csv, groups_csv, dict(stats)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plans", default=DEFAULT_PLANS, help="in-scope plan table (default: output/c1/c1_plans_ca.csv)")
    ap.add_argument("--rate-dir", default=DEFAULT_RATE_DIR, help="folder holding the rate files (default: Data/rate_files)")
    ap.add_argument("--out", default=DEFAULT_OUT, help="output folder (default: output/c2b)")
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 2)
    args = ap.parse_args()

    files = rate_files_for(args.plans, args.rate_dir)
    tmp_dir = os.path.join(args.out, "_tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    print(f"parsing provider groups in {len(files)} rate file(s): {[f for f, _ in files]}", file=sys.stderr)

    with cf.ProcessPoolExecutor(max_workers=min(args.workers, len(files))) as pool:
        results = list(pool.map(parse_groups, [(label, path, tmp_dir) for label, path in files]))

    con = duckdb.connect()
    members = ", ".join(f"'{m}'" for _, m, _, _ in results)
    groups = ", ".join(f"'{g}'" for _, _, g, _ in results)
    con.execute(f"""create table m as select * from read_csv([{members}], header=false, all_varchar=true,
                    names={MEMBER_COLS})""")
    con.execute(f"""create table g as select * from read_csv([{groups}], header=false, all_varchar=true,
                    names={GROUP_COLS})""")
    for t, int_cols in (("m", ["provider_group_id", "member_index"]),
                        ("g", ["provider_group_id", "n_members", "n_tins", "n_npis", "n_tin_npi_pairs"])):
        for col in int_cols:
            con.execute(f"alter table {t} alter {col} type integer")
    groups_path = os.path.join(args.out, "c2b_groups.parquet")
    summary_path = os.path.join(args.out, "c2b_group_summary.parquet")
    con.execute(f"copy m to '{groups_path}' (format parquet)")
    con.execute(f"copy g to '{summary_path}' (format parquet)")

    q = lambda sql: con.execute(sql).fetchall()
    per_file = {}
    for label, _, _, stats in results:
        r = q(f"""select count(distinct provider_group_id), count(*), count(distinct tin), count(distinct npi),
                         count(distinct tin || ':' || npi) from m where rate_file = '{label}'""")[0]
        dup = q(f"""select count(*), coalesce(sum(n), 0) from (select member_signature, count(*) n from g
                    where rate_file = '{label}' group by 1 having count(*) > 1)""")[0]
        per_file[label] = {**stats, "groups": r[0], "member_npi_rows": r[1], "unique_tins": r[2],
                           "unique_npis": r[3], "unique_tin_npi_pairs": r[4],
                           "repeated_member_sets": dup[0], "groups_in_repeated_sets": dup[1]}
    totals = q("select count(distinct npi), count(distinct tin), count(distinct tin || ':' || npi) from m")[0]
    summary = {"rate_files": per_file, "all_files": {"unique_npis": totals[0], "unique_tins": totals[1],
                                                     "unique_tin_npi_pairs": totals[2]},
               "outputs": [groups_path, summary_path]}
    with open(os.path.join(args.out, "c2b_step1_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    for _, mpath, gpath, _ in results:
        os.remove(mpath)
        os.remove(gpath)
    os.rmdir(tmp_dir)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
