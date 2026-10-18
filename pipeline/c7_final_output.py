"""C7 final file: one row per price with TIN, NPI, specialty, address, billing code and sub-code, rate and the
cost-effectiveness score of the provider (cost index below 1 = cheaper than peers) with its label.
The key is TIN + NPI + code type + code + sub-code: revenue codes are priced per HCPCS/CPT sub-code
(`additional_information`), so the code alone is not unique.

Input: output/c7/c7_price_ratios.parquet, output/c7/c7_npi_cost.parquet. Output: output/c7/c7_final.csv / .xlsx.
"""
import argparse
from pathlib import Path

import pandas as pd


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dir", default="output/c7")
    a = ap.parse_args()
    d = Path(a.dir)
    p = pd.read_parquet(d / "c7_price_ratios.parquet")
    n = pd.read_parquet(d / "c7_npi_cost.parquet")[["NPI", "TIN", "cost_index", "label", "n_codes"]]
    f = p.merge(n, how="left", on=["NPI", "TIN"])
    out = pd.DataFrame({
        "TIN": f.TIN, "NPI": f.NPI, "Specialty": f.Specialty, "Address": f.Address,
        "Billing_Code_Type": f.billing_code_type, "Billing_Code": f.Billing_Code,
        "Sub_Code": f.additional_information.fillna(""), "Rate": f.rate,
        "Price_vs_Peers": f.ratio.round(3),
        "Cost_Effectiveness_Score": f.cost_index.round(3),
        "Cost_Effectiveness_Label": f.label,
        "Prices_Compared": f.n_codes})
    key = ["TIN", "NPI", "Billing_Code_Type", "Billing_Code", "Sub_Code"]
    dup = int(out.duplicated(key).sum())
    if dup:
        raise SystemExit(f"{dup} rows repeat the key {key}")
    out = out.sort_values(key).reset_index(drop=True)
    out.to_csv(d / "c7_final.csv", index=False)
    out.to_excel(d / "c7_final.xlsx", index=False)
    print(len(out), "rows;", out.Cost_Effectiveness_Label.value_counts(dropna=False).to_dict())


if __name__ == "__main__":
    main()
