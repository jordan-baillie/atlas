"""One-time Databento pull: GLBX.MDP3 ohlcv-1d, all contract months, 17 commodity roots, 2010->today.
Quoted $43.24 total (metadata.get_cost, 2026-06-12). Per-root files so partial progress persists.
Idempotent: skips roots whose parquet already exists."""
import json, os, sys, time
import databento as db

ROOTS = ["CL","NG","HO","RB","GC","SI","HG","PL","PA",
         "ZC","ZS","ZW","ZL","ZM","LE","HE","GF"]
OUT = "/root/atlas/data/databento"
START, END = "2010-06-06", "2026-06-11"

key = json.load(open("/root/.atlas-secrets.json"))["DATABENTO_API_KEY"]
client = db.Historical(key)
total_cost = 0.0
for root in ROOTS:
    path = os.path.join(OUT, f"{root}_ohlcv1d.parquet")
    if os.path.exists(path):
        print(f"{root}: exists, skip", flush=True)
        continue
    kw = dict(dataset="GLBX.MDP3", schema="ohlcv-1d", symbols=[f"{root}.FUT"],
              stype_in="parent", start=START, end=END)
    cost = client.metadata.get_cost(**kw)
    print(f"{root}: pulling (${cost:.2f}) ...", flush=True)
    for attempt in range(3):
        try:
            data = client.timeseries.get_range(**kw)
            df = data.to_df()
            break
        except Exception as e:
            print(f"{root}: attempt {attempt+1} failed: {e}", flush=True)
            time.sleep(20)
    else:
        print(f"{root}: FAILED after retries", flush=True)
        continue
    tmp = path + ".tmp"
    df.to_parquet(tmp)
    os.replace(tmp, path)
    total_cost += cost
    print(f"{root}: saved {len(df)} rows, {df['symbol'].nunique()} symbols", flush=True)
print(f"DONE. total spent this run: ${total_cost:.2f}", flush=True)
