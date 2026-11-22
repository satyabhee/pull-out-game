"""ledger_diag: why do phantom fills at the touch survive the bbo ledger rule?

Replays chunks (one process) with the given replay options, logs every _ledger_touch decision per
(chunk, asset, side, px, block), then for each engine fill at the touch level whose maker is not a real maker
(validate category "margin rejection (unobservable)") reports the ledger's LAST decision at that side/price
before the fill block, and how many blocks earlier it was taken.

usage: python -m analysis.ledger_diag <chunk dirs...> --hl-data DIR <replay options> --out DIR
writes <out>/ledger_log.jsonl (all decisions), <out>/phantom_ledger.csv (one row per phantom touch fill),
and prints the outcome table.
"""
import collections
import csv
import json
import re
import sys
from pathlib import Path

from analysis import validate_chunks as vc
from analysis import validate_replay as vr
from engine_py import replay as rp

LOG: list = []
_CUR = {"chunk": None}

_orig_init = rp.Replay.__init__
_orig_ledger = rp.Replay._ledger_touch


def _init(self, window_dir, *a, **k):
    _CUR["chunk"] = Path(window_dir).name
    _orig_init(self, window_dir, *a, **k)


def _ledger(self, asset, side, lvl, name):
    before = dict(self.touch_reconciled)
    level = self.engine.books[asset].side(side).get(lvl[0]) if lvl else None
    nodes = list(level) if level is not None else []
    out = _orig_ledger(self, asset, side, lvl, name)
    diff = [k[1] for k, v in self.touch_reconciled.items() if v != before.get(k, 0) and k[0] == name]
    blk = getattr(self, "_block", None)
    if lvl:
        LOG.append({"chunk": _CUR["chunk"], "asset": name, "side": side.name, "px": lvl[0],
                    "height": blk.height if blk is not None else None, "outcome": ",".join(sorted(diff)) or "no_level",
                    "n_real": lvl[2], "n_ours": len(nodes), "sz_real": lvl[1],
                    "sz_ours": level.total if level is not None else 0})
    return out


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    out = Path(argv[argv.index("--out") + 1])
    out.mkdir(parents=True, exist_ok=True)
    rp.Replay.__init__, rp.Replay._ledger_touch = _init, _ledger
    vc.main(argv + ["--jobs", "1"])
    with open(out / "ledger_log.jsonl", "w") as f:
        for r in LOG:
            f.write(json.dumps(r) + "\n")
    analyse(out)


def analyse(out: Path):
    csv.field_size_limit(10**9)
    by = collections.defaultdict(list)                       # (chunk, asset, side) -> [(height, px, rec)]
    for line in open(out / "ledger_log.jsonl"):
        r = json.loads(line)
        by[(r["chunk"], r["asset"], r["side"])].append(r)
    txs = next(out.glob("**/txs.csv"))
    rows, tab, gap = [], collections.Counter(), collections.defaultdict(list)
    for r in csv.DictReader(open(txs)):
        if r["warmup"] in ("True", "1") or not r["category"].startswith("margin"):
            continue
        side_txt, _, levels = r["book_before"].partition(": ")
        prices = [float(m) for m in re.findall(r"(?:^|; )([0-9.]+) [0-9.]+ \(", levels)]
        realpx = {float(x) for x in re.findall(r"@([0-9.]+)", r["real"])}
        maker_side = "BUY" if side_txt.startswith("BUY") else "SELL"
        blk = int(r["block"])
        for sz, px, u in re.findall(r"([0-9.]+)@([0-9.]+) (0x[0-9a-f]+)", r["engine"]):
            px = float(px)
            if not prices or px != prices[0]:
                continue
            cands = [x for x in by[(r["chunk"], r["coin"], maker_side)]
                     if x["height"] is not None and x["height"] < blk and _pxf(x["px"], r["coin"]) == px]
            if cands:
                last = cands[-1]
                oc, ago = last["outcome"], blk - last["height"]
                extra = f'ours n={last["n_ours"]} sz={last["sz_ours"]} vs real n={last["n_real"]} sz={last["sz_real"]}'
            else:
                oc, ago, extra = "never at touch before fill", None, ""
            key = oc + (" (real filled same px)" if px in realpx else "")
            tab[oc] += 1
            if ago is not None:
                gap[oc].append(ago)
            rows.append({"chunk": r["chunk"], "hash": r["hash"], "block": blk, "coin": r["coin"], "px": px,
                         "maker": u, "real_same_px": px in realpx, "last_outcome": oc, "blocks_ago": ago,
                         "detail": extra})
    with open(out / "phantom_ledger.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["chunk"])
        w.writeheader()
        w.writerows(rows)
    tot = sum(tab.values())
    print(f"\nphantom fills at the touch: {tot}")
    for oc, n in tab.most_common():
        g = sorted(gap[oc])
        med = g[len(g) // 2] if g else None
        print(f"  {oc:45s} {n:5d} {n / max(1, tot):6.1%}  median blocks before fill {med}")


_TICK = {}


def _pxf(px, coin):
    """ledger px is in engine ticks (int); convert to the decimal price used in txs.csv."""
    if isinstance(px, float):
        return px
    from engine_py.types import ASSETS
    a = next(a for a in ASSETS.values() if a.name == coin)
    return float(a.ticks_to_px(px))


if __name__ == "__main__":
    if sys.argv[1:2] == ["--analyse"]:
        analyse(Path(sys.argv[2]))
    else:
        main()
