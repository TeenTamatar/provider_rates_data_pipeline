"""C7: peer-relative cost metrics per provider.

1. Regions: density clustering (HDBSCAN, haversine) of the geocoded California practice addresses from
   c7_geocode.py; leftover points join the nearest region within MAX_KM, otherwise "CA-remote";
   addresses outside California are "out_of_state". Region names come from the commonest city.
2. Peer specialty: reference/peer_specialty.csv maps each C6 specialty label to a peer specialty and a
   coarser peer family.
3. Peers of a price = prices for the same service from other TINs. The search widens until it holds at
   least --min-npi providers from --min-tin TINs: service level S0 (exact key) first, inside it the same
   peer specialty within 25/50/100/200 km, then statewide, then the same family, then everyone; only then
   the next service level (ignore descriptive modifiers, drop place of service, then entity type, then
   sub-code); last, code families (CPT sub-sections, HCPCS prefixes, revenue-code groups) compared on
   rate as a share of Medicare. Peers exclude the provider's own TIN. Weaker peers get a lower weight in the provider index.
4. Per price: rate / peer median, above P75, below P25, rate / Medicare (professional codes only).
5. Per NPI and per TIN: weighted geometric mean of the ratios (cost index), shrunk toward 1 when few
   codes are compared, plus a label.

Input: output/c6/c6_provider_rates.parquet, output/c7/c7_address_geo.parquet. Output: output/c7/.
"""
import argparse, json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.cluster import HDBSCAN
from sklearn.metrics.pairwise import haversine_distances

SK = ["billing_code_type", "Billing_Code", "additional_information", "billing_class", "setting",
      "service_code", "billing_code_modifier"]
# Modifiers that change the price; the others (telehealth 95, reference lab 90, CLIA waived QW, ...) only describe.
PRICE_MODIFIERS = {"22", "26", "50", "51", "52", "53", "54", "55", "56", "62", "66", "80", "81", "82",
                   "AS", "TC", "RR", "NU", "UE", "NR"}
BASE = [c for c in SK if c not in ("setting", "service_code")]
SWAP = lambda cols: [c if c != "billing_code_modifier" else "mod_price" for c in cols]
# Service levels, strictest first: (name, match columns, entity type must match, basis).
# basis "rate": peers' dollar rates for the same code. basis "medicare": peers' rate as a share of Medicare,
# which is comparable across codes inside a code family; only for prices that have a Medicare fee.
SERVICE_LEVELS = [("S0_exact", SK, True, "rate"),
                  ("S1_info_modifier_ignored", SWAP(SK), True, "rate"),
                  ("S2_any_place", SWAP(BASE), True, "rate"),
                  ("S3_any_entity", SWAP(BASE), False, "rate"),
                  ("S4_any_subcode", ["billing_code_type", "Billing_Code", "billing_class", "mod_price"], False, "rate"),
                  ("G1_code_family", ["billing_class", "code_family", "mod_price"], False, "medicare"),
                  ("G2_code_section", ["billing_class", "code_section"], False, "medicare")]
SERVICE_WEIGHT = {"S0_exact": 1.0, "S1_info_modifier_ignored": 1.0, "S2_any_place": 1.0, "S3_any_entity": 0.75,
                  "S4_any_subcode": 0.5, "G1_code_family": 0.5, "G2_code_section": 0.35}
# Provider scopes inside one service level, narrowest first: (similarity, radius km or None = statewide).
SCOPES = [("specialty", 25), ("specialty", 50), ("specialty", 100), ("specialty", 200), ("specialty", None),
          ("family", 100), ("family", None), ("all", 100), ("all", None)]
SCOPE_WEIGHT = {"specialty": 1.0, "family": 0.75, "all": 0.5}
REGIONAL_WEIGHT = 0.2  # prices with no peers get their region's average ratio at this weight

# CPT sub-sections used as code families: (first code, last code, family, section)
CPT_FAMILIES = [(99202, 99215, "em_office", "em"), (99217, 99239, "em_hospital", "em"),
                (99281, 99292, "em_emergency_critical", "em"), (99304, 99350, "em_facility_home", "em"),
                (99381, 99429, "em_preventive", "em"), (99430, 99499, "em_other", "em"),
                (90281, 90749, "immunization", "medicine"), (90785, 90899, "psychiatry", "medicine"),
                (90901, 90999, "dialysis_biofeedback", "medicine"), (91000, 91299, "gastro_tests", "medicine"),
                (92002, 92499, "ophthalmology_services", "medicine"), (92502, 92700, "ent_services", "medicine"),
                (92920, 93799, "cardiology_services", "medicine"), (94002, 94799, "pulmonary_services", "medicine"),
                (95004, 95199, "allergy_services", "medicine"), (95700, 96020, "neuro_testing", "medicine"),
                (96105, 96171, "behavioral_testing", "medicine"), (96360, 96549, "infusion_chemo", "medicine"),
                (97010, 97799, "physical_medicine", "medicine"), (98925, 98943, "manipulation", "medicine"),
                (99000, 99199, "medicine_other", "medicine"),
                (70010, 70559, "imaging_head_neck", "radiology"), (71045, 71555, "imaging_chest", "radiology"),
                (72020, 72295, "imaging_spine_pelvis", "radiology"), (73000, 73725, "imaging_extremities", "radiology"),
                (74000, 74775, "imaging_abdomen", "radiology"), (75557, 75989, "imaging_vascular", "radiology"),
                (76000, 76999, "ultrasound_other", "radiology"), (77001, 77799, "radiation_oncology_guidance", "radiology"),
                (78000, 79999, "nuclear_medicine", "radiology"),
                (80047, 80999, "lab_drug_chemistry", "pathology_lab"), (81000, 81099, "lab_urinalysis", "pathology_lab"),
                (81105, 81599, "lab_genetics", "pathology_lab"), (82000, 84999, "lab_chemistry", "pathology_lab"),
                (85002, 85999, "lab_hematology", "pathology_lab"), (86000, 86999, "lab_immunology", "pathology_lab"),
                (87000, 87999, "lab_microbiology", "pathology_lab"), (88000, 88399, "pathology_cytology", "pathology_lab"),
                (89050, 89398, "lab_other", "pathology_lab"),
                (10004, 19499, "surgery_skin_breast", "surgery"), (20100, 29999, "surgery_musculoskeletal", "surgery"),
                (30000, 32999, "surgery_respiratory", "surgery"), (33010, 37799, "surgery_cardiovascular", "surgery"),
                (38100, 39599, "surgery_hemic_chest", "surgery"), (40490, 49999, "surgery_digestive", "surgery"),
                (50010, 59899, "surgery_urinary_genital_ob", "surgery"), (60000, 64999, "surgery_endocrine_nervous", "surgery"),
                (65091, 68999, "surgery_eye", "surgery"), (69000, 69979, "surgery_ear", "surgery")]
MAX_KM = 75
RATIO_CLIP = (0.1, 10.0)


def build_regions(geo, min_cluster, min_samples):
    """Return a frame of unique locations with region id and name."""
    ca = geo[(geo.state == "CA") & geo.lat.notna()].copy()
    X = np.radians(ca[["lat", "lon"]].values)
    ca["cl"] = HDBSCAN(min_cluster_size=min_cluster, min_samples=min_samples, metric="haversine",
                       cluster_selection_method="leaf").fit(X).labels_
    cen = ca[ca.cl >= 0].groupby("cl")[["lat", "lon"]].mean()
    noise = ca.cl < 0
    D = haversine_distances(np.radians(ca.loc[noise, ["lat", "lon"]].values),
                            np.radians(cen.values)) * 6371
    near = cen.index.values[D.argmin(1)]
    ca.loc[noise, "cl"] = np.where(D.min(1) <= MAX_KM, near, -1)
    name = ca[ca.cl >= 0].groupby("cl").city.agg(lambda s: s.mode().iat[0].title())
    ca["region"] = ca.cl.map(lambda c: f"R{int(c):02d}" if c >= 0 else "CA-remote")
    ca["region_name"] = ca.cl.map(lambda c: name.get(c, "CA remote"))
    out = geo[["address_line", "city", "state", "zip5"]].merge(
        ca[["address_line", "city", "state", "zip5", "region", "region_name"]], how="left",
        on=["address_line", "city", "state", "zip5"])
    return out


def code_groups(code_type, code):
    """Code family and section. CPT: AMA sub-sections above; HCPCS: two letters/digits and the letter;
    revenue codes: first 3 and first 2 digits."""
    c = str(code)
    if code_type == "RC":
        return f"RC{c[:3]}", f"RC{c[:2]}"
    if c.isdigit() and len(c) == 5:
        n = int(c)
        for lo, hi, fam, sec in CPT_FAMILIES:
            if lo <= n <= hi:
                return fam, sec
        return "cpt_other", "cpt_other"
    if c and c[0].isalpha():
        return c[:2], c[0]
    return "other", "other"


def _km(lat, lon):
    la, lo = np.radians(lat), np.radians(lon)
    dl = la[:, None] - la[None, :]
    dn = lo[:, None] - lo[None, :]
    h = np.sin(dl / 2) ** 2 + np.cos(la)[:, None] * np.cos(la)[None, :] * np.sin(dn / 2) ** 2
    return 2 * 6371 * np.arcsin(np.sqrt(h))


def assign_peers(df, min_npi, min_tin):
    """For every price find peers (other TINs, same service) by widening the search until it holds at
    least min_npi providers from at least min_tin TINs. Peers come from all prices, targets are the
    prices still without peers. Returns df with peer columns."""
    df = df.copy()
    for c in SK:
        df[c] = df[c].fillna("")
    df["mod_price"] = np.where(df.billing_code_modifier.isin(PRICE_MODIFIERS), df.billing_code_modifier, "")
    pairs = list(zip(df.billing_code_type, df.Billing_Code))
    cg = {k: code_groups(*k) for k in set(pairs)}
    df["code_family"] = [cg[k][0] for k in pairs]
    df["code_section"] = [cg[k][1] for k in pairs]
    n = len(df)
    cols = ["peer_service_level", "peer_scope", "peer_radius_km", "peer_n_npi", "peer_n_tin",
            "peer_median", "peer_p25", "peer_p75", "peer_basis", "row_value"]
    for c in cols:
        df[c] = np.full(n, None, dtype=object) if c in ("peer_service_level", "peer_scope", "peer_basis") else np.full(n, np.nan)
    done = np.zeros(n, dtype=bool)
    lat, lon = df.lat.values.astype(float), df.lon.values.astype(float)
    tin, npi, rate = df.TIN.values, df.NPI.values, df.rate.values.astype(float)
    pct = df.pct_medicare.values.astype(float)
    spec, fam, ent = df.peer_specialty.values, df.peer_family.values, df.entity_type.values
    res = {c: df[c].values.copy() for c in cols}
    for lname, kcols, need_entity, basis in SERVICE_LEVELS:
        keys = kcols + (["entity_type"] if need_entity else [])
        val = rate if basis == "rate" else pct
        pool = df if basis == "rate" else df[df.pct_medicare.notna()]
        for _, ix in pool.groupby(keys, dropna=False).indices.items():
            idx = ix if basis == "rate" else pool.index.values[ix]
            tgt = idx[~done[idx]]
            if len(tgt) == 0 or len(idx) <= min_npi:
                continue
            D = _km(lat[idx], lon[idx])
            D = np.where(np.isnan(D), np.inf, D)
            pos = {g: k for k, g in enumerate(idx)}
            for i in tgt:
                r = pos[i]
                other = tin[idx] != tin[i]
                for sc, rad in SCOPES:
                    m = other.copy()
                    if sc == "specialty":
                        m &= spec[idx] == spec[i]
                    elif sc == "family":
                        m &= fam[idx] == fam[i]
                    if rad is not None:
                        m &= D[r] <= rad
                    if m.sum() < min_npi:
                        continue
                    sel = idx[m]
                    if len(np.unique(npi[sel])) < min_npi or len(np.unique(tin[sel])) < min_tin:
                        continue
                    v = val[sel]
                    res["peer_basis"][i] = "same_code_rate" if basis == "rate" else "pct_of_medicare"
                    res["row_value"][i] = val[i]
                    res["peer_service_level"][i] = lname
                    res["peer_scope"][i] = f"{sc}_{'state' if rad is None else str(rad) + 'km'}"
                    res["peer_radius_km"][i] = np.inf if rad is None else rad
                    res["peer_n_npi"][i] = len(np.unique(npi[sel]))
                    res["peer_n_tin"][i] = len(np.unique(tin[sel]))
                    res["peer_median"][i] = np.median(v)
                    res["peer_p25"][i], res["peer_p75"][i] = np.quantile(v, [.25, .75])
                    done[i] = True
                    break
        print(f"{lname}: {int(done.sum())} of {n} prices have peers")
    for c in cols:
        df[c] = res[c]
    df["peer_level"] = np.where(done, df.peer_service_level + ":" + df.peer_scope, "none")
    return df


def weights(rates, hilabs_codes, spec_proc):
    """Row weight: the specialty-procedure weight for the provider's HiLabs specialty, else the HiLabs
    code weight, else the floor. Scaled by the median positive weight and clipped to [0.1, 5]."""
    w = rates[["specialty_code", "Billing_Code"]].merge(
        spec_proc.rename(columns={"SPEC_CD": "specialty_code", "PROC_CD": "Billing_Code",
                                  "SPEC_PROC_WEIGHT": "w_sp"})[["specialty_code", "Billing_Code", "w_sp"]],
        how="left", on=["specialty_code", "Billing_Code"])
    cw = hilabs_codes.set_index("code").weight
    raw = w.w_sp.values.astype(float)
    cwv = rates.Billing_Code.map(cw).astype(float).values
    raw = np.where(np.isnan(raw), cwv, raw)
    med = np.nanmedian(raw[raw > 0])
    out = np.clip(np.nan_to_num(raw, nan=0.0) / med, 0.1, 5.0)
    return out


def medicare(rows, pfs):
    p = pfs[pfs.state == "CA"].copy()
    p["modifier"] = p.modifier.fillna("")
    p = p.drop_duplicates(["code", "modifier"])
    rows = rows.assign(billing_code_modifier=rows.billing_code_modifier.fillna(""))
    m = rows[["Billing_Code", "billing_code_modifier", "service_code", "billing_code_type", "billing_class"]].merge(
        p.rename(columns={"code": "Billing_Code", "modifier": "billing_code_modifier"}),
        how="left", on=["Billing_Code", "billing_code_modifier"])
    office = (m.service_code.astype(str) == "11").values
    ref = np.where(office, m.nonfacility, m.facility).astype(float)
    ok = (rows.billing_class == "professional").values & rows.billing_code_type.isin(["CPT", "HCPCS"]).values
    ref = np.where(ok & (ref > 0), ref, np.nan)
    return ref


def aggregate(rows, key, min_codes, shrink_k, lo, hi):
    r = rows[rows.ratio.notna()].copy()
    r["lw"] = r.w * np.log(r.ratio)
    r["real"] = (~r.imputed).astype(int)
    g = r.groupby(key).agg(n_codes=("real", "sum"), n_regional_average=("imputed", "sum"),
                           w_sum=("w", "sum"), lw_sum=("lw", "sum"),
                           share_above_p75=("above_p75", "mean"), share_below_p25=("below_p25", "mean"),
                           median_ratio=("ratio", "median"), median_pct_medicare=("pct_medicare", "median"),
                           codes_with_medicare=("pct_medicare", "count"))
    g["cost_index_raw"] = np.exp(g.lw_sum / g.w_sum)
    g["cost_index"] = np.exp(np.log(g.cost_index_raw) * g.n_codes / (g.n_codes + shrink_k))
    g["label"] = np.select([(g.n_codes == 0) & (g.n_regional_average > 0), g.n_codes < min_codes,
                            g.cost_index <= lo, g.cost_index >= hi],
                           ["regional_average_only", "insufficient_peers", "lower_cost", "higher_cost"], "typical")
    return g.drop(columns=["w_sum", "lw_sum"]).reset_index()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rates", default="output/c6/c6_provider_rates.parquet")
    ap.add_argument("--geo", default="output/c7/c7_address_geo.parquet")
    ap.add_argument("--geocode", default="output/c7/c7_geocode.parquet")
    ap.add_argument("--specialty-map", default="reference/peer_specialty.csv")
    ap.add_argument("--hilabs-xlsx", default="Data/Rate Benchmarking Individual Weights_Hackathon.xlsx")
    ap.add_argument("--codes", default="output/c3/ref/hilabs_codes.parquet")
    ap.add_argument("--pfs", default="output/c3/ref/medicare_pfs.parquet")
    ap.add_argument("--out", default="output/c7")
    ap.add_argument("--min-tin", type=int, default=3, help="minimum distinct other TINs among the peers")
    ap.add_argument("--min-npi", type=int, default=5, help="minimum distinct other providers (NPIs) among the peers")
    ap.add_argument("--min-codes", type=int, default=3, help="minimum compared prices for a label")
    ap.add_argument("--shrink-k", type=float, default=5.0)
    ap.add_argument("--lower", type=float, default=0.85)
    ap.add_argument("--higher", type=float, default=1.15)
    ap.add_argument("--region-min-cluster", type=int, default=40)
    ap.add_argument("--region-min-samples", type=int, default=10)
    ap.add_argument("--exclude-flag", default="", help="drop prices carrying this quality flag, e.g. unresolved_rate_conflict")
    a = ap.parse_args()
    out = Path(a.out)

    rates = pd.read_parquet(a.rates)
    if a.exclude_flag:
        rates = rates[~rates.quality_flags.fillna("").str.contains(a.exclude_flag)]
    rates = rates[rates.rate > 0].reset_index(drop=True)

    # --- regions and peer specialty (providers) ---
    gc = pd.read_parquet(a.geocode)
    reg = build_regions(gc, a.region_min_cluster, a.region_min_samples)
    geo = pd.read_parquet(a.geo)
    state = gc.drop_duplicates(["address_line", "city", "zip5"])[["address_line", "city", "zip5", "state"]]
    reg = reg.drop_duplicates(["address_line", "city", "zip5"])[["address_line", "city", "zip5", "region", "region_name"]]
    geo = geo.merge(state, how="left", on=["address_line", "city", "zip5"]).merge(
        reg, how="left", on=["address_line", "city", "zip5"])
    geo["region"] = np.where(geo.address_line.isna() | geo.lat.isna(), "unknown",
                             np.where(geo.state.fillna("CA") != "CA", "out_of_state", geo.region.fillna("CA-remote")))
    geo["region_name"] = geo.region_name.fillna(geo.region)
    rates = rates.merge(geo[["NPI", "TIN", "region", "region_name", "lat", "lon", "county_fips", "method"]]
                        .rename(columns={"method": "geo_method"}), how="left", on=["NPI", "TIN"])
    rates["region"] = rates.region.fillna("unknown")

    pm = pd.read_csv(a.specialty_map)
    rates = rates.merge(pm.rename(columns={"specialty": "Specialty"}), how="left", on="Specialty")
    rates["peer_specialty"] = rates.peer_specialty.fillna("unknown")
    rates["peer_family"] = rates.peer_family.fillna("unknown")

    # --- peers, ratios, weights ---
    rates["medicare_rate"] = medicare(rates, pd.read_parquet(a.pfs))
    rates["pct_medicare"] = rates.rate / rates.medicare_rate
    rows = assign_peers(rates, a.min_npi, a.min_tin)
    rows["ratio"] = (rows.row_value / rows.peer_median).clip(*RATIO_CLIP)
    rows["above_p75"] = np.where(rows.ratio.notna(), (rows.row_value > rows.peer_p75).astype(float), np.nan)
    rows["below_p25"] = np.where(rows.ratio.notna(), (rows.row_value < rows.peer_p25).astype(float), np.nan)
    sp = pd.read_excel(a.hilabs_xlsx, "Spec-Proc Weights", dtype={"SPEC_CD": str, "PROC_CD": str})
    rows["specialty_code"] = rows.specialty_code.astype("string")
    rows["w"] = weights(rows, pd.read_parquet(a.codes), sp) * (rows.peer_scope.str.split("_").str[0].map(SCOPE_WEIGHT) * rows.peer_service_level.map(SERVICE_WEIGHT)).fillna(1.0)
    # Last resort: a price with no peers takes the average ratio of compared prices in its region (same peer
    # family and billing class, then region, then the whole state). It carries no information about this
    # provider; it only keeps the price in the index at low weight and is flagged as imputed.
    rows["imputed"] = False
    real = rows.ratio.notna().values
    lg = rows.assign(lw=np.log(rows.ratio) * rows.w)[real]
    for keys in (["region", "peer_family", "billing_class"], ["region"], []):
        if keys:
            avg = lg.groupby(keys).apply(lambda d: np.exp(d.lw.sum() / d.w.sum()), include_groups=False)
            cand = rows[keys].merge(avg.rename("reg_ratio").reset_index(), how="left", on=keys).reg_ratio.values
        else:
            cand = np.full(len(rows), np.exp(lg.lw.sum() / lg.w.sum()))
        need = ~rows.ratio.notna().values & ~np.isnan(cand)
        rows.loc[need, "ratio"] = cand[need]
        rows.loc[need, "imputed"] = True
        rows.loc[need, "peer_level"] = "regional_average:" + ("+".join(keys) if keys else "state")
        rows.loc[need, "peer_basis"] = "regional_average"
        rows.loc[need, "peer_scope"] = "regional_average"
        rows.loc[need, "peer_service_level"] = "R_regional_average"
    base_w = weights(rows, pd.read_parquet(a.codes), sp)
    rows.loc[rows.imputed, "w"] = base_w[rows.imputed.values] * REGIONAL_WEIGHT
    rows.to_parquet(out / "c7_price_ratios.parquet", index=False)

    # --- provider metrics ---
    desc = ["NPI", "TIN", "provider_name", "entity_type", "peer_specialty", "peer_family", "region", "region_name"]
    npi = aggregate(rows, ["NPI", "TIN"], a.min_codes, a.shrink_k, a.lower, a.higher)
    attrs = rows.sort_values("rate", ascending=False).drop_duplicates(["NPI", "TIN"])[desc]
    npi = attrs.merge(npi, how="right", on=["NPI", "TIN"])
    lvl = rows.groupby(["NPI", "TIN"]).peer_level.agg(lambda s: s.mode().iat[0]).rename("main_peer_level").reset_index()
    npi = npi.merge(lvl, how="left", on=["NPI", "TIN"])
    npi.to_parquet(out / "c7_npi_cost.parquet", index=False)
    npi.to_csv(out / "c7_npi_cost.csv", index=False)
    tin = aggregate(rows, ["TIN"], a.min_codes, a.shrink_k, a.lower, a.higher)
    names = rows.groupby("TIN").provider_name.agg(lambda s: s.mode().iat[0])
    tin.insert(1, "business_name", tin.TIN.map(names))
    tin["n_npis"] = tin.TIN.map(rows.groupby("TIN").NPI.nunique())
    tin.to_parquet(out / "c7_tin_cost.parquet", index=False)
    tin.to_csv(out / "c7_tin_cost.csv", index=False)

    # --- validation ---
    v = {}
    v["rows"] = len(rows)
    v["rows_compared"] = int((rows.ratio.notna() & ~rows.imputed).sum())
    v["rows_regional_average"] = int(rows.imputed.sum())
    v["rows_by_service_level"] = rows.peer_service_level.fillna("none").value_counts().to_dict()
    v["rows_by_scope"] = rows.peer_scope.fillna("none").value_counts().to_dict()
    v["rows_by_basis"] = rows.peer_basis.fillna("none").value_counts().to_dict()
    v["median_ratio_by_scope"] = rows.groupby("peer_scope").ratio.median().round(4).to_dict()
    v["npis"] = len(npi)
    v["npi_labels"] = npi.label.value_counts().to_dict()
    v["tin_labels"] = tin.label.value_counts().to_dict()
    rng = np.random.default_rng(0)
    r = rows[rows.ratio.notna()].copy()
    r["half"] = rng.integers(0, 2, len(r))
    h = r.groupby(["NPI", "TIN", "half"]).apply(
        lambda d: np.exp((d.w * np.log(d.ratio)).sum() / d.w.sum()), include_groups=False).unstack()
    h = h.dropna()
    v["split_half_pairs"] = len(h)
    v["split_half_spearman"] = round(float(h[0].corr(h[1], method="spearman")), 4) if len(h) > 2 else None
    pro = npi[npi.codes_with_medicare >= 3]
    v["npi_with_medicare_pct"] = len(pro)
    v["spearman_index_vs_pct_medicare"] = (round(float(pro.cost_index.corr(pro.median_pct_medicare, method="spearman")), 4)
                                           if len(pro) > 2 else None)
    v["region_sizes_npi"] = npi.groupby("region_name").NPI.nunique().sort_values(ascending=False).head(25).to_dict()
    v["geo_method_rows"] = rows.geo_method.value_counts(dropna=False).to_dict()
    v["params"] = {k: getattr(a, k.replace("-", "_")) for k in
                   ["min_tin", "min_npi", "min_codes", "shrink_k", "lower", "higher", "region_min_cluster", "region_min_samples"]}
    (out / "c7_summary.json").write_text(json.dumps(v, indent=2, default=str))
    print(json.dumps(v, indent=2, default=str))


if __name__ == "__main__":
    main()
