"""Does grouping.p or placement time set queue position among resting orders? Pre-specified test, no replay.

Units: consecutive pairs of distinct makers filled at one price in one real tx (fill order = order in the\ntrades message; tids are random, not sequential).
Fresh-level restriction: the price was empty at the end of an earlier block of the same chunk (bid P above the
previous block's best bid, ask P below its best ask), so every order resting there was placed inside the chunk.
Each maker must have exactly one candidate order at P placed since the level was last empty: not cancelled by cloid
before the fill, not a post-only order that would have crossed (previous block's touch), and the maker sent no
int-oid cancel / modify on that asset since then and had no earlier fill at P since then.
Primary: among discordant pairs (time order and p-descending order disagree), share where p order is right, with a
chunk-cluster bootstrap 95% CI. Sanity: among concordant pairs, share both rules get right.
Exploratory: split by placement gap (same block / adjacent block / farther).

usage: python -m analysis.p_priority <chunk dirs or parents...> --hl-data H [--boot 4000]
"""
from __future__ import annotations

import argparse
import bisect
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

from data.oid_facts import _hour_files, _records
from data.parse import _block_files, read_block

ASSET = {0: "BTC", 1: "ETH"}


def chunk_rows(window: Path, hl_data: Path):
    blocks = []                                    # (height, time, txs)
    for h, p in _block_files(window):
        b = read_block(p)
        blocks.append((h, b["blockTime"], b["txs"]))
    if not blocks:
        return []
    t0, t1 = blocks[0][1], blocks[-1][1]
    heights = [h for h, _, _ in blocks]
    times = [t for _, t, _ in blocks]
    rows = []
    for asset, coin in ASSET.items():
        bbo = []
        for path in _hour_files(hl_data, f"bbo_{coin}", t0 - 60_000, t1 + 5_000):
            for r in _records(path, t0 - 60_000, t1 + 5_000):
                d = r["msg"]["data"]
                bid, ask = d["bbo"]
                bbo.append((d["time"], float(bid["px"]) if bid else None, float(ask["px"]) if ask else None))
        bbo.sort()
        bt = [x[0] for x in bbo]

        def touch_after(i):                        # best bid/ask at the end of block index i
            j = bisect.bisect_right(bt, times[i]) - 1
            return (bbo[j][1], bbo[j][2]) if j >= 0 else (None, None)

        trades = defaultdict(list)
        for path in _hour_files(hl_data, f"trades_{coin}", t0, t1 + 5_000):
            for ri, r in enumerate(_records(path, t0, t1 + 5_000)):
                for k, t in enumerate(r["msg"].get("data", [])):
                    if t0 <= t["time"] <= t1 and t.get("hash", "0x0").strip("0x"):
                        trades[t["hash"]].append((str(path), ri, k, t))   # message order, not tid (tids are random)
        # per block: placements, cloid cancels, int-oid cancellers
        place = defaultdict(list)                  # (user, buy, px) -> [(bi, tx, item, p, tif, cloid)]
        cloid_cancel = defaultdict(list)           # (user, cloid) -> [bi]
        int_cancel = defaultdict(list)             # user -> [bi]
        tx_block = {}
        for bi, (h, _, txs) in enumerate(blocks):
            for ti, tx in enumerate(txs):
                if tx.get("hash"):
                    tx_block[tx["hash"]] = (bi, ti)
                if tx["error"] is not None:
                    continue
                a, u = tx["action"], tx["user"].lower()
                kind = a.get("type")
                if kind == "order":
                    g = a.get("grouping")
                    pr = g.get("p") if isinstance(g, dict) else None
                    for k, o in enumerate(a.get("orders", [])):
                        if o.get("a") == asset and "limit" in (o.get("t") or {}):
                            place[(u, bool(o["b"]), float(o["p"]))].append(
                                (bi, ti, k, pr, o["t"]["limit"].get("tif"), o.get("c")))
                elif kind == "cancelByCloid":
                    for c in a.get("cancels", []):
                        if c.get("asset") == asset:
                            cloid_cancel[(u, c.get("cloid"))].append(bi)
                elif kind == "cancel":
                    if any(c.get("a") == asset for c in a.get("cancels", [])):
                        int_cancel[u].append(bi)
                elif kind in ("modify", "batchModify"):
                    mods = [a] if kind == "modify" else a.get("modifies", [])
                    if any((m.get("order") or {}).get("a") == asset for m in mods):
                        int_cancel[u].append(bi)
        earlier_fill = defaultdict(list)           # (user, px) -> [bi of fills]
        for hsh, ts in sorted(trades.items(), key=lambda kv: tx_block.get(kv[0], (1 << 60, 0))):
            if hsh not in tx_block:
                continue
            fb, _ = tx_block[hsh]
            ts = [t for *_, t in sorted(ts, key=lambda x: x[:3])]
            seq = []
            for t in ts:
                maker = (t["users"][0] if t["side"] == "A" else t["users"][1]).lower()
                buy = t["side"] == "A"             # taker sold: makers are bids
                seq.append((maker, buy, float(t["px"])))
            for (m1, b1, p1), (m2, b2, p2) in zip(seq, seq[1:]):
                if m1 == m2 or p1 != p2 or b1 != b2:
                    continue
                px, buy = p1, b1
                h0 = None                          # level empty at end of block h0-1
                for bi in range(fb, 0, -1):
                    bid, ask = touch_after(bi - 1)
                    empty = (bid is not None and px > bid) if buy else (ask is not None and px < ask)
                    if empty:
                        h0 = bi
                        break
                if h0 is None:
                    rows.append({"skip": "level never empty in chunk"})
                    continue
                cands = {}
                ok = True
                for m in (m1, m2):
                    if any(h0 <= x <= fb for x in int_cancel.get(m, [])):
                        ok = False
                        break
                    if any(h0 <= x < fb for x in earlier_fill.get((m, px), [])):
                        ok = False
                        break
                    cs = []
                    for (bi, ti, k, pr, tif, cl) in place.get((m, buy, px), []):
                        if not h0 <= bi <= fb:
                            continue
                        if bi == fb and tif != "Alo":
                            continue
                        if cl and any(bi <= x <= fb for x in cloid_cancel.get((m, cl), [])):
                            continue
                        if tif == "Alo":
                            bid, ask = touch_after(bi - 1) if bi > 0 else (None, None)
                            if (buy and ask is not None and px >= ask) or (not buy and bid is not None and px <= bid):
                                continue           # would have crossed: rejected
                        cs.append((bi, ti, k, pr))
                    if len(cs) != 1:
                        ok = False
                        break
                    cands[m] = cs[0]
                if not ok:
                    rows.append({"skip": "ambiguous or disqualified maker"})
                    continue
                a, b = cands[m1], cands[m2]
                if a[3] is None or b[3] is None:
                    rows.append({"skip": "p missing"})
                    continue
                time_right = a[:3] < b[:3]
                if a[3] == b[3]:
                    rows.append({"chunk": window.name, "tie": True, "time_right": time_right})
                    continue
                p_right = a[3] > b[3]
                gap = abs(blocks[a[0]][0] - blocks[b[0]][0])
                rows.append({"chunk": window.name, "tie": False, "time_right": time_right, "p_right": p_right,
                             "ex": {"hash": hsh, "asset": asset, "px": px, "buy": buy, "first": m1, "second": m2,
                                    "first_order": [blocks[a[0]][0], a[1], a[2], a[3]],
                                    "second_order": [blocks[b[0]][0], b[1], b[2], b[3]],
                                    "fill_block": blocks[fb][0], "h0": blocks[h0][0]},
                             "gap": "same block" if gap == 0 else "adjacent block" if gap == 1 else "2+ blocks",
                             "ms": abs(times[a[0]] - times[b[0]])})
            for maker, buy, px in seq:
                earlier_fill[(maker, px)].append(fb)
    return rows


def boot_share(rows, key, n, seed=11):
    by = defaultdict(list)
    for r in rows:
        by[r["chunk"]].append(r[key])
    ks = sorted(by)
    rnd = random.Random(seed)
    out = []
    for _ in range(n):
        s = [x for c in (rnd.choice(ks) for _ in ks) for x in by[c]]
        if s:
            out.append(sum(s) / len(s))
    out.sort()
    return out[int(0.025 * len(out))], out[int(0.975 * len(out)) - 1]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("dirs", nargs="+")
    ap.add_argument("--hl-data", required=True)
    ap.add_argument("--boot", type=int, default=4000)
    ap.add_argument("--examples", help="write discordant pair details (json) here")
    a = ap.parse_args(argv)
    wins = []
    for d in map(Path, a.dirs):
        wins += [d] if (d / "window.json").exists() else sorted(p for p in d.iterdir() if (p / "window.json").exists())
    rows = []
    for w in wins:
        try:
            rows += chunk_rows(w, Path(a.hl_data))
        except Exception as e:                     # noqa: BLE001
            print(f"{w.name}: {e}", file=sys.stderr)
    if a.examples:
        import json
        Path(a.examples).write_text(json.dumps([r for r in rows if r.get("ex")], default=str))
    skips = Counter(r["skip"] for r in rows if "skip" in r)
    use = [r for r in rows if "chunk" in r]
    ties = [r for r in use if r["tie"]]
    dis = [r for r in use if not r["tie"] and r["time_right"] != r["p_right"]]
    con = [r for r in use if not r["tie"] and r["time_right"] == r["p_right"]]
    print(f"chunks {len(wins)}; pairs considered {len(rows)}; skipped {dict(skips)}")
    print(f"usable pairs {len(use)} from {len({r['chunk'] for r in use})} chunks: "
          f"discordant {len(dis)}, concordant {len(con)}, equal p {len(ties)}")
    if con:
        s = sum(r["time_right"] for r in con) / len(con)
        print(f"SANITY concordant: both rules right {s:.3f} (n={len(con)}) CI {boot_share(con, 'time_right', a.boot)}")
    if ties:
        s = sum(r["time_right"] for r in ties) / len(ties)
        print(f"equal p: time order right {s:.3f} (n={len(ties)}) CI {boot_share(ties, 'time_right', a.boot)}")
    if dis:
        s = sum(r["p_right"] for r in dis) / len(dis)
        print(f"PRIMARY discordant: p order right {s:.3f} (n={len(dis)}, chunks {len({r['chunk'] for r in dis})}) "
              f"CI {boot_share(dis, 'p_right', a.boot)}")
        print("exploratory, discordant by placement time gap:")
        ms_split(dis)
        print("exploratory, discordant by placement gap:")
        for g in ("same block", "adjacent block", "2+ blocks"):
            sub = [r for r in dis if r["gap"] == g]
            if sub:
                print(f"  {g:15s} n={len(sub):4d} p right {sum(r['p_right'] for r in sub) / len(sub):.3f} "
                      f"CI {boot_share(sub, 'p_right', a.boot) if len({r['chunk'] for r in sub}) > 1 else 'n/a'}")


def ms_split(dis):  # MS_SPLIT exploratory: p-rule accuracy by placement time gap
    for lo, hi in ((0, 100), (100, 500), (500, 2000), (2000, 10_000), (10_000, 10**9)):
        sub = [r for r in dis if lo <= r["ms"] < hi]
        if sub:
            print(f"  {lo:>6}-{hi if hi < 10**9 else 'inf':>6} ms n={len(sub):4d} p right {sum(r['p_right'] for r in sub) / len(sub):.3f}")


if __name__ == "__main__":
    main()
