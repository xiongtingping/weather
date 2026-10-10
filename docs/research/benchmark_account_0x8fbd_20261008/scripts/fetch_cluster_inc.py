"""Round 26: the cluster's fills after the research window (all eight wallets,
taker and maker), from each wallet's newest fill already in hand to now.

Public data API only (read): /trades?user=&takerOnly=false|true, newest first,
offset pages of 500 under the 10,000-offset cap, then ``end=`` windows.
Writes <bench>/cluster_inc.json: {wallet: {"fills": [...], "taker": [...]}}.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

B = Path(sys.argv[1])
WALLETS = [
    "0x8fbd7cf5f806f563080864694415829f7229a959", "0x6fdd2bda5c031d551f588f176535f061a29a0b07",
    "0x5bae7577f470244ce8e678d20693eee2b8ffed25", "0x30200b893530920f253846199a153e6ac725df2b",
    "0x0e0e8174e213ff2c2ccb30c5652a7b7d77c72d75", "0x7076cb961e87bd22199445d02fe22973510cd0d9",
    "0x7d640ebd20cf136bee351100438bba44ca2e07f8", "0xf4d89f7e8c2fc8d2303e0f6b63cdff37fe304c91",
]
KEYS = ("side", "asset", "conditionId", "outcome", "price", "size", "timestamp", "transactionHash", "title", "eventSlug",
        "proxyWallet")


def get(url):
    for i in range(8):
        r = subprocess.run(["curl", "-s", "-m", "40", url], capture_output=True, text=True)
        try:
            j = json.loads(r.stdout)
        except Exception:
            time.sleep(2 + i)
            continue
        if isinstance(j, dict) and "Too Many" in str(j.get("error")):
            time.sleep(8 + 8 * i)
            continue
        return j
    return None


def since_for(wallet):
    """The newest fill already in hand for this wallet (research window)."""
    if wallet.startswith("0x8fbd"):
        w = json.load(open(B / "window.json"))
        return max(t["timestamp"] for t in w["fills"])
    c = json.load(open(B / "cluster.json"))
    for full, d in c.items():
        if full.lower() == wallet:
            return max(t["timestamp"] for t in d["fills"])
    return 1791400000


def pull(wallet, taker_only, since):
    rows, end = [], None
    while True:
        off, got = 0, []
        while off <= 9500:
            url = (f"https://data-api.polymarket.com/trades?user={wallet}&takerOnly={'true' if taker_only else 'false'}"
                   f"&limit=500&offset={off}" + (f"&end={end}" if end else ""))
            page = get(url)
            if not isinstance(page, list):
                break
            got += page
            if len(page) < 500 or min(t["timestamp"] for t in page) < since:
                break
            off += 500
            time.sleep(0.4)
        rows += got
        if not got:
            break
        oldest = min(t["timestamp"] for t in got)
        if oldest < since or len(got) < 500:
            break
        if end is not None and oldest >= end:
            break
        end = oldest
        time.sleep(0.4)
    seen, out = set(), []
    for t in rows:
        if t["timestamp"] < since:
            continue
        k = (t["transactionHash"], t["asset"], t["side"], str(t["price"]), str(t["size"]), t["timestamp"])
        if k in seen:
            continue
        seen.add(k)
        out.append({k2: t.get(k2) for k2 in KEYS})
    return out


out = {}
t0 = time.time()
for wallet in WALLETS:
    since = since_for(wallet) - 120
    fills = pull(wallet, False, since)
    taker = pull(wallet, True, since)
    out[wallet] = {"since": since, "fills": fills, "taker": taker}
    print(wallet[:10], "since", since, "fills", len(fills), "taker", len(taker),
          "newest", max((t["timestamp"] for t in fills), default=None), round(time.time() - t0), "s", file=sys.stderr)
json.dump(out, open(B / "cluster_inc.json", "w"))
