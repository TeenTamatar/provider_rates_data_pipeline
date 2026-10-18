"""Task C + Task E: Write per-state profiles and merge all plans into one network table.

Task C: docs/state_profiles/<STATE>.md
Task E: output/c2/c2_plans_network.csv  (strict + inclusive flags)

Usage:
    python c2/c2_merge_network.py --plans output/c2a_network/c2a_plans.csv \\
        --evidence output/c2_evidence/c2_evidence_all.csv \\
        --out-dir output/c2 --profiles-dir docs/state_profiles
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

KNOWN_RE = re.compile(r'\b(PPO|HMO|EPO|POS|EOA|SAL)\b', re.I)
TOKEN_RE  = re.compile(r'[A-Z]{2,6}')


def load_csv(path: str) -> list[dict]:
    if not path or not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


KNOWN_NET_TYPES = {"PPO", "HMO", "EPO", "POS"}
# These codes came from payer documents already cited in PLAN_CODES in c2a_classify.py.
# Any other code that appears is 'new' and needs a source before meaning is assigned.
SOURCED_CODES = {"EOA", "SAL"}  # Health Net codes with documented meaning


def top_unknown_fragments(rows: list[dict], n: int = 10) -> list[tuple[str, int]]:
    frags: Counter = Counter()
    for r in rows:
        for tok in TOKEN_RE.findall(r.get("plan_name", "").upper()):
            if not KNOWN_RE.match(tok) and 2 <= len(tok) <= 6:
                frags[tok] += 1
    return frags.most_common(n)


def id_oddities(rows: list[dict]) -> list[str]:
    notes = []
    mismatches = sum(r.get("plan_id_type_mismatch", "False") == "True" for r in rows)
    if mismatches:
        notes.append(f"{mismatches} rows with plan_id_type label mismatch (declared vs inferred)")
    types = Counter(r.get("plan_id_type_inferred", "") for r in rows)
    for t, n in types.items():
        notes.append(f"  {n} rows inferred as `{t}`")
    return notes


def write_state_profile(state: str, rows: list[dict], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)

    total = len(rows)
    by_market = Counter(r.get("plan_market_type", "unknown") for r in rows)
    by_brand  = Counter(r.get("brand", "unknown") for r in rows)
    by_net    = Counter(r.get("network_type", "unknown") for r in rows)
    unknown   = [r for r in rows if r.get("network_type") == "unknown"]
    # Only flag truly unreachable URLs (non-200 status with an error)
    dead_links = [r for r in rows
                  if r.get("head_error") and str(r.get("http_status", "")) != "200"]

    frags = top_unknown_fragments(unknown)
    oddities = id_oddities(rows)
    markets_covered = ", ".join(sorted(by_market.keys()))
    rate_files = sorted({r.get("rate_file_url", "") for r in rows if r.get("rate_file_url")})

    # Collect new plan codes that appear in this state but have no documented meaning
    all_codes_in_state = set()
    for r in rows:
        net = r.get("network_type", "")
        token = r.get("matched_token", "")
        for code in (net + "+" + token).split("+"):
            code = code.strip()
            if code and code not in KNOWN_NET_TYPES and code not in SOURCED_CODES and code != "unknown":
                all_codes_in_state.add(code)
    new_codes_needing_source = sorted(all_codes_in_state)

    lines = [
        f"# State Profile: {state}",
        f"",
        f"_Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d')}_",
        f"",
        f"**Markets covered in this profile:** {markets_covered}",
        f"",
        f"## Plan counts",
        f"",
        f"| Dimension | Value |",
        f"|---|---|",
        f"| Total plan rows | {total} |",
    ]
    for market, n in by_market.most_common():
        lines.append(f"| Market: {market} | {n} |")
    for brand, n in by_brand.most_common():
        lines.append(f"| Brand: {brand} | {n} |")
    lines += ["", "## Network type breakdown (name-keyword pass)", ""]
    for net, n in by_net.most_common():
        pct = 100 * n / total if total else 0
        lines.append(f"- `{net}`: {n} ({pct:.1f}%)")

    lines += ["", "## New payer plan codes — needs source", ""]
    if new_codes_needing_source:
        lines.append("> **No meaning has been assigned to any of these codes.**")
        lines.append("> Their meaning must come from payer documents. They are listed here for the user to supply a source.")
        lines.append("")
        for code in new_codes_needing_source:
            cnt = sum(1 for r in rows if code in (r.get("network_type", "") + r.get("matched_token", "")))
            lines.append(f"- `{code}` — {cnt} plans — _needs source (payer document URL required)_")
    else:
        lines.append("_No new plan codes in this state._")

    lines += ["", "## Unknown plan name fragments (name-keyword pass left these unresolved)", ""]
    if frags:
        lines.append("These tokens appear in unknown-type plan names and are not recognised keywords.")
        lines.append("They are listed only as observations — **no meaning is assigned.**")
        lines.append("")
        for tok, cnt in frags:
            lines.append(f"- `{tok}` — {cnt} plans")
    else:
        lines.append("_No unknown plans in this state._")

    lines += ["", "## Plan ID oddities", ""]
    if oddities:
        for o in oddities:
            lines.append(f"- {o}")
    else:
        lines.append("_None._")

    lines += ["", "## Rate file URLs", ""]
    for url in rate_files:
        lines.append(f"- `{url}`")

    if dead_links:
        lines += ["", "## Dead / unreachable rate files", ""]
        seen = set()
        for r in dead_links:
            url = r.get("rate_file_url", "")
            if url and url not in seen:
                seen.add(url)
                lines.append(f"- `{url}` — status={r.get('http_status', '')} error={r.get('head_error', '')}")

    out_path.write_text("\n".join(lines), encoding="utf-8")


def merge_evidence(plans: list[dict], evidence: list[dict]) -> list[dict]:
    """Merge c2_evidence results back onto unresolved rows from c2a."""
    # Build lookup: (plan_id, plan_name, rate_file_url) -> evidence row
    ev_by_key: dict[tuple, dict] = {}
    for e in evidence:
        key = (e.get("plan_id",""), e.get("plan_name",""), e.get("rate_file_url",""))
        ev_by_key[key] = e

    merged = []
    for r in plans:
        if r.get("network_type") == "unknown":
            key = (r.get("plan_id",""), r.get("plan_name",""), r.get("rate_file_url",""))
            ev = ev_by_key.get(key)
            if ev and ev.get("network_type","unknown") != "unknown":
                merged.append({**r,
                    "network_type":  ev["network_type"],
                    "is_ppo":        ev.get("is_ppo",""),
                    "evidence_type": ev.get("evidence_type",""),
                    "source_url":    ev.get("source_url",""),
                    "quote":         ev.get("quote",""),
                    "confidence":    ev.get("confidence",""),
                })
                continue
        merged.append(r)
    return merged


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plans", required=True, help="c2a_plans.csv (all plans with network_type)")
    ap.add_argument("--evidence", default="", help="c2_evidence_all.csv (optional)")
    ap.add_argument("--out-dir", default="output/c2")
    ap.add_argument("--profiles-dir", default="docs/state_profiles")
    args = ap.parse_args(argv)

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    profiles = Path(args.profiles_dir)

    plans   = load_csv(args.plans)
    evidence = load_csv(args.evidence)

    if evidence:
        plans = merge_evidence(plans, evidence)

    # Add strict / inclusive flags
    for r in plans:
        net = r.get("network_type","unknown")
        r["is_strict"]    = "true" if net == "PPO" else "false"
        r["is_inclusive"] = "true" if net in ("PPO","unknown") else "false"

    # Task E: write merged network table
    cols = list(plans[0].keys()) if plans else []
    for c in ["network_type","is_ppo","evidence_type","source_url","quote",
              "confidence","is_strict","is_inclusive"]:
        if c not in cols:
            cols.append(c)

    net_path = out / "c2_plans_network.csv"
    with open(net_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(plans)

    # Task C: state profiles
    states = sorted({r.get("state","") for r in plans if r.get("state")})
    for state in states:
        state_rows = [r for r in plans if r.get("state") == state]
        write_state_profile(state, state_rows, profiles / f"{state}.md")

    # Summary
    by_net = Counter(r.get("network_type","unknown") for r in plans)
    strict_ppo  = sum(r.get("is_strict")    == "true" for r in plans)
    inclusive   = sum(r.get("is_inclusive") == "true" for r in plans)

    summary = {
        "run_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total_plans": len(plans),
        "by_network_type": dict(by_net.most_common()),
        "strict_ppo": strict_ppo,
        "inclusive_ppo_plus_unknown": inclusive,
        "states_profiled": len(states),
        "profiles_written_to": str(profiles),
        "network_table": str(net_path),
        "by_state": {
            s: dict(Counter(r.get("network_type","unknown")
                            for r in plans if r.get("state")==s).most_common())
            for s in states
        },
    }
    summary_path = out / "c2_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    return summary


if __name__ == "__main__":
    main()
