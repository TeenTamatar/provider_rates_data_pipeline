"""C2b step 2: label provider groups from network names and network markers in TIN business names.

Reads the group table from step 1 and two pattern tables (first matching pattern wins, case-insensitive):
  1. `reference/network_names.csv`, matched on the group's `network_name` (the schema field naming the
     network the group belongs to). Used first, when a name states a network type: a group listed in the
     target network is a target member, one listed only in other networks is other_network. Names that state
     no type (e.g. Ambetter's "Exchange Core National") are ignored.
  2. `reference/business_name_labels.csv`, matched on the TIN business name.
Each TIN entry (group member) gets at most one pattern. The marker belongs to the
member, not the whole group: one provider group often lists "PPO: <County>",
"DIRECT NETWORK: AMBETTER HMO" and plain practice names side by side. So exclusions
drop members, and a group is excluded only when every member is excluded.

Markers carry a network type, so any target network works (`--network`, default PPO): a marker of the
target type labels the member with the target (e.g. `ppo`), a marker of another type `other_network`;
markers flagged out of scope (Ambetter, individual market) keep their own exclusion label.

  member label     <target> / other_network / other_product (strong), or a hint
                   (direct_network, direct_ffs_commercial, ipa), or none
  group label      <target>_labelled      >= 1 target member, no excluded member
                   <target>_and_excluded  target members and excluded members
                   partly_excluded        excluded members, no target member, some unmarked
                   excluded               every member excluded
                   none                   no strong marker (hints are listed separately)

Outputs (output/c2b/):
  c2b_step2_name_patterns.csv       every pattern with its counts, plus business-name
                                    prefixes ("XXX:") that no pattern matched
  c2b_step2_member_labels.csv       one row per TIN entry (group member) with its marker
  c2b_step2_group_name_labels.csv   one row per rate file x provider group
  c2b_step2_summary.json

Usage:
    python pipeline/c2b_step2_name_labels.py
    python pipeline/c2b_step2_name_labels.py --groups output/c2b/c2b_groups.parquet --patterns reference/business_name_labels.csv --out output/c2b
    python pipeline/c2b_step2_name_labels.py --network HMO --groups output/c2b_hmo/c2b_groups.parquet --out output/c2b_hmo
"""
import argparse
import csv
import json
import os
import re
from collections import Counter, defaultdict

import duckdb

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_GROUPS = os.path.join(PROJECT_ROOT, "output", "c2b", "c2b_groups.parquet")
DEFAULT_PATTERNS = os.path.join(PROJECT_ROOT, "reference", "business_name_labels.csv")
DEFAULT_NETWORK_NAMES = os.path.join(PROJECT_ROOT, "reference", "network_names.csv")
DEFAULT_OUT = os.path.join(PROJECT_ROOT, "output", "c2b")

EXCLUDE_LABELS = {"other_network", "other_product"}


def load_patterns(path):
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        row["rx"] = re.compile(row["regex"], re.IGNORECASE)
    return rows


def resolve(p, target):
    """The member label a business-name pattern gives for this target network."""
    if p["strength"] != "strong" or p.get("out_of_scope", "no") == "yes" or not p.get("network_type"):
        return p["label"]
    return target.lower() if p["network_type"] == target else "other_network"


def network_name_match(network_name, name_patterns, target):
    """(pattern row, label) from the group's network_name, or (None, None) when no name states a type."""
    types = {}
    for part in (network_name or "").split(";"):
        for p in name_patterns:
            if p["rx"].search(part):
                types.setdefault(p["network_type"], p)
    if not types:
        return None, None
    if target in types:
        return types[target], target.lower()
    return next(iter(types.values())), "other_network"


def match(name, patterns):
    """First pattern that matches the business name, with the county if the pattern captures one."""
    for p in patterns:
        m = p["rx"].search(name or "")
        if m:
            county = m.groupdict().get("county") or ""
            return p, county.strip()
    return None, ""


def group_label(n_members, n_target, n_excluded, target="PPO"):
    t = target.lower()
    if n_excluded and n_excluded == n_members:
        return "excluded"
    if n_target and n_excluded:
        return f"{t}_and_excluded"
    if n_target:
        return f"{t}_labelled"
    if n_excluded:
        return "partly_excluded"
    return "none"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--groups", default=DEFAULT_GROUPS, help="step 1 group table (default: output/c2b/c2b_groups.parquet)")
    ap.add_argument("--patterns", default=DEFAULT_PATTERNS, help="pattern table (default: reference/business_name_labels.csv)")
    ap.add_argument("--network-names", default=DEFAULT_NETWORK_NAMES,
                    help="network_name pattern table (default: reference/network_names.csv)")
    ap.add_argument("--network", default="PPO", help="target network type (default: PPO)")
    ap.add_argument("--out", default=DEFAULT_OUT, help="output folder (default: output/c2b)")
    args = ap.parse_args()
    target, t = args.network.upper(), args.network.lower()

    patterns = load_patterns(args.patterns)
    name_patterns = load_patterns(args.network_names) if os.path.exists(args.network_names) else []
    con = duckdb.connect()
    con.execute(f"create view m as select * from read_parquet('{args.groups}')")
    # One row per TIN entry (group member), with its NPIs.
    entries = con.execute("""select rate_file, provider_group_id, member_index, tin, business_name, list(distinct npi),
                                    any_value(network_name)
                             from m group by 1, 2, 3, 4, 5 order by 1, 2, 3""").fetchall()
    all_groups = con.execute("select distinct rate_file, provider_group_id from m order by 1, 2").fetchall()

    per_pattern = defaultdict(lambda: {"tin_entries": 0, "tins": set(), "groups": set(), "pairs": set(), "example": ""})
    unmatched_prefix = defaultdict(lambda: {"tin_entries": 0, "tins": set(), "groups": set(), "pairs": set(), "example": ""})
    groups = defaultdict(lambda: {"strong": [], "hints": [], "patterns": Counter(), "counties": set(),
                                  "n_members": 0, "n_target": 0, "n_excluded": 0})
    member_rows = []
    by_network_name = Counter()
    for rate_file, gid, member_index, tin, name, npis, network_name in entries:
        g = groups[(rate_file, gid)]
        g["n_members"] += 1
        np_, label = network_name_match(network_name, name_patterns, target)
        if np_:
            # The network_name field states a type: it decides before any business-name marker.
            by_network_name[label] += 1
            p, county = {**np_, "pattern_id": f"network_name:{np_['pattern_id']}", "strength": "strong"}, ""
        else:
            p, county = match(name, patterns)
            label = resolve(p, target) if p else "none"
        member_rows.append([rate_file, gid, member_index, tin, name, len(npis),
                            p["pattern_id"] if p else "", label,
                            p["strength"] if p else "", county])
        if p is None:
            prefix = re.match(r"^([^:]{1,40}):", name or "")
            if prefix:
                s = unmatched_prefix[prefix.group(1).strip()]
                s["tin_entries"] += 1
                s["tins"].add(tin)
                s["groups"].add((rate_file, gid))
                s["pairs"].update((tin, n) for n in npis)
                s["example"] = s["example"] or name
            continue
        s = per_pattern[p["pattern_id"]]
        s["tin_entries"] += 1
        s["tins"].add(tin)
        s["groups"].add((rate_file, gid))
        s["pairs"].update((tin, n) for n in npis)
        s["example"] = s["example"] or name
        g["n_target"] += label == t
        g["n_excluded"] += label in EXCLUDE_LABELS
        g["patterns"][p["pattern_id"]] += 1
        if county:
            g["counties"].add(county)
        (g["strong"] if p["strength"] == "strong" else g["hints"]).append(label)

    os.makedirs(args.out, exist_ok=True)
    pattern_path = os.path.join(args.out, "c2b_step2_name_patterns.csv")
    with open(pattern_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["pattern_id", "label", "strength", "tin_entries", "tins", "groups", "tin_npi_pairs", "example"])
        for p in patterns:
            s = per_pattern[p["pattern_id"]]
            w.writerow([p["pattern_id"], p["label"], p["strength"], s["tin_entries"], len(s["tins"]),
                        len(s["groups"]), len(s["pairs"]), s["example"]])
        for prefix, s in sorted(unmatched_prefix.items()):
            w.writerow([f"unmatched_prefix:{prefix}", "", "", s["tin_entries"], len(s["tins"]),
                        len(s["groups"]), len(s["pairs"]), s["example"]])

    member_path = os.path.join(args.out, "c2b_step2_member_labels.csv")
    with open(member_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["rate_file", "provider_group_id", "member_index", "tin", "business_name", "n_npis",
                    "pattern_id", "member_label", "strength", "county"])
        w.writerows(member_rows)

    label_path = os.path.join(args.out, "c2b_step2_group_name_labels.csv")
    label_counts, hint_counts = Counter(), Counter()
    with open(label_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["rate_file", "provider_group_id", "name_label", "exclude_reason", "hints", "matched_patterns",
                    "n_members", f"n_members_{t}", "n_members_excluded", "counties"])
        for rate_file, gid in all_groups:
            g = groups[(rate_file, gid)]
            label = group_label(g["n_members"], g["n_target"], g["n_excluded"], target)
            reason = ";".join(sorted({lab for lab in g["strong"] if lab in EXCLUDE_LABELS}))
            hints = ";".join(sorted(set(g["hints"])))
            label_counts[label] += 1
            for h in set(g["hints"]):
                hint_counts[f"{label}:{h}"] += 1
            w.writerow([rate_file, gid, label, reason, hints,
                        ";".join(f"{k}:{v}" for k, v in sorted(g["patterns"].items())),
                        g["n_members"], g["n_target"], g["n_excluded"], ";".join(sorted(g["counties"]))])

    # Pairs by member label, and by group label (a pair can sit in groups with different labels).
    con.execute(f"create table mem as select * from read_csv('{member_path}', all_varchar=true)")
    member_pairs = con.execute("""select x.member_label, count(distinct m.tin || ':' || m.npi), count(distinct m.npi)
                                  from m join mem x on x.rate_file = m.rate_file
                                   and x.provider_group_id = cast(m.provider_group_id as varchar)
                                   and x.member_index = cast(m.member_index as varchar)
                                  group by 1 order by 1""").fetchall()
    con.execute(f"create table lab as select * from read_csv('{label_path}', all_varchar=true)")
    pair_rows = con.execute("""select l.name_label, count(distinct m.tin || ':' || m.npi), count(distinct m.npi)
                               from m join lab l on l.rate_file = m.rate_file
                                and l.provider_group_id = cast(m.provider_group_id as varchar)
                               group by 1 order by 1""").fetchall()
    overlap = con.execute("""with p as (select distinct l.name_label, m.tin || ':' || m.npi pair
                                        from m join lab l on l.rate_file = m.rate_file
                                         and l.provider_group_id = cast(m.provider_group_id as varchar))
                             select count(*) from (select pair from p group by 1
                                                   having bool_or(name_label = '{t}_labelled') and bool_or(name_label = 'none'))""".format(t=t)).fetchone()[0]
    summary = {
        "network": target,
        "groups": len(all_groups),
        "groups_by_label": dict(label_counts),
        "groups_with_hint": dict(hint_counts),
        "member_entries_by_label": dict(Counter(r[7] for r in member_rows)),
        "pairs_by_member_label": {r[0]: {"tin_npi_pairs": r[1], "npis": r[2]} for r in member_pairs},
        "pairs_by_group_label": {r[0]: {"tin_npi_pairs": r[1], "npis": r[2]} for r in pair_rows},
        f"{t}_labelled_pairs_also_in_unlabelled_groups": overlap,
        "members_labelled_by_network_name": dict(by_network_name),
        "patterns": {p["pattern_id"]: {"tin_entries": per_pattern[p["pattern_id"]]["tin_entries"],
                                       "groups": len(per_pattern[p["pattern_id"]]["groups"])} for p in patterns},
        "unmatched_prefixes": sorted(unmatched_prefix),
        "outputs": [pattern_path, member_path, label_path],
    }
    with open(os.path.join(args.out, "c2b_step2_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
