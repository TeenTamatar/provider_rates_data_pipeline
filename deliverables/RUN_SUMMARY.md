# Run summary

Generated 2026-10-18T22:32:24 from the stage outputs in `output/`. Machine-readable copy: `run_summary.json`.

## Inputs and scope

| Metric | Value |
|---|---|
| Index files parsed | 4 |
| Plan rows parsed (unique after de-duplication) | 3,215 (3,214) |
| Unique plan IDs | 544 |
| Rate files linked by the indexes | 33 |
| In-scope plans (California, commercial group) | 1,312 on 1 rate file |
| **PPO plans identified** | **456** (non-PPO: 856) |
| **Rate files carrying the PPO plans** | **1** |
| Price rows scanned in all rate files | 7,650,002 |

## File quality scores (0 to 1)

| Dataset | Index score | Dataset score |
|---|---|---|
| ambetter | 0.924 | 0.941 |
| fidelis | 0.920 | 0.862 |
| healthnet | 0.986 | 0.890 |
| qualchoice | 0.929 | 0.000 |

Rate files: 32 of 33 reachable; score of the reachable ones mean 0.935, min 0.836, max 0.975. Per-file scores: `output/c3/c3_rate_file_scores.csv`.

## Rates extracted

| Metric | Value |
|---|---|
| PPO price rows kept from the rate file | 1,498,236 |
| After de-duplication (TIN-NPI x group x service x rate) | 48,227 |
| Provider x service rates (PPO) | 34,284 |
| One rate per key chosen by C4 | 26,951 |
| **Rows in the final provider rate file** | **26,903** |
| Unique NPIs (PPO set, final file) | 3,685, 3,678 |
| Unique TINs (PPO set, final file) | 2,849, 2,846 |
| TIN-NPI pairs (PPO set, final file) | 3,729, 3,722 |
| Billing codes (final file) | 762 |

## Addresses (C5)

| Metric | Value |
|---|---|
| NPIs located | 3,685 |
| NPIs with a verified address | 5 (0.1%) |
| NPIs with a probable address | 1,026 (27.8%) |
| NPIs with a unverified address | 2,654 (72.0%) |
| **Share of NPIs linked to a verified address** | **0.1%** |
| Same under the loose profile | 26.9% |

Evidence families present in the claims: nppes_self (13,059), medicare (464). An NPI counts as verified when any of its TIN-NPI pairs is verified (strict: two independent families agree and the newest evidence is at most 24 months old).

## Cost-effectiveness (C7)

| Metric | Value |
|---|---|
| Prices scored | 26,903 (compared with peers: 24,899) |
| Providers labelled `insufficient_peers` | 2,313 |
| Providers labelled `typical` | 740 |
| Providers labelled `lower_cost` | 280 |
| Providers labelled `higher_cost` | 267 |
| Providers labelled `regional_average_only` | 122 |
