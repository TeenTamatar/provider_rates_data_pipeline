"""C7 helper: suggest specialty merges with a small embedding model and compare with reference/peer_specialty.csv.

Embeds the specialty labels (BAAI/bge-small-en-v1.5), then lists
  - pairs the model finds similar (cosine >= --threshold) that the table keeps in different peer specialties
  - pairs the table merges that the model finds dissimilar (cosine < --low)
Nothing is changed automatically: a person reviews the lists and edits the CSV.
Output: output/c7/c7_specialty_suggestions.csv
"""
import argparse
from pathlib import Path

import numpy as np
import pandas as pd


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--map", default="reference/peer_specialty.csv")
    ap.add_argument("--model", default="BAAI/bge-small-en-v1.5")
    ap.add_argument("--threshold", type=float, default=0.85)
    ap.add_argument("--low", type=float, default=0.60)
    ap.add_argument("--out", default="output/c7/c7_specialty_suggestions.csv")
    a = ap.parse_args()
    from sentence_transformers import SentenceTransformer

    m = pd.read_csv(a.map)
    E = SentenceTransformer(a.model).encode(m.specialty.tolist(), normalize_embeddings=True)
    S = E @ E.T
    rows = []
    for i in range(len(m)):
        for j in range(i + 1, len(m)):
            same = m.peer_specialty[i] == m.peer_specialty[j]
            if not same and S[i, j] >= a.threshold:
                kind = "model_similar_table_separate"
            elif same and S[i, j] < a.low:
                kind = "table_merged_model_dissimilar"
            else:
                continue
            rows.append((kind, m.specialty[i], m.specialty[j], m.peer_specialty[i], m.peer_specialty[j],
                         round(float(S[i, j]), 3)))
    d = pd.DataFrame(rows, columns=["kind", "specialty_a", "specialty_b", "peer_a", "peer_b", "cosine"])
    d = d.sort_values(["kind", "cosine"], ascending=[True, False])
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    d.to_csv(a.out, index=False)
    print(d.to_string(index=False))


if __name__ == "__main__":
    main()
