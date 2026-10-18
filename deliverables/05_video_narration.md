# Narration (read aloud, about 4:50)

Centene publishes its prices as huge machine-readable files: four index files and 33 rate files, some of them 700 megabytes. Nothing in them says "this is the PPO price for this doctor at this address". Our job was to build exactly that: a provider-level PPO rate file for Centene, California, commercial group plans, with NPI, TIN, billing code, address, specialty and rate. We also had to document our logic and grade every file we used.

We built this as seven steps, C1 to C7.

C1 reads the index files. We stream them, so no file is ever loaded whole. The organizers confirmed that "commercial group" means employer plans only, so individual Ambetter plans are out. That leaves 1,312 Health Net California group plans, all pointing to one rate file.

C2 decides which of those plans are PPO. We classify by network, not by benefit design, using plan names and a sourced code table. The result is 456 PPO plans and 856 non-PPO plans. For example, EOA plans are non-PPO, because Health Net's own flyer says their primary care comes from the HMO network. Every rule has an ID and a written reason in our logic file.

The hard part is that this one file is shared by PPO, HMO and EPO plans, and it has no network field. So we treat a rate as PPO unless there is evidence against it. We remove entries named for other networks, capitation prices, and expired contracts. Where the same provider and service have different rates in different groups, they may belong to different networks, so we drop them. We call this the strict route. It costs about 2.5 percent of rates, and every rate we keep is unambiguous. The file is 97 percent duplicate rows. After cleaning, 1.5 million raw rows become 48,227 PPO rows, covering 3,729 provider pairs.

C3 grades every file. A file must first be reachable. Then it gets four scores: timeliness, compliance, plausibility and usability, built from 27 rate-file metrics and 15 index metrics. Each metric has a calculation, a reason, and a basis, such as the CMS schema, Medicare fees, NPPES or the IRS. Ambetter scores 0.95, Health Net 0.85 and Fidelis 0.84. QualChoice scores zero, because its rate file returns a 404. The main problems are missing network names, 96 percent duplicate rows, and revenue codes that lost their leading zeros. Scanning all 32 downloadable files takes about a minute, so the scoring scales to other payers.

C4 picks one rate when a provider has several for one service. We follow a written ladder: no conflict, keep it. Otherwise match on modifier, then place of service, then the rate most provider groups agree on. Only if everything fails do we keep the lowest rate, and we flag it. Of 26,951 keys, only 188, or 0.7 percent, needed that fallback.

C5 finds practice addresses. NPPES addresses are stale, so we do not rely on them alone. We combine several sources, including the CMS Doctors and Clinicians file and website extraction with Gemini, and give each address a confidence label. To test it, we built our own ground truth: 707 pairs, using the California facility register, which we held out of the pipeline, and CMS data. NPPES alone gets 96 percent on facilities and 70 percent on clinicians, but zero on the hard cases where it is wrong. Winning those cases is the goal. Honestly, no address has reached our "verified" label yet; most are probable or unverified.

C6 joins everything into the final file: one row per provider and service, with the chosen rate, address, specialty from the HiLabs crosswalk or NUCC, and quality flags. Nothing is silently dropped; problem rows are flagged. That gives 26,903 rows.

C7 measures cost-effectiveness. We compare each price only with the same service, from other TINs, in the same specialty and region. The search widens step by step until it has at least five providers from three TINs. Each provider gets a weighted cost index, and a label: lower cost, typical, higher cost, or insufficient peers. As a check, the index correlates 0.69 with percent of Medicare.

The whole pipeline runs with one command: python run_pipeline.py. Every decision is in LOGIC.md, and every file has a score. Thank you.
