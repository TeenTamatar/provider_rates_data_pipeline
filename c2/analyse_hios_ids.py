"""
Analyse the 1,570 unknown plans to answer:
1. Of the 481 without hios_product_id, how many have a usable plan_id?
   "Usable" = plan_id is 14+ chars long (HIOS format: issuer(5)+state(2)+product(3)+variant(2))
2. Brand/market breakdown of the 481
3. For CMS join: how many unknown plans have a 14-char HIOS plan_id vs only a short one?
"""
import pandas as pd

df = pd.read_csv("output/c2a_network/c2a_unresolved.csv")
print(f"Total unknown plans: {len(df)}")
print()

# Plans WITHOUT hios_product_id
no_prod = df[df["hios_product_id"].isna() | (df["hios_product_id"].astype(str).str.strip() == "")]
print(f"Plans without hios_product_id: {len(no_prod)}")
print()

print("Brand / market of the 481 without hios_product_id:")
print(no_prod.groupby(["brand", "plan_market_type"]).size().to_string())
print()

# Of those 481, how many have a plan_id that is 14+ chars?
def usable_plan_id(pid):
    s = str(pid).strip()
    # HIOS plan ID is 14 chars: 5-digit issuer + 2-char state + 3 product + 2 variant + 2 extra
    # At minimum we need >= 14 chars to strip variant and get a 12-char join key
    return len(s) >= 14 and s.replace("-","").isalnum()

no_prod = no_prod.copy()
no_prod["has_usable_plan_id"] = no_prod["plan_id"].apply(lambda x: usable_plan_id(str(x)))
print("Of the 481: usable plan_id (>=14 chars):")
print(no_prod["has_usable_plan_id"].value_counts().to_string())
print()
print("Sample plan_ids from the 481:")
for pid in no_prod["plan_id"].dropna().unique()[:10]:
    print(f"  '{pid}'  len={len(str(pid).strip())}")

print()
# Plans WITH hios_product_id — how many have a plan_id too?
has_prod = df[~(df["hios_product_id"].isna() | (df["hios_product_id"].astype(str).str.strip() == ""))]
print(f"Plans WITH hios_product_id: {len(has_prod)}")
has_prod = has_prod.copy()
has_prod["has_usable_plan_id"] = has_prod["plan_id"].apply(lambda x: usable_plan_id(str(x)))
print("Of those: also have usable plan_id (>=14 chars):")
print(has_prod["has_usable_plan_id"].value_counts().to_string())

print()
# Overall: how many have a 14-char HIOS plan_id (for the CMS 14-char join)?
df["has_usable_plan_id"] = df["plan_id"].apply(lambda x: usable_plan_id(str(x)))
print("ALL 1,570 unknown plans — usable plan_id for 14-char CMS join:")
print(df.groupby(["brand","plan_market_type","has_usable_plan_id"]).size().to_string())
