"""C7 step 1: lat/lon and county for every practice address in the C6 rate file.

Reads the C5 chosen addresses (address_line, city, state, zip5 per TIN-NPI) and sends each unique street
address to the Census batch geocoder, which returns coordinates and county FIPS in one call. Addresses
the Census cannot match get the mean coordinates of matched addresses in the same ZIP5, then ZIP3
(method recorded per row). Results are cached in c7_geocode.parquet, so a re-run only sends new addresses.

Output: output/c7/c7_geocode.parquet (one row per unique address), c7_address_geo.parquet (one row per
TIN-NPI of the C6 file), c7_geocode_summary.json.
"""
import argparse, io, json, time
from pathlib import Path

import pandas as pd
import requests

URL = "https://geocoding.geo.census.gov/geocoder/geographies/addressbatch"


def batch(rows, benchmark, vintage, chunk, pause):
    out = []
    for i in range(0, len(rows), chunk):
        part = rows.iloc[i:i + chunk]
        buf = io.StringIO()
        part[["key", "address_line", "city", "state", "zip5"]].to_csv(buf, header=False, index=False)
        for attempt in range(4):
            try:
                r = requests.post(URL, data={"benchmark": benchmark, "vintage": vintage},
                                  files={"addressFile": ("a.csv", buf.getvalue())}, timeout=300)
                r.raise_for_status()
                break
            except Exception as e:
                print(f"chunk {i} attempt {attempt}: {e}")
                time.sleep(10 * (attempt + 1))
        else:
            continue
        cols = ["key", "input", "match", "match_type", "match_address", "coords", "tiger_id", "side",
                "state_fips", "county_fips", "tract", "block"]
        res = pd.read_csv(io.StringIO(r.text), header=None, names=cols, dtype=str, on_bad_lines="skip")
        out.append(res)
        print(f"chunk {i}-{i + len(part)}: {(res.match == 'Match').sum()} matched")
        time.sleep(pause)
    return pd.concat(out) if out else pd.DataFrame()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rates", default="output/c6/c6_provider_rates.parquet")
    ap.add_argument("--addresses", default="output/c5/c5_addresses.csv")
    ap.add_argument("--out", default="output/c7")
    ap.add_argument("--benchmark", default="Public_AR_Current")
    ap.add_argument("--vintage", default="Current_Current")
    ap.add_argument("--chunk", type=int, default=1000)
    ap.add_argument("--pause", type=float, default=2.0)
    ap.add_argument("--offline", action="store_true", help="skip the Census call; use cached results and fallbacks only")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    ca = pd.read_csv(a.addresses, dtype=str)
    ca["zip5"] = ca.zip5.str.zfill(5)
    g = ca.dropna(subset=["address_line"]).drop_duplicates(["address_line", "city", "state", "zip5"])
    g = g[["address_line", "city", "state", "zip5"]].reset_index(drop=True)
    g["key"] = range(len(g))
    g["lat"] = g["lon"] = float("nan")
    g["county_fips"] = None
    g["method"] = None
    prev = out / "c7_geocode.parquet"
    if prev.exists():
        p = pd.read_parquet(prev)
        p = p[p.method == "census_batch"].drop(columns=["method"])
        m = g[["key", "address_line", "city", "state", "zip5"]].merge(p, on=["address_line", "city", "state", "zip5"])
        g.loc[m.key, ["lat", "lon", "county_fips"]] = m[["lat", "lon", "county_fips"]].values
        g.loc[m.key, "method"] = "census_batch"
    todo = g[g.method.isna()]
    print(f"{len(g)} unique addresses, {len(g) - len(todo)} cached, {len(todo)} to send")
    res = batch(todo, a.benchmark, a.vintage, a.chunk, a.pause) if len(todo) and not a.offline else pd.DataFrame()
    if len(res):
        res = res[res.match == "Match"].copy()
    if len(res):
        xy = res.coords.str.split(",", expand=True)
        res["key"] = res.key.astype(int)
        res["lon"] = pd.to_numeric(xy[0], errors="coerce")
        res["lat"] = pd.to_numeric(xy[1], errors="coerce")
        res["county_fips"] = res.state_fips.str.zfill(2) + res.county_fips.str.zfill(3)
        res = res.dropna(subset=["lat"]).set_index("key")
        g = g.set_index("key")
        g.loc[res.index, ["lat", "lon", "county_fips"]] = res[["lat", "lon", "county_fips"]].values
        g.loc[res.index, "method"] = "census_batch"
        g = g.reset_index()
    g["zip5"] = g.zip5.astype("string").str.zfill(5)
    ok = g.lat.notna()
    for k, name in ((5, "zip5_mean"), (3, "zip3_mean")):
        z = g.zip5.str[:k]
        cen = g[ok].groupby(z[ok])[["lat", "lon"]].mean()
        need = ~g.lat.notna() & z.isin(cen.index)
        g.loc[need, "lat"] = z[need].map(cen.lat).values
        g.loc[need, "lon"] = z[need].map(cen.lon).values
        g.loc[need, "method"] = name
    g["method"] = g.method.fillna("none")
    g.drop(columns=["key"]).to_parquet(out / "c7_geocode.parquet", index=False)

    r = pd.read_parquet(a.rates, columns=["NPI", "TIN"]).drop_duplicates()
    pairs = ca.drop(columns=["lat", "lon", "county_fips"], errors="ignore").merge(g.drop(columns=["key"]), on=["address_line", "city", "state", "zip5"], how="left")
    r = r.merge(pairs[["npi", "tin", "address_line", "city", "zip5", "lat", "lon", "county_fips", "method"]]
                .rename(columns={"npi": "NPI", "tin": "TIN"}), on=["NPI", "TIN"], how="left")
    r.to_parquet(out / "c7_address_geo.parquet", index=False)
    summ = {"unique_addresses": len(g), "method": g.method.value_counts().to_dict(),
            "pairs": len(r), "pairs_with_coords": int(r.lat.notna().sum()),
            "pairs_with_county": int(r.county_fips.fillna("").ne("").sum())}
    (out / "c7_geocode_summary.json").write_text(json.dumps(summ, indent=2))
    print(json.dumps(summ, indent=2))


if __name__ == "__main__":
    main()
