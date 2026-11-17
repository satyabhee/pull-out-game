"""Apply blocks in order, emit fills and a state hash per block.

  uv run python -m engine_py.replay BLOCK.json.gz [MORE.json.gz | DIR ...] [--policy first_come] [--quiet]

Each block is applied to a copy of the engine state and committed only if the whole block succeeds
(all-or-nothing, 01-book sections 2 and 6). This layer also:
- converts decimal strings to ticks/lots per market; a bad value is a counted INVALID_UNITS rejection;
- resolves cancel/modify targets before the engine: (owner, cloid), then (owner, exchange_oid), else
  unresolved and counted (01-book "Exchange identities"). exchange_oid estimates from anchors are
  not wired in yet (G1), so oid-targeted cancels only resolve when an order carries a known oid;
- records each accepted taker's tx hash, so replayed fills can be linked to recorded trades
  (a real trade's hash is its taker tx hash; see report/G0_REPORT.md section 4).
"""
from __future__ import annotations

import argparse
import copy
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from data.parse import RawCancel, RawModify, RawPlace, load_block, parse_block
from .matching import MarketEngine
from .ordering import POLICIES
from .state import state_hash
from .types import Cancel, Fill, MarketConfig, Place, RejectReason, hyperliquid_perp_config


def default_markets() -> dict[int, MarketConfig]:
    """Asset number -> config. szDecimals from info {"type":"meta"}: BTC 5, ETH 4."""
    return {0: hyperliquid_perp_config("BTC", 5), 1: hyperliquid_perp_config("ETH", 4)}


@dataclass
class BlockResult:
    height: int
    block_time: int
    fills: list[Fill]
    stats: Counter
    state_hash: str
    taker_tx: dict[tuple[str, int], str] = field(default_factory=dict)   # (market, taker order id) -> tx hash


class Replay:
    def __init__(self, markets: dict[int, MarketConfig] | None = None, policy: str = "project") -> None:
        self.markets = markets or default_markets()
        self.engines = {c.market_id: MarketEngine(c) for c in self.markets.values()}
        self.policy = POLICIES[policy]
        self.totals: Counter = Counter()

    # ---- helpers -----------------------------------------------------------------------------
    @staticmethod
    def _resolve(eng: MarketEngine, owner: str, oid: int | None, cloid: str | None, stats: Counter, kind: str):
        if cloid is not None:
            ids, how = eng.book.find_by_cloid(owner, cloid), "cloid"
        else:
            ids, how = eng.book.find_by_exchange_oid(owner, oid), "oid"
        if len(ids) == 1:
            stats[f"{kind}_resolved_by_{how}"] += 1
            return ids[0]
        stats[f"{kind}_unresolved_{how}" + ("_ambiguous" if ids else "")] += 1
        return None

    @staticmethod
    def _place(eng: MarketEngine, a: RawPlace, stats: Counter, fills: list, taker_tx: dict) -> None:
        cfg = eng.config
        px, sz = cfg.to_ticks(a.px), cfg.to_lots(a.sz)
        if px is None or sz is None:
            stats[f"rejected:{RejectReason.INVALID_UNITS.value}"] += 1
            return
        r = eng.submit(Place(a.owner, a.side, px, sz, a.tif, a.cloid, None, a.tx_index, a.action_index))
        if not r.accepted:
            stats[f"rejected:{r.reject_reason.value}"] += 1
            return
        stats[f"accepted:{a.tif.name}"] += 1
        if r.fills:
            fills.extend(r.fills)
            taker_tx[(cfg.market_id, r.order_id)] = a.tx_hash
        if r.self_trade:
            stats["self_trade_cancel"] += 1
        if r.cancelled_lots and not r.self_trade:
            stats["ioc_remainder_cancelled"] += 1

    # ---- one block ---------------------------------------------------------------------------
    def apply_block(self, block: dict) -> BlockResult:
        asset_names = {a: c.market_id for a, c in self.markets.items()}
        pb = parse_block(block, asset_names)
        staged = copy.deepcopy(self.engines)
        stats: Counter = Counter({f"skipped:{k}": v for k, v in pb.skipped.items()})
        fills: list[Fill] = []
        taker_tx: dict = {}
        for e in staged.values():
            e.begin_block(pb.height)
        for a in self.policy(pb.actions):
            eng = staged[asset_names[a.asset]]
            if isinstance(a, RawPlace):
                self._place(eng, a, stats, fills, taker_tx)
            elif isinstance(a, RawCancel):
                target = self._resolve(eng, a.owner, a.oid, a.cloid, stats, "cancel")
                if target is not None:
                    eng.cancel(Cancel(a.owner, target))
            elif isinstance(a, RawModify):
                target = self._resolve(eng, a.owner, a.target_oid, a.target_cloid, stats, "modify")
                if target is None:
                    continue                              # unresolved: no replacement is placed
                eng.cancel(Cancel(a.owner, target))
                self._place(eng, a.new, stats, fills, taker_tx)
        for e in staged.values():
            e.check_full()
        self.engines = staged                             # commit only after the whole block succeeded
        stats["fills"] = len(fills)
        self.totals.update(stats)
        return BlockResult(pb.height, pb.block_time, fills, stats, state_hash(self.engines), taker_tx)


def _files(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for p in map(Path, paths):
        out += sorted(p.glob("*.json.gz"), key=lambda f: int(f.name.split(".")[0])) if p.is_dir() else [p]
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("paths", nargs="+", help="block files (.json.gz) or folders of them")
    ap.add_argument("--policy", default="project", choices=sorted(POLICIES))
    ap.add_argument("--quiet", action="store_true", help="only print the summary")
    args = ap.parse_args(argv)
    rp, t0, n, last = Replay(policy=args.policy), time.time(), 0, None
    for f in _files(args.paths):
        b = load_block(f)
        if last is not None and b["height"] != last + 1:
            print(f"note: gap before height {b['height']} (previous {last}); book carries over", file=sys.stderr)
        r = rp.apply_block(b)
        last, n = r.height, n + 1
        if not args.quiet:
            s = r.stats
            acc = sum(v for k, v in s.items() if k.startswith("accepted:"))
            rej = sum(v for k, v in s.items() if k.startswith("rejected:"))
            unr = sum(v for k, v in s.items() if "_unresolved_" in k)
            print(f"height {r.height}  accepted {acc}  rejected {rej}  fills {len(r.fills)}  "
                  f"unresolved targets {unr}  hash {r.state_hash[:16]}")
    print(f"\n{n} blocks in {time.time() - t0:.1f}s, policy={args.policy}")
    for k, v in sorted(rp.totals.items()):
        print(f"  {k:<40} {v}")
    for mid, e in rp.engines.items():
        print(f"  book {mid}: {len(e.book)} resting orders, best bid {e.book.best_bid()} ask {e.book.best_ask()} (ticks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
