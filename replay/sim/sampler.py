"""M1 hidden-state sampler: who trades, what they send, and what the chain hides (MAHITO_PLAN M1 sampler.py).

Agents (hand-set defaults in replay/sim/config.py until M3's profiles exist):
- market makers: quote `mm_levels` post-only levels per side around the mid with priority bids, reusing a few
  sizes; each block they may cancel every quote (mostly by cloid, some by oid) and place new ones (more often
  when the mid moves). Cancels and new quotes are separate txs, so they race takers inside a block;
- bots: IOC takers that cross the book; `bot_broke` of them have no free margin (the chain rejects them);
- retail: GTC / IOC / post-only orders near the touch, some reduce-only; `retail_broke` have no margin.
Hidden events: pre-window resting orders (owners unknown to the replay), batch cuts inside blocks, invisible
events that burn oids. Everything comes from one seeded random.Random, so a seed reproduces the chunk exactly.
"""
from __future__ import annotations

import hashlib
import random
from decimal import Decimal

from replay.sim.config import SimConfig
from replay.sim.rules_engine import Account, CancelItem, Chain, OrderItem, Tx


def address(seed: int, role: str, i: int) -> str:
    return "0x" + hashlib.sha256(f"sim:{seed}:{role}:{i}".encode()).hexdigest()[:40]


class Sampler:
    def __init__(self, cfg: SimConfig, rng: random.Random):
        self.cfg, self.rng = cfg, rng
        s = cfg.seed
        self.mms = [address(s, "mm", i) for i in range(cfg.n_mm)]
        self.bots = [address(s, "bot", i) for i in range(cfg.n_bots)]
        self.retail = [address(s, "retail", i) for i in range(cfg.n_retail)]
        self.mid = {a: m[0] for a, m in cfg.markets.items()}
        self.mm_sizes = {u: [cfg.markets[0][2] * rng.randint(1, 4) for _ in range(cfg.mm_sizes)] for u in self.mms}
        self.mm_p = {u: rng.randint(0, cfg.mm_p_max) for u in self.mms}
        self.mm_orders: dict[str, list[tuple[int, str]]] = {u: [] for u in self.mms}   # (asset, cloid) quoted
        self.cloid_n = 0
        broke = set(self.bots[:cfg.bot_broke]) | set(self.retail[:cfg.retail_broke])
        self.accounts = {}
        for u in self.mms + self.bots + self.retail:
            pos = {a: rng.choice([-1, 0, 0, 1]) * cfg.markets[a][2] for a in cfg.markets} if u in self.retail else {}
            self.accounts[u] = Account(free_margin=Decimal(0) if u in broke else Decimal(10**7), position=pos)

    # ------------------------------------------------------------ helpers
    def _cloid(self) -> str:
        self.cloid_n += 1
        return "0x" + hashlib.sha256(f"cl:{self.cfg.seed}:{self.cloid_n}".encode()).hexdigest()[:32]

    def _quotes(self, u: str, asset: int):
        """(side, px, sz) for one MM's ladder around the current mid."""
        step = self.cfg.markets[asset][1]
        mid = self.mid[asset]
        out = []
        for k in range(self.cfg.mm_levels):
            sz = self.rng.choice(self.mm_sizes[u])
            out.append(("B", mid - step * (k + 1), sz))
            out.append(("A", mid + step * (k + 1), sz))
        return out

    def burn(self) -> int:
        """Oids burned by invisible events before one placed item (chain hook)."""
        return self.rng.randint(1, 3) if self.rng.random() < self.cfg.burn_prob else 0

    # ------------------------------------------------------------ pre-window
    def seed_book(self, chain: Chain, t0: int) -> None:
        """Resting orders placed before the chunk: the replay sees them only as (px, sz, n) levels."""
        if self.cfg.pre_window_ms <= 0:
            return
        for u in self.mms:
            for asset in self.cfg.markets:
                for side, px, sz in self._quotes(u, asset):
                    c = self._cloid()
                    chain.seed(u, asset, side, px, sz, t_ms=t0 - self.rng.randint(1, self.cfg.pre_window_ms),
                               p=self.mm_p[u], cloid=c)
                    self.mm_orders[u].append((asset, c))
        for u in self.retail:                                   # some deeper anonymous liquidity
            if self.rng.random() < 0.5:
                asset = self.rng.choice(list(self.cfg.markets))
                step, size = self.cfg.markets[asset][1:]
                side = self.rng.choice("BA")
                px = self.mid[asset] + (-1 if side == "B" else 1) * step * self.rng.randint(2, 8)
                chain.seed(u, asset, side, px, size * self.rng.randint(1, 3), t_ms=t0 - self.cfg.pre_window_ms)

    # ------------------------------------------------------------ one block
    def block_txs(self, chain: Chain, height: int, time_ms: int) -> list[Tx]:
        cfg, rng = self.cfg, self.rng
        moved = set()
        for a in self.mid:
            if rng.random() < cfg.mid_step_prob:
                self.mid[a] += rng.choice((-1, 1)) * cfg.markets[a][1]
                moved.add(a)
        txs: list[Tx] = []
        for u in self.mms:
            for asset in cfg.markets:
                p_requote = cfg.mm_requote_on_move if asset in moved else cfg.mm_requote_prob
                if rng.random() >= p_requote:
                    continue
                mine = [(a, c) for a, c in self.mm_orders[u] if a == asset]
                if mine:
                    by_cloid, by_oid = [], []
                    for a, c in mine:
                        oid = chain.open_cloid.get((u, a, c))
                        if oid is not None and rng.random() >= cfg.mm_cloid_share:
                            by_oid.append(CancelItem(a, oid=oid))
                        else:
                            by_cloid.append(CancelItem(a, cloid=c))
                    if by_cloid:
                        txs.append(Tx(0, u, "cancelByCloid", by_cloid))
                    if by_oid:
                        txs.append(Tx(0, u, "cancel", by_oid))
                    self.mm_orders[u] = [(a, c) for a, c in self.mm_orders[u] if a != asset]
                items = []
                for side, px, sz in self._quotes(u, asset):
                    c = self._cloid()
                    items.append(OrderItem(asset, side, px, sz, "Alo", cloid=c))
                    self.mm_orders[u].append((asset, c))
                txs.append(Tx(0, u, "order", items, p=self.mm_p[u]))
        for u in self.bots:
            if rng.random() < cfg.bot_ioc_prob:
                txs.append(self._taker(u, chain, "Ioc"))
        for u in self.retail:
            if rng.random() < cfg.retail_prob:
                tif = rng.choices(list(cfg.retail_tif), weights=list(cfg.retail_tif.values()))[0]
                txs.append(self._retail(u, chain, tif))
        rng.shuffle(txs)                                        # proposer (arrival) order
        for i, tx in enumerate(txs):
            tx.index = i
        return txs

    def _taker(self, u: str, chain: Chain, tif: str) -> Tx:
        asset = self.rng.choice(list(self.cfg.markets))
        step, size = self.cfg.markets[asset][1:]
        side = self.rng.choice("BA")
        opp = chain.best(asset, "A" if side == "B" else "B")
        ref = opp if opp is not None else self.mid[asset]
        px = ref + (step if side == "B" else -step) * self.rng.randint(0, 2)
        sz = size * self.rng.randint(1, self.cfg.taker_sweep_max)
        return Tx(0, u, "order", [OrderItem(asset, side, px, sz, tif)])

    def _retail(self, u: str, chain: Chain, tif: str) -> Tx:
        if tif != "Alo":
            tx = self._taker(u, chain, tif)
        else:
            asset = self.rng.choice(list(self.cfg.markets))
            step, size = self.cfg.markets[asset][1:]
            side = self.rng.choice("BA")
            own = chain.best(asset, side)
            ref = own if own is not None else self.mid[asset] + (-step if side == "B" else step)
            px = ref + (-step if side == "B" else step) * self.rng.randint(0, 2)
            tx = Tx(0, u, "order", [OrderItem(asset, side, px, size * self.rng.randint(1, 2), "Alo")])
        it = tx.items[0]
        pos = self.accounts[u].position.get(it.asset, 0)
        if pos and self.rng.random() < self.cfg.reduce_only_share:      # an order that closes the position
            it.side = "A" if pos > 0 else "B"
            it.reduce_only = True
        return tx

    def batches(self, txs: list[Tx]) -> list[list[Tx]]:
        """Consensus batches: runs of consecutive txs (one batch unless a cut is drawn)."""
        if len(txs) > 1 and self.rng.random() < self.cfg.batch_cut_prob:
            k = self.rng.randint(1, len(txs) - 1)
            return [txs[:k], txs[k:]]
        return [txs]
