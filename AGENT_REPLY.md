# Reply to your task report, plus decisions on the open questions

Thanks for the report. Tasks A and B are verified because they reproduce known numbers. Tasks C to F need the checks below before I treat them as done. Please do the checks first, then continue with the next steps.

## Part 1: Decisions on the open questions
Work with these defaults. Do not ask me again about them unless an organizer's answer changes them. I am asking the organizers separately.

| Question | Decision for now |
|---|---|
| Q3 Facility (revenue-code) prices | Keep them in the full output, flagged by `billing_class`. In the Excel view show only CPT/HCPCS rows on the HiLabs codes. Do not change C4. Filtering RC is a one-line change in C6 if the organizers say facility is out |
| Q9 What counts as a verified address | Provisional: at least two independent source families agree and one is recent. Applies to C5. Note: Q9 is about addresses, not SBC URLs |
| Q10 Scraping provider directories | **No.** Bulk files only (NPPES, CMS Doctors and Clinicians, geocoder). Do not scrape any directory page and do not try to bypass the Centene FHIR 403 |
| Q14 Excel row limit | Excel (limit 1,048,576 rows) holds the HiLabs-code rows only (about 8,615 expanded for Health Net CA). The complete output is CSV or Parquet alongside. Add a row-count guard that switches to CSV or splits sheets above 1,000,000 rows, and document it in the README |
| SBC URL pattern per payer | Not an organizer question. I will supply it. For Centene, the pattern used in the earlier experiment is in `experiments/network_inference/RESULTS.md`; read it from there. Do not invent hosts for other payers |

Before I ask the organizers about Q3, run this and report the result. If few RC keys land on HiLabs codes, the question mostly answers itself.

```python
# hilabs_codes: table of the 583 codes from the HiLabs workbook (column: code)
print(con.sql("""
  SELECT billing_code_type, count(*) AS keys,
         sum(CASE WHEN addl_key IN (SELECT code FROM hilabs_codes)
                   OR billing_code IN (SELECT code FROM hilabs_codes) THEN 1 ELSE 0 END) AS on_hilabs
  FROM chosen GROUP BY ALL""").df())
```

## Part 2: Review of your task report

### Task E: the strict and inclusive numbers
Strict 720 and inclusive 2,290 means roughly 1,570 plans have no network type, because Task D has not resolved anything. Do not describe `c2_plans_network.csv` as a finished classification. Add a column or summary line showing how many plans are `unknown` per state and why. Report the inclusive number as "PPO plus unknown", not as PPO.

### Scope check (do this first)
Organizers confirmed **commercial group plans only**. Check whether 720 and 2,290 include individual plans. Run and paste the output:

```python
import pandas as pd
df = pd.read_csv("output/c2/c2_plans_network.csv")
print(df.groupby(["state", "plan_market_type"]).size().unstack(fill_value=0))
print(df[df.plan_market_type == "group"].groupby(["state", "network_type"]).size())
```

I expect group plans to exist in very few states (possibly only California, Health Net), since Ambetter is the individual marketplace brand. If so, extending to other states for group scope is small, and I want to know before more effort goes into it. Do not start downloading rate files for other states until I decide.

### Task F: the runner test
`python run_state.py --state TX --market individual` proves the runner executes, but individual plans are out of scope. Re-run it on a state that has group plans (from the table above) and show its counts next to the C2a summary for that state. If no other state has group plans, say so.

### Task C: state profiles
Open `docs/state_profiles/` and confirm two things in every profile: new payer plan codes (like EOA or SAL) are listed under a "needs source" heading, and no meaning has been assigned to any of them. Also confirm each profile says which markets it covers. If any profile assigns a meaning without a payer document, fix it and tell me.

### Task D: evidence agent
It cannot resolve plans until it has document URLs. Do not guess a URL pattern. Wait for me, or use the manual `plan_id,url` CSV path. When it runs on real documents:
1. Run on 5 plans first and print the quote and source URL for each so I can check them against the document by eye.
2. Confirm the `robots.txt` check, per-host delay, and plan-year check are in place.
3. Leave `model_fallback()` as a stub unless I supply a model.

## Part 3: What to do next, in order
1. Run the Q3 check and the scope check above, and paste both outputs.
2. Re-run Task F on a group-market state and compare it to C2a.
3. Update `LOGIC.md` with: strict vs inclusive counts, how many plans are unresolved and why, and the decisions in Part 1.
4. **Stop and wait for me** before: downloading any new rate file, scraping anything, or running Task D on more than five plans.
5. After my reply, the priority goes back to the California critical path: C2b steps 5 to 8, then C3, C5 and C6.

## Rules that still apply
- No guessing network type. Evidence or `unknown`.
- Models only read and quote; every answer needs a quote found in the source text.
- No hardcoded paths. Cache everything. Do not overwrite existing outputs silently.
- If a number differs from what I gave you, stop and report instead of changing rules.
- Tell me the file size before downloading any multi-GB rate file.
- Report for each task: the command run, the summary JSON, the counts, and anything that differed.
