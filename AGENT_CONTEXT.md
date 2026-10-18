# Agent brief: extend the provider-rates pipeline to other states

## 1. Project in one paragraph
Take-home challenge "Data Pipeline Curation for Provider Rates". We turn Centene's price transparency (Transparency in Coverage) files into one provider-level **PPO** rate file: NPI x TIN x billing code x address x specialty x rate. Steps are C1 (parse index files) to C7 (cost effectiveness). Scoring: Logic 40, Metrics 35, Structure 25. **Explained, defensible logic matters more than volume.** The repo must re-run from the README with **no hardcoded paths**.

## 2. Vocabulary (do not guess these)
- **NPI**: 10-digit provider ID (Type 1 person, Type 2 organization). **TIN**: 9-digit tax ID of the billing entity; one TIN has many NPIs.
- **Index file**: JSON table of contents. Plans, plan IDs, market type, rate file URLs. **No rates, no NPIs, no network type.**
- **Rate file**: huge in-network JSON. `provider_references` (provider_group_id -> TIN + NPIs + business_name) and `in_network` (billing code -> negotiated_rates -> provider_references + negotiated_prices). **No network/plan field, no address.**
- **NPPES**: national NPI registry (specialty taxonomy codes, addresses, ~80% stale). **NUCC**: taxonomy code -> specialty name.
- Plan types: PPO, HMO, EPO, POS. Our target is the PPO network. In the Health Net CA file "contracts" are inferred from price patterns, not read from a field.

## 3. Data and scope
- Four Centene index files: Ambetter, Fidelis, QualChoice, Health Net (URLs in section 7).
- **Organizers' scope (17 Oct): commercial group plans only.** For California that is Health Net CA (1,312 group plans, 1 rate file). Ambetter CA (individual) is out.
- Now extending to **other states** whose index files give no network type.

## 4. What is already done (California)
| Step | Result |
|---|---|
| C1 | 3,214 plans, 1,312 CA group plans, 33 unique rate files, QualChoice file 404, 154 Health Net IDs labelled `ein` that are really HIOS |
| C2a | CA group: 456 PPO, 856 non-PPO (HMO 719, EOA 127, POS 6, SAL 4), 0 unknown, 0 conflicts |
| C2b steps 1-4 | Group tables, business-name markers, capitation flags, contract dates. Steps 5-8 (parallel-contract test, labels, strict vs inclusive, plan confidence) **not done** |
| C4 | Done as a first version (`c4_select.py`): 27,565 chosen keys, 95.0% no conflict, funnel ends at 0 |
| C3, C5, C6, C7 | Not started |

Existing scripts (reuse, do not rewrite): `c1_parse_index.py`, `c2a_classify.py`, `c2_network_evidence.py`, `c4_select.py`.

## 5. Tasks for the agent (in order)

### Task A: Run C1 on all four index files
```
python c1_parse_index.py --out output/c1 --index <4 URLs or Data/*_index.json>
```
Acceptance: `c1_summary.json` shows 4 index files, about 3,214 unique plans, 33 unique rate files, 0 state conflicts. Differences from these numbers must be explained (index dates are 2026-09-28 and 2026-09-30). If downloading is blocked (403), tell the user to put the files in `Data/` and re-run with the glob; do not work around the block.

### Task B: Run C2a per state
```
python c2a_classify.py --plans output/c1/c1_plans.csv --out-dir output/c2a
```
Acceptance: CA group reproduces the numbers in section 4. Summary has a per-state breakdown. `c2a_unresolved.csv` lists unknown plans.

### Task C: Profile each new state before applying rules
For every state in scope, write `docs/state_profiles/<STATE>.md`: plan counts by market and brand, most frequent unmatched plan-name fragments, ID-label oddities, dead links. **List new payer plan codes (like EOA, SAL) but do not assign them a meaning.** Their meaning must come from payer documents; leave them in a "needs source" list for the user.

### Task D: Resolve unlabelled plans by reading documents
Use and extend `c2_network_evidence.py`:
1. Add a `discover` source for the CMS Plan Attributes file (join on HIOS plan ID) where it covers the plans. Verify its column names from the actual file; do not assume.
2. Keep the URL-pattern and manual `--urls` sources. Ask the user for the real SBC URL pattern per payer; do not invent hosts.
3. Add a `robots.txt` check and a per-host delay before any fetch.
4. Add a plan-year check (reject documents for the wrong year).
5. Leave `model_fallback()` as a stub unless the user supplies a model; if wired, it must return a **verbatim quote** that is checked against the document text.

Acceptance: every output row has `network_type`, `evidence_type`, `source_url`, `quote`, `confidence`. A plan with no document stays `unknown`.

### Task E: Merge into one network table
`output/c2/c2_plans_network.csv`: all plans, one row each, with network_type, evidence_type, confidence, and strict/inclusive flags. Strict = labelled PPO only. Inclusive = adds unknown-type plans. Summary reports counts both ways per state.

### Task F: Config-driven, per-state runner
`python run_state.py --state TX --market group` runs C1 filter, C2a, Task D and Task E for that state. All payer URLs, patterns, code lists and rate limits live in `config/*.json`. No state logic hardcoded in code.

## 6. Hard rules
1. **No guessing network type.** Evidence or `unknown`. Tested fact: guessing from names is right 61% (large model) or 25% (small model); reading a document was 51 of 51.
2. Models only **read and quote**; every answer needs a quote found in the source text.
3. Cache every download, fetch and model answer. A re-run makes zero network calls.
4. No hardcoded paths. Paths come from arguments or config. Use `argparse` `parse_args()` in scripts (not in notebooks).
5. **Do not expand the rate file to one row per NPI.** Keep normalized Parquet tables (groups, prices). Streaming parsers (`ijson`), DuckDB for queries.
6. Do not download multi-GB rate files without telling the user the file size first.
7. Respect site terms and `robots.txt`. No scraping of provider directories until the user confirms Q10 (are directories allowed?). Centene's FHIR directory returns 403; do not try to bypass it.
8. Every rule added must also be written into `LOGIC.md` with its reason (what it does, why, counts). Every step writes a summary JSON.
9. Never overwrite existing outputs silently. Write to a new folder or ask.
10. If a result differs from the numbers in section 4, stop and report; do not change rules to match.

## 7. Source URLs
- https://www.centene.com/content/dam/centene/Centene%20Corporate/json/DOCUMENT/2026-09-28_ambetter_index.json
- https://www.centene.com/content/dam/centene/Centene%20Corporate/json/DOCUMENT/2026-09-28_fidelis_index.json
- https://www.centene.com/content/dam/centene/Centene%20Corporate/json/DOCUMENT/2026-09-28_qualchoice_index.json
- https://www.centene.com/content/dam/centene/Centene%20Corporate/json/DOCUMENT/2026-09-30_healthnet_index.json
- Rate files: from `output/c1/c1_rate_files.csv` (column `rate_file_url`).

## 8. Environment
Windows, Python venv at `.venv` (`pip install duckdb pandas pyarrow ijson pypdf`). Project root `d:\Hilabs`. Run scripts from a terminal, not a notebook (`c1_parse_index.py` uses a process pool).

## 9. Open questions (ask the user, do not decide)
- Q3: are facility (revenue-code) prices in scope? (56% of chosen C4 keys are RC)
- Q10: may directories be scraped?
- Q14: Excel row limit handling for the final file.
- Real SBC URL pattern per payer; meaning of any new plan code.
- Answer-key sample format for checking C4.

## 10. Report back with
For each task: the command run, the summary JSON, counts vs the expected numbers, anything that differed, and a list of items needing the user's decision. Do not mark a task done if its acceptance check fails.
