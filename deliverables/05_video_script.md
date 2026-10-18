# Video script: Centene PPO provider rate pipeline (max 5:00)

Target: about 650 spoken words at 130 words per minute. Timings are cumulative. Screen cues are in *italics*.
Numbers are from the 2026-10-18 run. Re-check them against `output/` and `LOGIC.md` before recording.

---

## 0:00 – 0:30 | The problem

*Screen: title slide, then the pipeline diagram (C1 → C7).*

"Centene publishes its prices as huge machine-readable files: four index files, 33 rate files, up to 700 megabytes each. Nothing in them says 'this is the PPO price for this doctor at this address'. Our job was to build that: a provider-level PPO rate file for **Centene, California, commercial group plans**, with NPI, TIN, billing code, address, specialty and rate. We also had to document our logic and grade the quality of every file we used."

## 0:30 – 1:15 | C1 and C2: which plans and which files

*Screen: C1 output table, then the plan-classification table.*

"C1 streams the index files with a JSON streaming parser, so no file is ever loaded whole. It gives 3,214 unique plan rows. The organizers confirmed that 'commercial group' means employer plans only, so we dropped individual Ambetter plans. That leaves 1,312 Health Net California group plans, all pointing to one rate file.

C2 decides which of those plans are PPO. We classify by network, not by benefit design, using plan names and a sourced code table. Result: 456 PPO plans and 856 non-PPO. EOA plans are non-PPO: Health Net's own flyer says their primary care comes from the HMO network. Every rule has an ID and a written reason in our logic file."

## 1:15 – 2:00 | C2b: PPO prices inside a shared file

*Screen: funnel graphic: 1.5M raw rows → 48,227 PPO rows.*

"The hard part: that one file is shared by PPO, HMO and EPO plans, and it has no network field. So we treat a rate as PPO unless there is evidence against it. We remove entries named for other networks, such as Ambetter or SIMNSA, capitation prices, and expired contracts. And where the same provider and service has different rates in different groups, which may be different networks, we drop it. That is our **strict route**: it costs 2.5% of rates and every kept rate is unambiguous.

The file is 97% duplicate rows. After deduplication, 1.5 million raw rows become 48,227 PPO rows covering 3,729 provider pairs."

## 2:00 – 2:45 | C3: grading every file

*Screen: score table for Ambetter, Health Net, Fidelis, QualChoice.*

"C3 scores every file we touched. A file must first be reachable, then it gets four scores: timeliness, compliance, plausibility and usability, from 27 rate-file metrics and 15 index metrics. Each metric has a calculation, a reason, and a basis such as the CMS schema, Medicare fees, NPPES, or the IRS.

Ambetter scores 0.95, Health Net 0.85, Fidelis 0.84. QualChoice scores zero because its rate file returns a 404. The biggest problems are missing network names, 96% duplicate rows, and revenue codes that lost their leading zeros. Our scans run on all 32 downloadable files in about a minute, so the scoring scales to other payers."

## 2:45 – 3:30 | C4: one rate per key

*Screen: C4 rule table and the 26,951-key result.*

"A provider can have several rates for one service. C4 applies a written ladder. If there is no conflict, keep it. Otherwise match on modifier, then on place of service, then on the rate most provider groups agree on. Only if all fail do we keep the lowest rate, and we flag it. Of 26,951 keys, 26,322 had no conflict and only 188, or 0.7%, needed the fallback."

## 3:30 – 4:15 | C5 and C6: addresses and specialty

*Screen: address source diagram and the test-set results table.*

"NPPES addresses are stale, so C5 does not rely on them. It combines several sources, including the CMS Doctors and Clinicians file and website extraction with Gemini, and gives each address a confidence label. We built our own test set: 707 pairs with ground truth from the California facility register, which was held out from the pipeline, and from CMS. NPPES alone gets 96% on facilities and 70% on clinicians, and 0% on the hard cases where it is wrong. Beating that on the hard cases is the goal.

C6 joins it all: one row per provider and service, with the chosen address, the specialty from the HiLabs crosswalk or NUCC, and quality flags. Nothing is silently dropped; rows are flagged. That gives 26,903 rows."

## 4:15 – 4:45 | C7: cost-effectiveness

*Screen: map with 19 regions, then cost-index labels.*

"For cost-effectiveness we compare each price only to the same service, from other TINs, in the same specialty and region. The search widens step by step until it has at least five providers from three TINs. Each provider gets a weighted, shrunk cost index, and a label: lower, typical or higher cost, or insufficient peers. We checked it against Medicare: the cost index correlates 0.69 with percent of Medicare."

## 4:45 – 5:00 | Close

*Screen: `python run_pipeline.py` and the repo.*

"The whole thing runs with one command, `python run_pipeline.py`. Every decision is in LOGIC.md, and every file has a score. Thank you."

---

## Recording notes

- **Be honest about limits** if time allows (one sentence in C5 or the close): no address reached 'verified' yet (999 probable, 2,718 unverified), and most providers have only one priced code, so 2,313 get 'insufficient peers' in C7.
- **If you are over 5:00**, cut the C4 section to two sentences, then trim C7.
- **Confirm the video requirements** (format, whether it must show the code or a demo) against the problem statement before recording. I could not open that Notion page, so the 5-minute cap comes from your Execution Plan.
