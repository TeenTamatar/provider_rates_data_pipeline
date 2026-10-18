#!/usr/bin/env python3
"""High-level metrics of a pipeline run, read from the stage outputs.

Writes output/RUN_SUMMARY.md (for people) and output/run_summary.json (for tools).
Run after the pipeline:  python pipeline/run_summary.py
"""
import argparse
import datetime as dt
import glob
import json
import os

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def jload(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def share(n, d):
    return round(n / d, 4) if d else None


def pct(x):
    return "n/a" if x is None else f"{100 * x:.1f}%"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=os.path.join(ROOT, "output"))
    a = ap.parse_args()
    O = lambda *p: os.path.join(ROOT, "output", *p)
    S = {}

    # C1: index files
    c1 = jload(O("c1", "c1_summary.json"))
    S["index_files_parsed"] = c1["index_files_parsed"]
    S["plan_rows_parsed"] = c1["plan_rows_raw"]
    S["unique_plan_rows"] = c1["plan_rows_unique"]
    S["unique_plan_ids"] = c1["unique_plan_ids"]
    S["rate_files_linked"] = c1["unique_rate_files"]
    S["in_scope_plan_rows"] = c1["in_scope_plan_rows"]
    S["in_scope_rate_files"] = c1["in_scope_rate_files"]

    # C2: PPO plans and files
    net = pd.read_csv(O("c2", "c2_plan_network.csv"), dtype=str)
    S["ppo_plans"] = int((net.network_class == "ppo").sum())
    S["non_ppo_plans"] = int((net.network_class == "non_ppo").sum()) if "non_ppo" in set(net.network_class) else int((net.network_class != "ppo").sum())
    S["ppo_plan_rate_files"] = sorted(net[net.network_class == "ppo"].rate_file_name.unique().tolist())

    # Rate files scanned
    px = 0
    for f in glob.glob(O("scan", "*_stats.json")):
        px += jload(f)["counts"].get("price_objects", 0)
    S["rate_file_price_rows_scanned_all_files"] = px

    # C2b / C4 / C6: rates extracted
    c2b = jload(O("c2b", "c2b_step6_summary.json"))
    S["ppo_rate_file_price_rows"] = c2b["kept"]["raw_price_rows"]
    S["ppo_rate_rows_deduplicated"] = c2b["kept"]["rows"]
    S["ppo_provider_service_rates"] = c2b["kept"]["pair_keys"]
    S["ppo_unique_npis"] = c2b["kept"]["npis"]
    S["ppo_unique_tins"] = c2b["kept"]["tins"]
    S["ppo_tin_npi_pairs"] = c2b["kept"]["tin_npi_pairs"]
    S["ppo_billing_codes"] = c2b["kept"]["billing_codes"]
    c4 = jload(O("c4", "c4_summary.json"))
    S["c4_rates_chosen"] = c4["chosen_keys"]
    c6 = jload(O("c6", "c6_summary.json"))
    S["final_rate_rows"] = c6["rows"]
    S["final_unique_npis"] = c6["npis"]
    S["final_unique_tins"] = c6["tins"]
    S["final_tin_npi_pairs"] = c6["tin_npi_pairs"]
    S["final_billing_codes"] = c6["billing_codes"]

    # C3: file quality
    ds = pd.read_csv(O("c3", "c3_dataset_scores.csv"))
    S["dataset_scores"] = {r.dataset: round(float(r.dataset_score), 4) for r in ds.itertuples()}
    fs = pd.read_csv(O("c3", "c3_rate_file_scores.csv"))
    ok = fs[fs.gate > 0]
    S["rate_files_scored"] = int(len(fs))
    S["rate_files_reachable"] = int(len(ok))
    S["rate_file_score_mean_reachable"] = round(float(ok.file_score.mean()), 4)
    S["rate_file_score_min_reachable"] = round(float(ok.file_score.min()), 4)
    S["rate_file_score_max_reachable"] = round(float(ok.file_score.max()), 4)
    idx = pd.read_csv(O("c3", "c3_index_scores.csv"))
    S["index_scores"] = {r.dataset: round(float(r.index_score), 4) for r in idx.itertuples()}

    # C5: verified address share
    ad = pd.read_csv(O("c5", "c5_addresses.csv"), dtype=str)
    n_pairs = len(ad)
    npis = ad.groupby("npi").label.agg(lambda x: "verified" if (x == "verified").any() else ("probable" if (x == "probable").any() else "unverified"))
    S["c5_pairs"] = n_pairs
    S["c5_pairs_by_label"] = ad.label.value_counts().to_dict()
    S["c5_npis"] = int(len(npis))
    S["c5_npis_by_label"] = npis.value_counts().to_dict()
    S["share_npis_verified_address"] = share(int((npis == "verified").sum()), len(npis))
    S["share_npis_verified_or_probable"] = share(int((npis != "unverified").sum()), len(npis))
    if "label_loose" in ad.columns:
        nl = ad.groupby("npi").label_loose.agg(lambda x: x.str.startswith("verified").any())
        S["share_npis_verified_loose"] = share(int(nl.sum()), len(nl))
    S["c5_families_in_claims"] = jload(O("c5", "c5_summary.json")).get("claims_and_evidence", {}).get("claims_by_family", {})

    # C7
    c7 = jload(O("c7", "c7_summary.json"))
    S["c7_prices"] = c7["rows"]
    S["c7_prices_compared"] = c7["rows_compared"]
    S["c7_npi_labels"] = c7["npi_labels"]

    S["generated_at"] = dt.datetime.now().isoformat(timespec="seconds")
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "run_summary.json"), "w", encoding="utf-8") as f:
        json.dump(S, f, indent=2, sort_keys=True)

    L = []
    L.append("# Run summary\n")
    L.append(f"Generated {S['generated_at']} from the stage outputs in `output/`. Machine-readable copy: `run_summary.json`.\n")
    L.append("## Inputs and scope\n")
    L.append("| Metric | Value |\n|---|---|")
    L.append(f"| Index files parsed | {S['index_files_parsed']} |")
    L.append(f"| Plan rows parsed (unique after de-duplication) | {S['plan_rows_parsed']:,} ({S['unique_plan_rows']:,}) |")
    L.append(f"| Unique plan IDs | {S['unique_plan_ids']:,} |")
    L.append(f"| Rate files linked by the indexes | {S['rate_files_linked']} |")
    L.append(f"| In-scope plans (California, commercial group) | {S['in_scope_plan_rows']:,} on {S['in_scope_rate_files']} rate file |")
    L.append(f"| **PPO plans identified** | **{S['ppo_plans']}** (non-PPO: {S['non_ppo_plans']}) |")
    L.append(f"| **Rate files carrying the PPO plans** | **{len(S['ppo_plan_rate_files'])}** |")
    L.append(f"| Price rows scanned in all rate files | {S['rate_file_price_rows_scanned_all_files']:,} |\n")
    L.append("## File quality scores (0 to 1)\n")
    L.append("| Dataset | Index score | Dataset score |\n|---|---|---|")
    for k in sorted(S["dataset_scores"]):
        L.append(f"| {k} | {S['index_scores'].get(k, float('nan')):.3f} | {S['dataset_scores'][k]:.3f} |")
    L.append(f"\nRate files: {S['rate_files_reachable']} of {S['rate_files_scored']} reachable; score of the reachable ones mean {S['rate_file_score_mean_reachable']:.3f}, min {S['rate_file_score_min_reachable']:.3f}, max {S['rate_file_score_max_reachable']:.3f}. Per-file scores: `output/c3/c3_rate_file_scores.csv`.\n")
    L.append("## Rates extracted\n")
    L.append("| Metric | Value |\n|---|---|")
    L.append(f"| PPO price rows kept from the rate file | {S['ppo_rate_file_price_rows']:,} |")
    L.append(f"| After de-duplication (TIN-NPI x group x service x rate) | {S['ppo_rate_rows_deduplicated']:,} |")
    L.append(f"| Provider x service rates (PPO) | {S['ppo_provider_service_rates']:,} |")
    L.append(f"| One rate per key chosen by C4 | {S['c4_rates_chosen']:,} |")
    L.append(f"| **Rows in the final provider rate file** | **{S['final_rate_rows']:,}** |")
    L.append(f"| Unique NPIs (PPO set, final file) | {S['ppo_unique_npis']:,}, {S['final_unique_npis']:,} |")
    L.append(f"| Unique TINs (PPO set, final file) | {S['ppo_unique_tins']:,}, {S['final_unique_tins']:,} |")
    L.append(f"| TIN-NPI pairs (PPO set, final file) | {S['ppo_tin_npi_pairs']:,}, {S['final_tin_npi_pairs']:,} |")
    L.append(f"| Billing codes (final file) | {S['final_billing_codes']:,} |\n")
    L.append("## Addresses (C5)\n")
    L.append("| Metric | Value |\n|---|---|")
    L.append(f"| NPIs located | {S['c5_npis']:,} |")
    for lab in ("verified", "probable", "unverified"):
        n = S["c5_npis_by_label"].get(lab, 0)
        L.append(f"| NPIs with a {lab} address | {n:,} ({pct(share(n, S['c5_npis']))}) |")
    L.append(f"| **Share of NPIs linked to a verified address** | **{pct(S['share_npis_verified_address'])}** |")
    if "share_npis_verified_loose" in S:
        L.append(f"| Same under the loose profile | {pct(S['share_npis_verified_loose'])} |")
    L.append(f"\nEvidence families present in the claims: {', '.join(f'{k} ({v:,})' for k, v in S['c5_families_in_claims'].items()) or 'n/a'}. An NPI counts as verified when any of its TIN-NPI pairs is verified (strict: two independent families agree and the newest evidence is at most 24 months old).\n")
    L.append("## Cost-effectiveness (C7)\n")
    L.append("| Metric | Value |\n|---|---|")
    L.append(f"| Prices scored | {S['c7_prices']:,} (compared with peers: {S['c7_prices_compared']:,}) |")
    for k, v in S["c7_npi_labels"].items():
        L.append(f"| Providers labelled `{k}` | {v:,} |")
    with open(os.path.join(a.out, "RUN_SUMMARY.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print("wrote", os.path.join(a.out, "RUN_SUMMARY.md"))


if __name__ == "__main__":
    main()
