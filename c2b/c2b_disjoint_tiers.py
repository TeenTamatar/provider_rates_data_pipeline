"""Compute disjoint TIN/NPI tier counts from step7 labelled parquet."""
import duckdb
from collections import defaultdict

con = duckdb.connect()
con.execute("CREATE TABLE s7 AS SELECT * FROM 'output/c2b_steps5to8/c2b_step7_labelled.parquet'")

TIER_RANK = {"PPO": 4, "shared": 3, "ambiguous": 2, "unknown": 1}

SEP = "-" * 65
def log(m=""): print(m, flush=True)

# ── Raw key-level (NOT disjoint) ──────────────────────────────────────────
log("KEY-LEVEL COUNTS (not disjoint — same TIN/NPI may appear in multiple rows):")
rows = con.execute("""
    SELECT network_label, evidence_type, confidence,
           count(*) AS rows,
           count(DISTINCT tin) AS tins,
           count(DISTINCT npi) AS npis
    FROM s7 WHERE include_in_step7
    GROUP BY network_label, evidence_type, confidence
    ORDER BY rows DESC
""").fetchall()
log(f"  {'network_label':<12} {'evidence_type':<24} {'conf':<8} {'rows':>6} {'tins':>5} {'npis':>5}")
log("  " + SEP)
for r in rows:
    log(f"  {str(r[0]):<12} {str(r[1]):<24} {str(r[2]):<8} {r[3]:>6} {r[4]:>5} {r[5]:>5}")
sum_tins = sum(r[4] for r in rows)
sum_npis = sum(r[5] for r in rows)
log(f"  {'SUM (overcounts)':36} {sum_tins:>5} {sum_npis:>5}")
log()

# ── Build per-TIN top tier ─────────────────────────────────────────────────
log("TIN-LEVEL DISJOINT (each TIN at its highest-evidence tier):")
tin_rows = con.execute("""
    SELECT tin, network_label, npi, include_in_step7
    FROM s7
""").fetchall()

# collect per-TIN: max rank, set of npis, key count at each label
tin_max_rank   = {}
tin_npis       = defaultdict(set)
tin_label_keys = defaultdict(lambda: defaultdict(int))

for tin, label, npi, incl in tin_rows:
    rank = TIER_RANK.get(label, 1)
    if tin not in tin_max_rank or rank > tin_max_rank[tin]:
        tin_max_rank[tin] = rank
    tin_npis[tin].add(npi)
    if incl:
        tin_label_keys[tin][label] += 1

rank_to_label = {v: k for k, v in TIER_RANK.items()}
tier_tins  = defaultdict(list)
for tin, rank in tin_max_rank.items():
    tier_tins[rank_to_label[rank]].append(tin)

tier_order = ["PPO", "shared", "ambiguous", "unknown"]
log(f"  {'tier':<12} {'TINs':>5}  {'NPIs':>5}  {'PPO keys':>9}  {'shared keys':>12}  {'unk/amb keys':>13}")
log("  " + SEP)

total_t = total_n = 0
summary = {}
for t in tier_order:
    tins = tier_tins[t]
    npis = set()
    ppo_k = sh_k = oth_k = 0
    for tin in tins:
        npis |= tin_npis[tin]
        ppo_k += tin_label_keys[tin].get("PPO", 0)
        sh_k  += tin_label_keys[tin].get("shared", 0)
        oth_k += tin_label_keys[tin].get("ambiguous", 0) + tin_label_keys[tin].get("unknown", 0)
    log(f"  {t:<12} {len(tins):>5}  {len(npis):>5}  {ppo_k:>9}  {sh_k:>12}  {oth_k:>13}")
    total_t += len(tins); total_n += len(npis)
    summary[t] = {"tins": len(tins), "npis": len(npis), "ppo_keys": ppo_k,
                  "shared_keys": sh_k, "other_keys": oth_k}

log(f"  {'TOTAL':<12} {total_t:>5}  {total_n:>5}")
log(f"  All-plans unique TINs: 2,856  NPIs: 3,694")
log()

# ── NPI-level disjoint ─────────────────────────────────────────────────────
log("NPI-LEVEL DISJOINT (each NPI at its highest-evidence tier):")
npi_max_rank = {}
for tin, label, npi, incl in tin_rows:
    rank = TIER_RANK.get(label, 1)
    if npi not in npi_max_rank or rank > npi_max_rank[npi]:
        npi_max_rank[npi] = rank

npi_tier_counts = defaultdict(int)
for npi, rank in npi_max_rank.items():
    npi_tier_counts[rank_to_label[rank]] += 1

total_n2 = 0
for t in tier_order:
    c = npi_tier_counts[t]
    log(f"  {t:<12} npis={c:>5}")
    total_n2 += c
log(f"  {'TOTAL':<12} npis={total_n2:>5}")
log()

# ── PPO pure vs PPO-touched ────────────────────────────────────────────────
log("PPO detail:")
ppo_pure = sum(1 for tin in tier_tins["PPO"]
               if set(tin_label_keys[tin].keys()) == {"PPO"})
log(f"  TINs in PPO tier (any PPO key):  {len(tier_tins['PPO'])}")
log(f"  TINs with ONLY PPO keys (pure):  {ppo_pure}")
ppo_also_shared = sum(1 for tin in tier_tins["PPO"]
                      if "shared" in tin_label_keys[tin])
ppo_also_unk = sum(1 for tin in tier_tins["PPO"]
                   if "unknown" in tin_label_keys[tin])
log(f"  PPO TINs that also have shared:  {ppo_also_shared}")
log(f"  PPO TINs that also have unknown: {ppo_also_unk}")
log()

# ── Mixed TINs: those spanning multiple labels ────────────────────────────
log("TINs by number of distinct network_labels they touch:")
label_counts = defaultdict(int)
for tin, lk in tin_label_keys.items():
    n = len(lk)
    label_counts[n] += 1
for n, c in sorted(label_counts.items()):
    log(f"  {n} distinct label(s): {c} TINs")
