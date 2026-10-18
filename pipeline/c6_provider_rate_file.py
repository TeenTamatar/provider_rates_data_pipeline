"""C6: build the provider rate file (deliverable 1).

Joins C4 chosen rates x C5 primary address x specialty, one row per
TIN x NPI x billing code (key). Alternatives (other prices, other address
candidates) go to side tables so rows never multiply.

Specialty: individuals (NPPES entity type 1) use the HiLabs taxonomy crosswalk;
organizations (type 2), and individuals whose taxonomies are not in the
crosswalk, use the NUCC classification of the primary taxonomy.
"""
import argparse
import json
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
NPPES = "Data/NPPES_Data_Dissemination_October_2026_V2/npidata_pfile_20050523-20261011.csv"


def load_taxonomy(nppes, npis):
    """Entity type and ordered taxonomy codes (primary first) for each NPI."""
    cols = ['"NPI"', '"Entity Type Code"']
    for i in range(1, 16):
        cols += [f'"Healthcare Provider Taxonomy Code_{i}"', f'"Healthcare Provider Primary Taxonomy Switch_{i}"']
    con = duckdb.connect()
    con.register("t", pd.DataFrame({"npi": sorted(npis)}))
    df = con.execute(
        f"SELECT {', '.join(cols)} FROM read_csv('{nppes.as_posix()}', all_varchar=true, header=true) "
        'WHERE "NPI" IN (SELECT npi FROM t)').df()
    rows = []
    for r in df.itertuples(index=False):
        r = list(r)
        codes = [(r[2 + 2 * i], r[3 + 2 * i]) for i in range(15) if r[2 + 2 * i]]
        codes.sort(key=lambda c: c[1] != "Y")  # stable: primary first
        rows.append({"npi": r[0], "entity_type": r[1], "taxonomies": [c[0] for c in codes]})
    return pd.DataFrame(rows)


def assign_specialty(tax, xwalk, nucc):
    x = dict(zip(xwalk["Taxonomy Code"], zip(xwalk["SPEC_CD"], xwalk["SPEC_DESCR"])))
    n = dict(zip(nucc["Code"], nucc["Classification"]))
    out = []
    for r in tax.itertuples(index=False):
        spec = cd = src = None
        tx = r.taxonomies
        if r.entity_type == "1":
            for t in tx:  # primary first, then the NPI's other taxonomies
                if t in x:
                    cd, spec = x[t]
                    src = "hilabs_crosswalk" if t == tx[0] else "hilabs_crosswalk_secondary_taxonomy"
                    break
        if spec is None and tx and tx[0] in n:
            spec, src = n[tx[0]], "nucc_classification"
        out.append({"npi": r.npi, "entity_type": r.entity_type,
                    "primary_taxonomy": tx[0] if tx else None,
                    "specialty_code": cd, "specialty": spec or "Unknown",
                    "specialty_source": src or "none"})
    return pd.DataFrame(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--chosen", default="output/c4/c4_chosen.parquet")
    ap.add_argument("--alternatives", default="output/c4/c4_alternatives.parquet")
    ap.add_argument("--addresses", default="output/c5/c5_addresses.csv")
    ap.add_argument("--candidates", default="output/c5/c5_candidates.parquet")
    ap.add_argument("--nppes", default=NPPES)
    ap.add_argument("--crosswalk", default="Data/Rate Benchmarking Individual Weights_Hackathon.xlsx")
    ap.add_argument("--nucc", default="Data/nucc_taxonomy_261.csv")
    ap.add_argument("--deactivated", default="Data/NPPES Deactivated NPI Report 20261012.xlsx")
    ap.add_argument("--out", default="output/c6")
    a = ap.parse_args()
    p = lambda s: ROOT / s
    out = p(a.out)
    out.mkdir(parents=True, exist_ok=True)

    chosen = pd.read_parquet(p(a.chosen))
    addr = pd.read_csv(p(a.addresses), dtype=str)
    cand = pd.read_parquet(p(a.candidates))
    assert not addr.duplicated(["tin", "npi"]).any(), "C5 must give one address per TIN-NPI"

    tax = load_taxonomy(p(a.nppes), set(chosen["npi"]))
    xwalk = pd.read_excel(p(a.crosswalk), sheet_name="Taxonomy-Spec Xwalk", dtype=str)
    procs = pd.read_excel(p(a.crosswalk), sheet_name="Spec-Proc Weights", dtype=str)
    nucc = pd.read_csv(p(a.nucc), dtype=str)
    spec = assign_specialty(tax, xwalk, nucc)

    dead = pd.read_excel(p(a.deactivated), skiprows=1, dtype=str)
    dead_npis = set(dead.iloc[:, 0].dropna())

    df = chosen.merge(addr[["tin", "npi", "address", "unit", "city", "state", "zip5", "county", "label",
                            "score", "source_list"]].rename(columns={"label": "address_label", "score": "address_score",
                                                                      "source_list": "address_sources"}),
                      on=["tin", "npi"], how="left")
    df = df.merge(spec, on="npi", how="left")
    df["on_hilabs_code"] = df["billing_code"].isin(set(procs["PROC_CD"]))
    flag = lambda mask, name: mask.map({True: name, False: ""})
    parts = [
        flag(df["conflict_unresolved"], "unresolved_rate_conflict"),
        flag(df["address"].isna(), "no_address"),
        flag(df["address_label"] == "unverified", "address_unverified"),
        flag(~df["negotiated_type"].isin(["negotiated", "fee schedule"]), "non_dollar_type"),
        flag(df["specialty"] == "Unknown", "specialty_unknown"),
        flag(~df["npi"].str.fullmatch(r"[1-9]\d{9}"), "invalid_npi"),
        flag(df["entity_type"].isna() & df["npi"].str.fullmatch(r"[1-9]\d{9}"), "npi_not_in_nppes"),
    ]
    df["quality_flags"] = pd.concat(parts, axis=1).apply(lambda r: ";".join(x for x in r if x), axis=1)

    deactivated = df["npi"].isin(dead_npis)
    dropped = df[deactivated]
    df = df[~deactivated]

    final = df.rename(columns={
        "tin": "TIN", "npi": "NPI", "business_name": "provider_name", "negotiated_rate": "rate",
    })
    final["Address"] = (final["address"].fillna("") + (", " + final["unit"]).where(final["unit"].notna(), "")
                        + ", " + final["city"].fillna("") + ", " + final["state"].fillna("") + " " + final["zip5"].fillna("")
                        ).str.strip(", ")
    final.loc[final["address"].isna(), "Address"] = None
    cols = ["NPI", "TIN", "provider_name", "entity_type", "Address", "address_label", "address_score",
            "billing_code_type", "billing_code", "additional_information", "billing_class", "setting",
            "service_code", "billing_code_modifier", "negotiated_type", "rate", "specialty", "specialty_code",
            "specialty_source", "primary_taxonomy", "evidence", "resolved_by", "copy_status", "provider_group_id",
            "on_hilabs_code", "quality_flags"]
    final = final[cols].rename(columns={"billing_code": "Billing_Code", "specialty": "Specialty",
                                        "address_label": "Address_Label"})
    final = final.sort_values(["TIN", "NPI", "billing_code_type", "Billing_Code", "additional_information"],
                              na_position="first")
    final.to_parquet(out / "c6_provider_rates.parquet", index=False)
    final.to_csv(out / "c6_provider_rates.csv", index=False)

    # Excel view: HiLabs benchmark codes only (fits Excel's 1,048,576-row limit; the full file does not need to).
    view = final[final["on_hilabs_code"]]
    assert len(view) < 1_000_000
    with pd.ExcelWriter(out / "c6_provider_rates_excel_view.xlsx") as w:
        view.to_excel(w, sheet_name="rates_hilabs_codes", index=False)

    # Side tables: alternatives never multiply the main rows.
    alt = pd.read_parquet(p(a.alternatives))
    alt = alt[~alt["npi"].isin(dead_npis)]
    alt.to_parquet(out / "c6_rate_alternatives.parquet", index=False)
    cand = cand[(cand["rank"] > 1) & ~cand["npi"].isin(dead_npis)]
    cand.to_parquet(out / "c6_address_alternatives.parquet", index=False)
    dropped.to_csv(out / "c6_dropped_deactivated.csv", index=False)

    keys = ["TIN", "NPI", "billing_code_type", "Billing_Code", "additional_information"]
    pairs = final[["TIN", "NPI"]].drop_duplicates()
    summary = {
        "rows": len(final), "rows_input_chosen": len(chosen),
        "rows_dropped_deactivated_npi": int(deactivated.sum()),
        "deactivated_npis": sorted(set(dropped["npi"])),
        "tin_npi_pairs": len(pairs), "npis": final["NPI"].nunique(), "tins": final["TIN"].nunique(),
        "billing_codes": final["Billing_Code"].nunique(),
        "rows_on_hilabs_codes": len(view),
        "rows_without_address": int(final["Address"].isna().sum()),
        "pairs_by_address_label": addr.merge(pairs.rename(columns={"TIN": "tin", "NPI": "npi"}))["label"].value_counts().to_dict(),
        "specialty_source_by_npi": spec[spec["npi"].isin(final["NPI"])]["specialty_source"].value_counts().to_dict(),
        "npis_specialty_unknown": int((spec[spec["npi"].isin(final["NPI"])]["specialty"] == "Unknown").sum()),
        "entity_type_by_npi": spec[spec["npi"].isin(final["NPI"])]["entity_type"].value_counts().to_dict(),
        "rows_unresolved_rate_conflict": int(final["quality_flags"].str.contains("unresolved_rate_conflict").sum()),
        "duplicate_keys": int(final.duplicated(keys).sum()),
        "alternative_rate_rows": len(alt), "alternative_address_rows": len(cand),
    }
    (out / "c6_summary.json").write_text(json.dumps(summary, indent=2, default=int))
    print(json.dumps(summary, indent=2, default=int))


if __name__ == "__main__":
    main()
