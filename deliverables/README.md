# Deliverables

Final outputs of the latest full run (2026-10-18). Each file is a copy of what the pipeline writes; re-running `python run_pipeline.py` regenerates the originals under `output/` (and files 03 directly here).

| File | What it is | Source |
|---|---|---|
| `01_provider_rate_file.csv`, `.xlsx` | Provider-level PPO rate file for California commercial group plans: one row per TIN × NPI × billing code × sub-code, with specialty, practice address, rate and the provider's cost-effectiveness score. 26,903 rows. | `output/c7/c7_final.*` |
| `02_logic.md`, `.pdf` | Logic file: scope, assumptions, and every rule with its reason, for (a) identifying the PPO network and (b) mapping TIN → NPI → address and specialty. | `LOGIC.md`, `LOGIC.pdf` |
| `03_file_quality_metrics.md`, `03_file_quality_scores.xlsx` | File quality score: every metric, how it is calculated and why, and the score of every index and rate file. | `pipeline/c3_report.py` |
| `04_cost_effectiveness_by_provider.csv` | Cost index and label per TIN–NPI pair (3,722 rows). | `output/c7/c7_npi_cost.csv` |
| `04_cost_effectiveness_by_tin.csv` | Cost index and label per TIN (2,846 rows). | `output/c7/c7_tin_cost.csv` |
| `05_video_script.md`, `05_video_narration.md` | Script and narration for the walkthrough video (max 5 minutes). | |
| `RUN_SUMMARY.md` | Headline numbers of the run. | `output/RUN_SUMMARY.md` |

How each stage works is in the repository [README](../README.md).
