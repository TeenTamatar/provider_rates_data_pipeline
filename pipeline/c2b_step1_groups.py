"""C2b step 1: build the provider group tables for the in-scope rate files.

Reads the shared scan (pipeline/rate_scan.py, output/scan/) for every rate file used by the in-scope
plans (California by default), so no rate file is parsed again, and writes two normalized tables:

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
    python pipeline/rate_scan.py            # once, after C1
    python pipeline/c2b_step1_groups.py
    python pipeline/c2b_step1_groups.py --plans output/c1/c1_plans_ca.csv --scan output/scan --out output/c2b
"""
import argparse
import csv
import json
import os

import duckdb

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PLANS = os.path.join(PROJECT_ROOT, "output", "c1", "c1_plans_ca.csv")
DEFAULT_SCAN = os.path.join(PROJECT_ROOT, "output", "scan")
DEFAULT_OUT = os.path.join(PROJECT_ROOT, "output", "c2b")


def short_name(file_name):
    """'2026-09-30_centene-management-company-llc_healthnet-ca_in-network.json' -> 'healthnet-ca'."""
    stem = file_name.replace("_in-network.json", "")
    return stem.split("_")[-1]


def scanned_files_for(plans_path, scan_dir, table):
    """(label, scan table path) for every rate file the in-scope plans use; the scan must exist."""
    with open(plans_path, newline="", encoding="utf-8") as f:
        names = sorted({row["rate_file_name"] for row in csv.DictReader(f) if row["rate_file_name"]})
    files = []
    for name in names:
        label = short_name(name)
        path = os.path.join(scan_dir, f"{label}_{table}.parquet").replace("\\", "/")
        if not os.path.isfile(path):
            raise SystemExit(f"Scan not found: {path}  (run pipeline/rate_scan.py after C1; the rate file must be "
                             "downloaded or streamed, see handoff/DATA_SOURCES.md)")
        files.append((label, path))
    return files


def nonempty(col, alias=None):
    """Empty text becomes NULL, as when these tables were built from CSV."""
    return f"nullif({col}, '') {alias or col.split('.')[-1]}"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plans", default=DEFAULT_PLANS, help="in-scope plan table (default: output/c1/c1_plans_ca.csv)")
    ap.add_argument("--scan", default=DEFAULT_SCAN, help="shared scan folder (default: output/scan)")
    ap.add_argument("--out", default=DEFAULT_OUT, help="output folder (default: output/c2b)")
    args = ap.parse_args()

    members = scanned_files_for(args.plans, args.scan, "members")
    groups = scanned_files_for(args.plans, args.scan, "groups")
    os.makedirs(args.out, exist_ok=True)
    con = duckdb.connect()
    m_union = " union all ".join(
        f"""select '{label}' rate_file, cast(m.provider_group_id as integer) provider_group_id,
                   cast(m.member_index as integer) member_index, {nonempty('m.tin_type')}, {nonempty('m.tin_raw', 'tin')},
                   {nonempty('m.business_name')}, {nonempty('m.npi')}, {nonempty('g.network_name_raw', 'network_name')}
            from '{mp}' m join '{gp}' g using (provider_group_id)"""
        for (label, mp), (_, gp) in zip(members, groups))
    g_union = " union all ".join(
        f"""select '{label}' rate_file, cast(provider_group_id as integer) provider_group_id,
                   cast(n_members as integer) n_members, cast(n_tins as integer) n_tins, cast(n_npis as integer) n_npis,
                   cast(n_tin_npi_pairs as integer) n_tin_npi_pairs, nullif(network_name_raw, '') network_name,
                   {nonempty('member_signature')}, {nonempty('business_names')}
            from '{gp}'""" for label, gp in groups)
    con.execute(f"create table m as {m_union}")
    con.execute(f"create table g as {g_union}")
    groups_path = os.path.join(args.out, "c2b_groups.parquet")
    summary_path = os.path.join(args.out, "c2b_group_summary.parquet")
    con.execute(f"copy m to '{groups_path}' (format parquet)")
    con.execute(f"copy g to '{summary_path}' (format parquet)")

    q = lambda sql: con.execute(sql).fetchall()
    per_file = {}
    for label, _ in members:
        r = q(f"""select count(distinct provider_group_id), count(*), count(distinct tin), count(distinct npi),
                         count(distinct tin || ':' || npi) from m where rate_file = '{label}'""")[0]
        dup = q(f"""select count(*), coalesce(sum(n), 0) from (select member_signature, count(*) n from g
                    where rate_file = '{label}' group by 1 having count(*) > 1)""")[0]
        nn = q(f"select count(*) filter (where network_name is not null) from g where rate_file = '{label}'")[0][0]
        per_file[label] = {"member_npi_rows": r[1], "groups": r[0], "groups_with_network_name": nn,
                           "unique_tins": r[2], "unique_npis": r[3], "unique_tin_npi_pairs": r[4],
                           "repeated_member_sets": dup[0], "groups_in_repeated_sets": dup[1]}
    totals = q("select count(distinct npi), count(distinct tin), count(distinct tin || ':' || npi) from m")[0]
    summary = {"rate_files": per_file, "all_files": {"unique_npis": totals[0], "unique_tins": totals[1],
                                                     "unique_tin_npi_pairs": totals[2]},
               "source": "shared scan (pipeline/rate_scan.py)", "outputs": [groups_path, summary_path]}
    with open(os.path.join(args.out, "c2b_step1_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
