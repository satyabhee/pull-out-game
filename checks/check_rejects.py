"""
check_rejects.py: what do rejections look like in the free explorer blocks?
Run on the server (reads blocks already downloaded, no network):
  cd ~/hl && .venv/bin/python check_rejects.py

Answers G0's question: can we tell rejected orders from accepted ones?
"""
import collections, glob, gzip, json

files = sorted(glob.glob("blocks/*/*.json.gz"))[:2000]
txs_total, err_txs = 0, 0
err_by_type, err_samples = collections.Counter(), collections.defaultdict(list)
tx_keys, action_keys = collections.Counter(), collections.defaultdict(collections.Counter)
multi_order = 0

for f in files:
    with gzip.open(f, "rt") as fh:
        blk = json.load(fh)
    for t in blk.get("blockDetails", blk).get("txs", []):
        txs_total += 1
        tx_keys.update(t.keys())
        a = t.get("action", {})
        typ = a.get("type")
        action_keys[typ].update(a.keys())
        if typ == "order" and len(a.get("orders", [])) > 1:
            multi_order += 1
        if t.get("error"):
            err_txs += 1
            err_by_type[typ] += 1
            if len(err_samples[typ]) < 3:
                err_samples[typ].append(str(t["error"])[:200])

print(f"scanned {len(files)} blocks, {txs_total} txs, {err_txs} txs with an error "
      f"({100 * err_txs / max(txs_total, 1):.2f}%)")
print("fields on every tx:", dict(tx_keys))
print("order actions carrying more than one order (batches):", multi_order)
print("\nerrors by action type:", dict(err_by_type))
for typ, ss in err_samples.items():
    print(f"\n  {typ}:")
    for s in ss:
        print("    ", s)
print("\nfields per action type:")
for typ, ks in action_keys.items():
    print(f"  {typ}: {dict(ks)}")
