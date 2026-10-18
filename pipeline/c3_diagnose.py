"""C3 diagnosis: why each dataset scored what it did, problems ranked by severity, and what to improve.

Template-based and deterministic (no model). For every dataset (index file + its rate files):

  1. Points lost. Each measured metric's exact cost to its file's score:
        group weight / weights of measured groups x (1 - metric score) / measured metrics in the group.
     It is then carried to the dataset score (index^0.3 x files^0.7) by the first-order effect:
        index metric:     0.3 x dataset / index score x points lost in the index score
        rate-file metric: 0.7 x dataset / mean file score x points lost in that file / number of files
  2. Severity by dataset points lost: High >= 2.0, Medium >= 0.5, Low > 0; problems under 0.01 points are
     listed together as minor. Floors for defects that make a plan's rates wrong or missing however few
     files they touch: an unreachable index or rate file is Critical, a state mismatch (I11) at least High.
     When the dataset score is already 0, other problems are ranked by points of the index score instead.
  3. Text. Each metric has a template (what is wrong, why it matters, fix for the publisher, fix for our
     pipeline), filled with the metric's numbers, the files it affects and examples read from the scan tables.

Inputs: output/c3/c3_{rate_file,index}_{metrics,scores}.csv, c3_dataset_scores.csv, output/scan/, Data/*_index.json.
Outputs: output/c3/c3_diagnosis.csv (one row per dataset x problem), output/c3/c3_diagnosis.md (report).

Usage:
    python pipeline/c3_diagnose.py
"""
import argparse
import collections
import csv
import glob
import json
import os
import sys

import duckdb

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from c3_index_metrics import GROUPS as INDEX_GROUPS, INDEX_SHARE, STATES, hios_kind  # noqa: E402
from c3_metrics import GROUPS as FILE_GROUPS  # noqa: E402
from rate_scan import CODE_RE, load_hcpcs, tin_valid  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
C3 = os.path.join(ROOT, "output", "c3")
SCAN = os.path.join(ROOT, "output", "scan")
HIGH, MEDIUM, MINOR = 2.0, 0.5, 0.01  # dataset points; problems below MINOR are listed on one line
# Severity floors: defects that make a plan's rates wrong or missing, however few files they touch.
FLOOR = {"M1": "Critical", "I1": "Critical", "I9": "Critical", "I11": "High"}
ORDER = ["Critical", "High", "Medium", "Low"]
DISPLAY = {"ambetter": "Ambetter", "healthnet": "Health Net", "fidelis": "Fidelis", "qualchoice": "QualChoice"}

# id: (problem, why it matters, fix for the publisher, fix for our pipeline)
T = {
    "M1": ("The rate file cannot be downloaded.",
           "Every plan that points to it has no published rates, which the rule requires; the file scores 0.",
           "Restore the file at the URL the index lists, or correct the link.",
           "Keep the file in the scores as 0 and recheck the link each run."),
    "M2": ("The rate file is older than the monthly update cycle.",
           "Old rates may no longer be paid; the rule requires monthly updates.",
           "Republish monthly and update `last_updated_on`.",
           "Flag rates from stale files in C4 and C6."),
    "M3": ("The rate file is much older than the index that links it.",
           "The index may describe plans whose rates have since changed.",
           "Regenerate rate files and the index together.",
           "Report the lag next to every rate taken from the file."),
    "M4": ("Some prices have expired or have invalid end dates.",
           "Expired rates are no longer payable.",
           "Remove expired prices or update their `expiration_date`.",
           "Drop expired prices in C4 (C2b step 4 already does)."),
    "M5": ("Some objects are missing fields the schema requires, or carry them in the wrong format.",
           "Missing fields mean prices or providers cannot be read reliably.",
           "Validate files with the CMS schema validator before publishing.",
           "Count and skip incomplete objects; never fill them in by guessing."),
    "M6": ("Some fields hold values outside the schema's allowed list.",
           "Unknown values cannot be interpreted (e.g. an unknown billing class).",
           "Use only the values the schema version allows.",
           "Treat unknown values as invalid in C4."),
    "M7": ("Fields the schema requires in certain cases are missing (service code on professional prices, business name on EIN TINs, plan sponsor on EIN plans).",
           "Without a place of service, a professional rate cannot be matched to a setting.",
           "Fill the conditional fields.",
           "Rates without a required place of service are kept but flagged in C4."),
    "M8": ("Prices refer to provider groups that do not exist in the file.",
           "Those prices cannot be tied to any provider.",
           "Publish every referenced provider group.",
           "Drop prices whose provider reference does not resolve."),
    "M9": ("Billing codes are malformed for their code type.",
           "Codes that do not match the standard code sets cannot be compared with Medicare or with other payers, and joins on the code fail.",
           "Publish codes as text in their standard form (revenue codes are 4 digits with leading zeros, e.g. `0250`; CPT anesthesia codes are 5 digits, e.g. `00104`) and label each with its correct code type.",
           "Zero-pad revenue codes to 4 digits and CPT codes to 5 digits before C4; drop codes that remain invalid."),
    "M10": ("Some NPIs fail the CMS check-digit rule.",
            "An invalid NPI cannot be linked to a provider, address or specialty.",
            "Correct the NPIs at the source.",
            "Exclude invalid NPIs from C5 and C6 and list them."),
    "M11": ("Some TINs are not valid EINs (often a prefix the IRS never issued).",
            "On individual clinicians this pattern is consistent with Social Security numbers published as EINs; the CMS schema says to report the NPI instead (F47). Not proven.",
            "Replace SSN-based TINs with the provider's NPI (`tin.type = npi`) as the schema instructs.",
            "Keep the rows but flag the TIN; do not use it to group providers in C5."),
    "M12": ("Some place-of-service codes are not on the CMS list.",
            "The setting of those rates is unknown.",
            "Use CMS place-of-service codes or `CSTM-00`.",
            "Treat invalid codes as unknown setting in C4."),
    "M13": ("Some prices are not dollar amounts (percentages of charges).",
            "Percentages cannot be compared without the provider's charges; the rule prefers dollar amounts.",
            "Publish dollar amounts wherever one can be set in advance.",
            "Keep percentage prices out of dollar comparisons in C4 and C7."),
    "M14": ("Some prices are $0 or negative.",
            "These are placeholders, not rates; the schema requires a rate above 0.",
            "Remove placeholder prices.",
            "Drop prices at or below $0 in C4."),
    "M15": ("Some professional rates are far from the file's usual ratio to Medicare.",
            "They are likely data errors or unusual contracts and distort comparisons.",
            "Review the flagged rates before publishing.",
            "Flag them in C4 and C7; use the extreme ones only with a warning."),
    "M16": ("Some rates are far from the other rates for the same service in the same file.",
            "Likely data-entry errors or unit mismatches.",
            "Review the flagged rates.",
            "Flag them in C4."),
    "M17": ("Some NPIs are deactivated or not in NPPES.",
            "Rates for providers who can no longer bill are stale.",
            "Remove deactivated providers from the file.",
            "Exclude deactivated NPIs from C5 and C6."),
    "M18": ("Facility (institutional) prices are attached to groups listing only individual clinicians.",
            "Facility fees are paid to organizations; the provider list is likely wrong.",
            "List the facility's organization NPI on institutional prices.",
            "Flag these prices in C4."),
    "M19": ("Most price rows are exact duplicates.",
            "The same price is repeated many times (e.g. one per item name), which inflates the file and any count taken from it.",
            "Publish each price once per provider group and service.",
            "Deduplicate on group x service x rate before any count (C2b step 5 already does)."),
    "M20": ("The same billing code appears in several items.",
            "A reader cannot tell which item is the real one; it is the main source of the duplicate rows.",
            "Publish one item per billing code and arrangement.",
            "Merge items by code before extracting rates."),
    "M21": ("Many high-weight HiLabs codes have no price in the file.",
            "Negotiators cannot benchmark the services that matter most.",
            "Publish rates for all covered services, as the rule requires.",
            "Report coverage gaps per specialty in C6 and C7."),
    "M23": ("Provider groups carry no network name.",
            "The file mixes networks (e.g. PPO and HMO) with nothing to tell them apart, which is why C2b had to infer the PPO rates.",
            "Fill `network_name` (from March 2027 the rule requires one file per network).",
            "Keep the C2b inference rules; use `network_name` wherever a payer fills it."),
    "M24": ("The same provider has different rates for the same service in different groups, and the file does not say which applies.",
            "A negotiator cannot tell which rate is real; we had to drop these (C2b, A9).",
            "Name a distinct network for each contract, or publish one rate per provider and service.",
            "Keep the strict rule (drop the conflicting rates) and report how many were dropped."),
    "M26": ("Bundled or capitated items do not list the services they cover.",
            "A capitation or bundle price is meaningless without its service list.",
            "Fill `bundled_codes` and `covered_services`.",
            "Exclude these items from service-level comparisons."),
    "M27": ("Some percentage prices are not whole-number percentages.",
            "A value like 1.0 may mean 100% or 1%.",
            "Write percentages as whole numbers (40.5 for 40.5%).",
            "Exclude ambiguous percentages."),
    "I1": ("The index file cannot be downloaded.", "Nothing can be found from it; the dataset scores 0.",
           "Restore the index at its URL.", "Keep the dataset at 0 until it returns."),
    "I2": ("The index is older than the monthly update cycle.", "Its plan list and links may be out of date.",
           "Republish the index monthly.", "Report the index age with the dataset score."),
    "I3": ("Some index objects are missing required fields.", "Plans or links may be unreadable.",
           "Validate with the CMS schema validator.", "Skip incomplete objects and count them."),
    "I4": ("Some plan ID types or markets are outside the allowed values.", "The plan cannot be classified.",
           "Use the allowed values.", "Treat them as unknown."),
    "I5": ("Conditional fields are missing (plan sponsor on EIN plans, or a structure with no file link).",
           "The plan's sponsor or files cannot be identified.", "Fill the fields.", "Flag the plans."),
    "I6": ("Some plan IDs do not match their declared type (e.g. HIOS IDs labelled `ein`, or a HIOS ID with an invalid state code).",
           "Mislabelled IDs break joins with CMS plan data and state detection.",
           "Label each ID with its real type and use standard HIOS IDs.",
           "Infer the real type from the ID's shape (C1 already does) and keep the mismatch flag."),
    "I7": ("Plan IDs are not at the level the rule requires: 10-character HIOS product IDs instead of 14-character plan IDs.",
           "A product ID covers many plans, so the index cannot say which specific plan a file serves; CMS lists 14-character plan IDs for many of these products.",
           "Report the 14-character HIOS plan ID (or the 5-digit issuer ID only where no plan ID exists).",
           "Map product IDs to plan IDs with the CMS Plan Attributes file where possible."),
    "I8": ("Some reporting structures link no out-of-network (allowed-amount) file.", "The rule requires one.",
           "Publish and link the allowed-amount file.", "Report the gap."),
    "I9": ("A rate file the index links cannot be downloaded.",
           "The plans that point to it have no published rates at all.",
           "Restore the file or fix the link.", "Score the file 0 (it is), and recheck every run."),
    "I10": ("An out-of-network file the index links cannot be downloaded.", "The required file is effectively missing.",
            "Restore the file or fix the link.", "Report the gap."),
    "I11": ("A linked rate file's providers practise mostly in a different state from the plans that use it.",
            "The plans are probably pointing to the wrong network file (Ambetter KS links a Kentucky network, F46); members would see the wrong providers and rates.",
            "Link each plan to its own state's network file.",
            "Do not use that file's rates for the plans' state; report the mismatch."),
    "I12": ("The same plan ID carries different types, markets or issuers.", "The plan cannot be identified reliably.",
            "Use one consistent record per plan ID.", "Keep the first record and flag the rest."),
    "I13": ("The index repeats plan rows.", "Plan counts are inflated.", "Remove duplicate rows.", "Deduplicate in C1 (already done)."),
    "I14": ("A plan is listed under several reporting structures.", "It is unclear which rate file applies.",
            "List each plan once.", "Use every linked file and flag the plan."),
    "I15": ("Some file links are not HTTPS.", "Downloads can be intercepted or altered.", "Serve files over HTTPS.", "None needed."),
}


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def read(name):
    with open(os.path.join(C3, name), encoding="utf-8") as f:
        return list(csv.DictReader(f))


def points_lost(rows, groups, gate_id):
    """Fraction of the score each metric costs, for one file or index."""
    gate = num(next(r["score"] for r in rows if r["metric_id"] == gate_id))
    if not gate:
        return {gate_id: 1.0}
    meas = [r for r in rows if r["group"] in groups and r["score"] not in ("", None)]
    n = collections.Counter(r["group"] for r in meas)
    w = sum(groups[g] for g in n)
    return {r["metric_id"]: groups[r["group"]] / w * (1 - float(r["score"])) / n[r["group"]] for r in meas
            if float(r["score"]) < 1}


# ---- deterministic evidence from the scan tables and index files
def evidence_file(metric, label, hcpcs):
    p = lambda n: os.path.join(SCAN, f"{label}_{n}.parquet")
    if not os.path.exists(p("prices")):
        return ""
    con = duckdb.connect()
    if metric == "M9":
        codes = con.execute(f"select distinct billing_code_type, billing_code from '{p('prices')}'").fetchall()
        bad = []
        for t, c in codes:
            rx = CODE_RE.get(t)
            ok = bool(c) and (bool(rx.match(c)) if rx else True)
            if ok and t == "HCPCS" and c[0].isalpha() and c[0] != "D":
                ok = c in hcpcs
            if not ok:
                bad.append(f"{t} `{c}`")
        return "e.g. " + ", ".join(sorted(bad)[:6]) if bad else ""
    if metric == "M11":
        tins = con.execute(f"select distinct tin_type, tin from '{p('members')}' limit 200000").fetchall()
        bad = [t for ty, t in tins if tin_valid(ty, t)[0] == "invalid"]
        pref = collections.Counter(t[:2] for t in bad).most_common(4)
        return f"{len(bad):,} distinct TINs; most common prefixes " + ", ".join(f"{k} ({v})" for k, v in pref) if bad else ""
    if metric == "M19":
        r = con.execute(f"""select billing_code_type, billing_code, coalesce(additional_information, ''), count(*) n
                            from '{p('prices')}' group by provider_group_id, billing_code_type, billing_code,
                                 additional_information, negotiation_arrangement, negotiated_type, billing_class,
                                 setting, service_code, billing_code_modifier, negotiated_rate
                            order by n desc, 1, 2, 3 limit 1""").fetchone()
        return f"e.g. one {r[0]} `{r[1]}`{' / ' + r[2] if r[2] else ''} price repeated {r[3]:,} times" if r else ""
    if metric == "M24":
        r = con.execute(f"""
            with mm as (select distinct provider_group_id g, tin, npi from '{p('members')}' where npi <> '0'),
                 pr as (select provider_group_id g, billing_code, coalesce(billing_code_modifier, '') m,
                               coalesce(service_code, '') sc, negotiated_rate r
                        from '{p('prices')}' where billing_code_type = 'CPT' and billing_code in ('99213', '99214'))
            select mm.npi, pr.billing_code, count(distinct pr.r) nr, min(pr.r), max(pr.r), count(distinct mm.g) ng
            from mm join pr using (g) group by all order by nr desc, ng desc, mm.npi, pr.billing_code limit 1""").fetchone()
        return (f"e.g. NPI {r[0]} has {r[2]} rates for CPT {r[1]} (${r[3]:,.2f}-${r[4]:,.2f}) across {r[5]} groups"
                if r and r[2] > 1 else "")
    return ""


def evidence_index(metric, dataset):
    paths = glob.glob(os.path.join(ROOT, "Data", f"*_{dataset}_index.json"))
    if not paths or metric not in ("I6", "I7"):
        return ""
    d = json.load(open(paths[0], encoding="utf-8"))
    ids = collections.Counter()
    for s in d.get("reporting_structure", []):
        for pl in s.get("reporting_plans", []):
            pid, typ = str(pl.get("plan_id", "")), pl.get("plan_id_type")
            kind, st = hios_kind(pid)
            if metric == "I7" and kind == "product":
                ids[pid] += 1
            if metric == "I6" and ((typ == "ein" and kind) or (typ == "hios" and st and st not in STATES)):
                ids[f"{pid} (labelled {typ})"] += 1
    return "e.g. " + ", ".join(f"`{k}`" for k, _ in ids.most_common(3)) if ids else ""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.parse_args()
    fm, fs, im, ix, ds = (read(n) for n in ("c3_rate_file_metrics.csv", "c3_rate_file_scores.csv",
                                            "c3_index_metrics.csv", "c3_index_scores.csv", "c3_dataset_scores.csv"))
    hcpcs = load_hcpcs()
    by_file = collections.defaultdict(list)
    for r in fm:
        by_file[r["rate_file"]].append(r)
    meta = {r["metric_id"]: r for r in fm + im}
    rows_out, md = [], ["# C3 diagnosis: why each dataset scored what it did", "",
                        "Generated by `pipeline/c3_diagnose.py` from the C3 outputs. Points are dataset-score points "
                        "(0-100) each problem costs; severity: Critical = a file or index cannot be downloaded, "
                        f"High >= {HIGH}, Medium >= {MEDIUM}, Low below. Definitions: `LOGIC.md` section 3C.", ""]
    for d in sorted(ds, key=lambda r: -float(r["dataset_score"])):
        name = d["dataset"]
        D, I, F = float(d["dataset_score"]), float(d["index_score"]), float(d["mean_rate_file_score"])
        files = [r for r in fs if r["dataset"] == name]
        issues = {}
        # index problems
        irows = [r for r in im if r["dataset"] == name]
        for mid, pts in points_lost(irows, INDEX_GROUPS, "I1").items():
            r = next(x for x in irows if x["metric_id"] == mid)
            eff = INDEX_SHARE * D / I * pts * 100 if I and D else None
            issues[mid] = {"scope": "index", "points": eff if D else INDEX_SHARE * pts * 100, "index_points": not D,
                           "detail": f"{r['numerator']} / {r['denominator']} (score {r['score']})" if r["denominator"]
                           else f"score {r['score']}", "note": r["note"], "files": []}
        # rate-file problems, summed over the dataset's files
        for f in files:
            for mid, pts in points_lost(by_file[f["rate_file"]], FILE_GROUPS, "M1").items():
                r = next(x for x in by_file[f["rate_file"]] if x["metric_id"] == mid)
                eff = (1 - INDEX_SHARE) * D / F * pts / len(files) * 100 if F and D else None
                it = issues.setdefault(mid, {"scope": "rate files", "points": 0.0, "index_points": False,
                                             "files": [], "note": ""})
                if eff is not None and it["points"] is not None:
                    it["points"] += eff
                elif eff is None:
                    it["points"] = None
                it["files"].append((f["rate_file"], r["score"], r["numerator"], r["denominator"], r["note"]))
        def severity(mid, it):
            p = it["points"] or 0
            sev = "High" if p >= HIGH else "Medium" if p >= MEDIUM else "Low"
            floor = FLOOR.get(mid)
            return floor if floor and ORDER.index(floor) < ORDER.index(sev) else sev

        for mid, it in issues.items():
            it["severity"] = severity(mid, it)
        minor = sorted(mid for mid, it in issues.items()
                       if it["severity"] == "Low" and it["points"] is not None and it["points"] < MINOR)
        ranked = sorted(((m, it) for m, it in issues.items() if m not in minor),
                        key=lambda kv: (ORDER.index(kv[1]["severity"]), -(kv[1]["points"] or 0)))
        md += [f"## {DISPLAY.get(name, name)}: dataset score {D:.3f}", "",
               f"Index {I:.3f}, mean of {len(files)} rate file(s) {F:.3f}; dataset = index^{INDEX_SHARE} x files^{1 - INDEX_SHARE:.1f}.", ""]
        if not ranked:
            md += ["No problems found.", ""]
        for rank, (mid, it) in enumerate(ranked, 1):
            sev = it["severity"]
            problem, why, fix_pub, fix_pipe = T.get(mid, (meta[mid]["metric"], "", "", ""))
            if sev == "Critical" and not D:
                pts = "all"
            elif it["points"] is None:
                pts = "n/a"
            else:
                pts = f"{it['points']:.2f}" + (" index" if it["index_points"] else "")
            if it["scope"] == "rate files":
                fl = sorted(it["files"], key=lambda x: float(x[1]))
                ev = evidence_file(mid, fl[0][0], hcpcs)
                scope = (f"{len(fl)} of {len(files)} rate files; worst: " +
                         ", ".join(f"{f[0]} {float(f[1]):.3f}" for f in fl[:3]))
                detail = f"worst file {fl[0][0]}: {fl[0][2]} / {fl[0][3]}; {fl[0][4]}".rstrip("; ")
            else:
                ev = evidence_index(mid, name)
                scope, detail = "index", f"{it['detail']}; {it['note']}".rstrip("; ")
            md += [f"### {rank}. [{sev}] {mid} {meta[mid]['metric']} ({pts} points)", "",
                   f"- **Problem:** {problem}", f"- **Where:** {scope}", f"- **Numbers:** {detail}"]
            if ev:
                md.append(f"- **Evidence:** {ev}")
            md += [f"- **Why it matters:** {why}", f"- **Fix (publisher):** {fix_pub}",
                   f"- **Fix (our pipeline):** {fix_pipe}", ""]
            rows_out.append({"dataset": name, "dataset_score": D, "rank": rank, "severity": sev, "metric_id": mid,
                             "metric": meta[mid]["metric"], "scope": it["scope"], "points_lost": pts,
                             "where": scope, "numbers": detail, "evidence": ev, "problem": problem,
                             "why": why, "fix_publisher": fix_pub, "fix_pipeline": fix_pipe})
        if minor:
            md += ["**Minor (under 0.01 points each):** " + ", ".join(f"{m} {meta[m]['metric']}" for m in minor), ""]
            for m in minor:
                rows_out.append({"dataset": name, "dataset_score": D, "rank": "", "severity": "Minor", "metric_id": m,
                                 "metric": meta[m]["metric"], "scope": issues[m]["scope"],
                                 "points_lost": f"{issues[m]['points']:.4f}", "where": "", "numbers": "",
                                 "evidence": "", "problem": T.get(m, ("",))[0], "why": "", "fix_publisher": "",
                                 "fix_pipeline": ""})
    with open(os.path.join(C3, "c3_diagnosis.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows_out[0]))
        w.writeheader()
        w.writerows(rows_out)
    with open(os.path.join(C3, "c3_diagnosis.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    for r in rows_out:
        print(f"{r['dataset']:10} {r['rank']:2} {r['severity']:8} {r['metric_id']:4} {r['points_lost']:>6}  {r['metric']}")


if __name__ == "__main__":
    main()
