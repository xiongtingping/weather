import numpy as np, pandas as pd
from load import load
df = load()
dec = df.drop_duplicates("dec").copy()
print("== P0 version by event date (decisions) ==")
p0 = dec[dec.stage=="P0"]
print(p0.groupby("ver").agg(n=("dec","size"), sd=("st", lambda s: 0), dmin=("d","min"), dmax=("d","max"), tmin=("t","min"), tmax=("t","max")).drop(columns="sd").to_string())
print("== P0 first decisions: hours to settlement ==")
f = p0[p0["first"]==1]
print(f["h"].describe().to_string())
print(pd.cut(f["h"], [-100,0,12,24,36,48,60,72,200]).value_counts().sort_index().to_string())
print("first P0 with q complete:", f["q"].notna().mean(), "n", len(f))
print("== all decisions by stage x hours bin ==")
dec["hb"] = pd.cut(dec["h"], [-100,0,6,12,24,36,48,60,200])
print(dec.pivot_table(index="hb", columns="stage", values="dec", aggfunc="count", observed=False).to_string())
print("== q complete share by stage x hb ==")
dec["qc"] = dec["q"].notna()
print(dec.pivot_table(index="hb", columns="stage", values="qc", aggfunc="mean", observed=False).round(3).to_string())
print("== station-days by stage ==")
print(dec.groupby("stage").apply(lambda g: g.groupby(["st","d"]).ngroups).to_string())
print("== spread (ask-bid) median by hb, buckets with both sides ==")
df["hb"] = pd.cut(df["h"], [-100,0,6,12,24,36,48,60,200])
both = df[df.bid.notna() & df.ask.notna()]
print(both.assign(sp=both.ask-both.bid).groupby("hb", observed=False)["sp"].describe()[["count","50%","75%","mean"]].to_string())
print("share of buckets w/o bid:", df.bid.isna().groupby(df.hb, observed=False).mean().round(3).to_string())
print("mid_sum by hb:", dec.groupby("hb", observed=False)["ms"].median().round(3).to_string())
print("units:", dec.groupby("unit").apply(lambda g: g.groupby(["st","d"]).ngroups).to_string())
print("nsys P0:", p0["nsys"].value_counts(dropna=False).sort_index().to_string())
