"""The match loop and order-type semantics for one market (specs/01-book.md sections 2-6).

- Price-time priority; every fill at the resting maker's price.
- GTC rests its remainder (after an explicit non-crossing check); IOC cancels it; ALO is rejected
  whole if it would touch or cross the opposite best, otherwise rests.
- Self-trade prevention: cancel-taker. Matching stops at the first self-owned maker; no skipping.
- Order id and arrival sequence are assigned only on admission; a rejected ALO gets neither.
- Counters never wrap: exhaustion raises EngineError and the replay discards the whole block.
"""
from __future__ import annotations

from .book import Book
from .types import (U64_MAX, U128_MAX, Cancel, EngineError, Fill, MarketConfig, Order, Place,
                    RejectReason, Side, SubmitResult, Tif)


class MarketEngine:
    def __init__(self, config: MarketConfig) -> None:
        self.config = config
        self.book = Book()
        self.last_order_id = 0          # last assigned value of each counter (0 = none yet)
        self.last_arrival_sequence = 0
        self.last_fill_sequence = 0
        self.block_height = 0
        self._index_in_block = 0

    # ---- counters ----------------------------------------------------------------------------
    def _next_order_id(self) -> int:
        if self.last_order_id >= U128_MAX:
            raise EngineError("order id counter exhausted")
        self.last_order_id += 1
        return self.last_order_id

    def _next_arrival(self) -> int:
        if self.last_arrival_sequence >= U64_MAX:
            raise EngineError("arrival sequence exhausted")
        self.last_arrival_sequence += 1
        return self.last_arrival_sequence

    def _next_fill(self) -> int:
        if self.last_fill_sequence >= U64_MAX:
            raise EngineError("fill sequence exhausted")
        self.last_fill_sequence += 1
        return self.last_fill_sequence

    def begin_block(self, height: int) -> None:
        self.block_height = height
        self._index_in_block = 0

    # ---- admission ---------------------------------------------------------------------------
    def _admission_reject(self, p: Place) -> RejectReason | None:
        c = self.config
        if p.size_lots <= 0:
            return RejectReason.SIZE_ZERO
        if not (c.min_price_tick <= p.price_tick <= c.max_price_tick):
            return RejectReason.PRICE_OUT_OF_RANGE
        if p.size_lots > c.max_order_lots:
            return RejectReason.SIZE_TOO_LARGE
        if c.notional_atoms(p.price_tick, p.size_lots) > c.max_order_notional:
            return RejectReason.NOTIONAL_TOO_LARGE
        return None

    def _would_take(self, side: Side, price: int) -> bool:
        if side == Side.BUY:
            ba = self.book.best_ask()
            return ba is not None and price >= ba
        bb = self.book.best_bid()
        return bb is not None and price <= bb

    def _new_order(self, p: Place) -> Order:
        return Order(self.config.market_id, self.config.config_version, self._next_order_id(), p.owner_id,
                     p.side, p.price_tick, p.size_lots, p.size_lots, p.tif, self._next_arrival(),
                     p.cloid, p.exchange_oid)

    # ---- submit ------------------------------------------------------------------------------
    def submit(self, p: Place) -> SubmitResult:
        reason = self._admission_reject(p)
        if reason is not None:
            return SubmitResult(False, reason)
        if p.tif == Tif.ALO:
            if self._would_take(p.side, p.price_tick):
                return SubmitResult(False, RejectReason.POST_ONLY_WOULD_TAKE)
            o = self._new_order(p)
            self.book.add(o)
            self.check_invariants()
            return SubmitResult(True, order_id=o.order_id, fills=[], rested_lots=o.size_lots)

        taker = self._new_order(p)                     # GTC / IOC: id + sequence before matching
        fills, self_trade = self._match(taker)
        res = SubmitResult(True, order_id=taker.order_id, fills=fills, self_trade=self_trade)
        if taker.remaining_lots:
            if self_trade or taker.tif == Tif.IOC:
                res.cancelled_lots = taker.remaining_lots
            else:                                      # GTC remainder: explicit non-crossing check
                if self._would_take(taker.side, taker.price_tick):
                    raise EngineError(f"GTC remainder of order {taker.order_id} would still cross")
                self.book.add(taker)
                res.rested_lots = taker.remaining_lots
        self.check_invariants()
        return res

    def _match(self, taker: Order) -> tuple[list[Fill], bool]:
        maker_side = Side.SELL if taker.side == Side.BUY else Side.BUY
        fills: list[Fill] = []
        while taker.remaining_lots:
            best = self.book.best_level(maker_side)
            if best is None:
                break
            price, level = best
            if (taker.side == Side.BUY and price > taker.price_tick) or \
               (taker.side == Side.SELL and price < taker.price_tick):
                break
            maker = level[next(iter(level))]           # FIFO front
            if maker.owner_id == taker.owner_id:       # self-trade: cancel taker remainder, stop
                return fills, True
            qty = min(taker.remaining_lots, maker.remaining_lots)
            fills.append(Fill(self.config.market_id, self.config.config_version, maker.order_id,
                              taker.order_id, maker.owner_id, taker.owner_id, taker.side, price, qty,
                              self._next_fill(), self.block_height, self._index_in_block))
            self._index_in_block += 1
            taker.remaining_lots -= qty
            maker.remaining_lots -= qty
            if maker.remaining_lots == 0:
                self.book.remove(maker.order_id)
        return fills, False

    # ---- cancel ------------------------------------------------------------------------------
    def cancel(self, c: Cancel) -> bool:
        """Remove a resting order. False if it is not resting or belongs to someone else."""
        o = self.book.orders.get(c.order_id)
        if o is None or o.owner_id != c.owner_id:
            return False
        self.book.remove(c.order_id)
        self.check_invariants()
        return True

    # ---- invariants --------------------------------------------------------------------------
    def check_invariants(self) -> None:
        if self.book.crossed():
            raise EngineError(f"{self.config.market_id}: crossed book "
                              f"bid {self.book.best_bid()} >= ask {self.book.best_ask()}")

    def check_full(self) -> None:
        """Deep check (end of block, snapshots): crossed book, quantities, ids, sequences."""
        self.check_invariants()
        seen_seq = set()
        for side in (Side.BUY, Side.SELL):
            for price, level in self.book.levels(side):
                last = 0
                for oid, o in level.items():
                    if oid != o.order_id or o.side != side or o.price_tick != price:
                        raise EngineError(f"order {oid} filed under the wrong level")
                    if not (0 < o.remaining_lots <= o.size_lots):
                        raise EngineError(f"order {oid} has invalid remaining_lots")
                    if o.arrival_sequence <= last or o.arrival_sequence in seen_seq:
                        raise EngineError(f"arrival sequence out of order at order {oid}")
                    if o.order_id > self.last_order_id or o.arrival_sequence > self.last_arrival_sequence:
                        raise EngineError(f"order {oid} is ahead of the counters")
                    last = o.arrival_sequence
                    seen_seq.add(last)
