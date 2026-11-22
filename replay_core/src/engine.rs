//! The matching engine (port of `engine_py/matching.py`, function by function; the Python file is the reference).
//!
//! Chain rules it implements:
//! - price-time priority; every fill is at the resting maker's price; the best level is re-read every iteration;
//! - GTC: match, rest the remainder. IOC: match, drop the remainder (no fill is not an error).
//!   ALO (post-only): rejected whole if it would cross (equal price included), otherwise it rests;
//! - self-trade: `stp = cancel_taker` (stop, keep the maker, cancel the taker's remainder; the engine default) or
//!   `expire_maker` (cancel the resting order, keep matching; MAHITO_PLAN §1 says this is the chain's rule);
//! - priority bids (`p_priority`): "docs" = Hyperliquid's priority-fee rule, a new ALO goes ahead of in-window
//!   ALOs placed less than 400 ms earlier with a lower p; older orders keep their place (other modes: see
//!   `engine_py/matching.py::Engine.__init__`);
//! - cancels by cloid resolve only when exactly one resting order has that (user, asset, cloid); never guess;
//! - scheduleCancel: at end of block, users whose time has come lose all their orders (deterministic order);
//! - outside fills (liquidations, TWAP, triggers): forced fills against the named maker, then placeholders;
//! - rule 8 snapshot reconcile / refill / clear, and evictions on outside evidence;
//! - a crossed book after any action is a bug: it raises, it is never repaired.
//!
//! Copy-on-write: every map here is persistent (`im`) and the books are `book::Book`, so `Engine::clone()` is cheap
//! and a copy pays only for what it later changes (MAHITO_PLAN M2).
//!
//! What stays in Python (`replay/rust_engine.py`): dispatching an action to these functions, building Fill /
//! Reject objects, calling the oid resolver (a Python object), and the `check_every_action` option.

use crate::book::{Book, BookError, OrderData, Side};
use im::{HashMap as PMap, OrdMap, Vector};
use std::sync::Arc;

pub const PLACEHOLDER: &str = "placeholder";
pub const DOCS_T_MS: i64 = 400;
pub const TIF_GTC: u8 = 0;
pub const TIF_IOC: u8 = 1;
pub const TIF_ALO: u8 = 2;

/// Reject reasons the engine itself produces (codes shared with the Python glue).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RejectCode {
    AloWouldCross,
    SelfTrade,
    UnknownOrder,
    NoMaker,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum EngineError {
    Book(String),
    Crossed(String),
}

impl From<BookError> for EngineError {
    fn from(e: BookError) -> Self {
        EngineError::Book(e.0)
    }
}

type R<T> = Result<T, EngineError>;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PMode {
    Always,
    Known,
    KnownPos,
    Docs,
    DocsAll,
    Window(i64),
}

/// One fill as the engine records it (the Python glue adds taker user, block and tx hash).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FillRec {
    pub maker_order: u64,
    pub taker_order: Option<u64>,
    pub maker_user: Arc<str>,
    pub px: i64,
    pub sz: i64,
    pub seq: u64,
}

/// A user's resting engine oids in insertion order (Python: dict used as an ordered set).
#[derive(Debug, Clone, Default)]
pub struct UserSet {
    order: OrdMap<u64, u64>, // insertion tick -> oid
    pos: PMap<u64, u64>,     // oid -> insertion tick
}

impl UserSet {
    pub fn contains(&self, oid: u64) -> bool {
        self.pos.contains_key(&oid)
    }
    pub fn oids(&self) -> Vec<u64> {
        self.order.values().copied().collect()
    }
    pub fn len(&self) -> usize {
        self.pos.len()
    }
    pub fn is_empty(&self) -> bool {
        self.pos.is_empty()
    }
}

#[derive(Debug, Clone)]
pub struct Engine {
    pub assets: Vec<u32>, // sorted
    pub books: Vec<Book>, // books[i] is assets[i]
    pub expire_maker: bool,
    pub p_priority: Option<PMode>,
    pub evict_arrival: bool,
    pub p_meta: PMap<u64, (i64, i64)>, // oid -> (p, block time ms)
    pub block_time_ms: i64,
    pub next_oid: u64,
    pub next_seq: u64,
    pub next_fill_seq: u64,
    by_cloid: PMap<(Arc<str>, u32, Arc<str>), Vector<u64>>,
    by_user: PMap<Arc<str>, UserSet>,
    user_tick: u64,
    pub schedules: PMap<Arc<str>, i64>,
    pub block: u64,
    pub stats: OrdMap<String, i64>,
}

fn crosses(side: Side, px: i64, best_opposite: Option<i64>) -> bool {
    match (best_opposite, side) {
        (None, _) => false,
        (Some(o), Side::Buy) => px >= o,
        (Some(o), Side::Sell) => px <= o,
    }
}

fn opposite(side: Side) -> Side {
    match side {
        Side::Buy => Side::Sell,
        Side::Sell => Side::Buy,
    }
}

/// Snapshot / reconcile results, in the Python dict's key order.
pub type Counts = Vec<(&'static str, i64)>;

impl Engine {
    pub fn new(assets: Vec<u32>, expire_maker: bool, p_priority: Option<PMode>, evict_arrival: bool) -> Self {
        let mut assets = assets;
        assets.sort_unstable();
        let books = assets.iter().map(|&a| Book::new(a)).collect();
        Engine {
            assets,
            books,
            expire_maker,
            p_priority,
            evict_arrival,
            p_meta: PMap::new(),
            block_time_ms: 0,
            next_oid: 1,
            next_seq: 1,
            next_fill_seq: 1,
            by_cloid: PMap::new(),
            by_user: PMap::new(),
            user_tick: 0,
            schedules: PMap::new(),
            block: 0,
            stats: OrdMap::new(),
        }
    }

    pub fn bump(&mut self, key: &str, by: i64) {
        *self.stats.entry(key.to_string()).or_insert(0) += by;
    }

    pub fn book_index(&self, asset: u32) -> R<usize> {
        self.assets
            .iter()
            .position(|&a| a == asset)
            .ok_or_else(|| EngineError::Book(format!("no book for asset {asset}")))
    }

    pub fn book(&self, asset: u32) -> R<&Book> {
        Ok(&self.books[self.book_index(asset)?])
    }

    pub fn book_mut(&mut self, asset: u32) -> R<&mut Book> {
        let i = self.book_index(asset)?;
        Ok(&mut self.books[i])
    }

    // ------------------------------------------------------------ by_user (ordered sets)
    pub fn user_oids(&self, user: &str) -> Option<&UserSet> {
        self.by_user.get(user)
    }

    /// `by_user.setdefault(user, {})[oid] = None`: insert keeps the first position of an existing oid.
    pub fn user_add(&mut self, user: Arc<str>, oid: u64) {
        let tick = self.user_tick;
        let set = self.by_user.entry(user).or_default();
        if !set.pos.contains_key(&oid) {
            set.pos.insert(oid, tick);
            set.order.insert(tick, oid);
            self.user_tick += 1;
        }
    }

    fn user_remove(&mut self, user: &Arc<str>, oid: u64) {
        let empty = match self.by_user.get_mut(user) {
            None => return,
            Some(set) => {
                if let Some(t) = set.pos.remove(&oid) {
                    set.order.remove(&t);
                }
                set.is_empty()
            }
        };
        if empty {
            self.by_user.remove(user);
        }
    }

    // ------------------------------------------------------------ public (Engine API)
    #[allow(clippy::too_many_arguments)] // one argument per Order field, as Engine._new_order
    pub fn new_order(&mut self, user: Arc<str>, asset: u32, side: Side, px: i64, sz: i64, tif: u8,
                     cloid: Option<Arc<str>>) -> OrderData {
        let o = OrderData { oid: self.next_oid, user, asset, side, px, sz, tif, cloid, seq: self.next_seq, block: self.block };
        self.next_oid += 1;
        self.next_seq += 1;
        o
    }

    /// Pre-window liquidity: one anonymous order, behind anything already seeded.
    pub fn seed(&mut self, asset: u32, side: Side, px: i64, sz: i64) -> R<u64> {
        let order = self.new_order(Arc::from(PLACEHOLDER), asset, side, px, sz, TIF_GTC, None);
        let oid = order.oid;
        self.book_mut(asset)?.add(Arc::new(order), sz, None)?;
        self.assert_not_crossed(asset)?;
        Ok(oid)
    }

    pub fn assert_not_crossed(&self, asset: u32) -> R<()> {
        let book = self.book(asset)?;
        if book.is_crossed() {
            return Err(EngineError::Crossed(format!(
                "asset {asset} crossed at block {}: bid {} >= ask {}",
                self.block,
                book.best_bid().expect("crossed implies a bid"),
                book.best_ask().expect("crossed implies an ask")
            )));
        }
        Ok(())
    }

    /// Engine._rest: rest an order (priority insertion when p_priority is on), index it by user and cloid.
    pub fn rest(&mut self, order: OrderData, remaining: i64, p: Option<i64>) -> R<()> {
        let order = Arc::new(order);
        let at = match (self.p_priority, p) {
            (Some(mode), Some(p)) if p != 0 => Some(self.jump_position(&order, p, mode)?),
            _ => None,
        };
        if let (Some(mode), Some(p)) = (self.p_priority, p) {
            let records_zero = matches!(mode, PMode::Known | PMode::KnownPos | PMode::Docs | PMode::DocsAll);
            if order.user.as_ref() != PLACEHOLDER && (p != 0 || records_zero) {
                self.p_meta.insert(order.oid, (p, self.block_time_ms));
            }
        }
        self.book_mut(order.asset)?.add(order.clone(), remaining, at)?;
        if order.user.as_ref() != PLACEHOLDER {
            self.user_add(order.user.clone(), order.oid);
            if let Some(cloid) = &order.cloid {
                let key = (order.user.clone(), order.asset, cloid.clone());
                if self.by_cloid.contains_key(&key) {
                    self.bump("cloid_reused_while_resting", 1);
                }
                self.by_cloid.entry(key).or_default().push_back(order.oid);
            }
        }
        Ok(())
    }

    /// Walk forward from the tail past every order the new one jumps (Book.add's `jumps` walk).
    fn jump_position(&self, order: &OrderData, p: i64, mode: PMode) -> R<usize> {
        let t = self.block_time_ms;
        let level = match self.book(order.asset)?.level(order.side, order.px) {
            None => return Ok(0),
            Some(l) => l,
        };
        let mut i = level.nodes.len();
        while i > 0 {
            let other = &level.nodes[i - 1].order;
            let m = self.p_meta.get(&other.oid);
            let jumps = match mode {
                PMode::Always => p > m.map(|x| x.0).unwrap_or(0),
                PMode::Known => m.is_some_and(|x| p > x.0),
                PMode::KnownPos => m.is_some_and(|x| 0 < x.0 && x.0 < p),
                PMode::Docs | PMode::DocsAll => m.is_some_and(|x| {
                    p > x.0
                        && t - x.1 < DOCS_T_MS
                        && (mode == PMode::DocsAll || (order.tif == TIF_ALO && other.tif == TIF_ALO))
                }),
                PMode::Window(w) => m.is_some_and(|x| x.0 > 0 && p > x.0 && t - x.1 <= w),
            };
            if !jumps {
                break;
            }
            i -= 1;
        }
        Ok(i)
    }

    /// Engine._forget: drop an order that left the book from the user / cloid / priority indexes.
    fn forget(&mut self, order: &OrderData) {
        if order.user.as_ref() == PLACEHOLDER {
            return;
        }
        self.p_meta.remove(&order.oid);
        self.user_remove(&order.user, order.oid);
        if let Some(cloid) = &order.cloid {
            let key = (order.user.clone(), order.asset, cloid.clone());
            let empty = match self.by_cloid.get_mut(&key) {
                None => false,
                Some(oids) => {
                    if let Some(i) = oids.index_of(&order.oid) {
                        oids.remove(i);
                    }
                    oids.is_empty()
                }
            };
            if empty {
                self.by_cloid.remove(&key);
            }
        }
    }

    /// Engine._remove: take an order off whichever book holds it and forget it.
    pub fn remove(&mut self, oid: u64) -> R<Arc<OrderData>> {
        let i = self
            .books
            .iter()
            .position(|b| b.contains(oid))
            .ok_or_else(|| EngineError::Book(format!("oid {oid} is not resting")))?;
        let node = self.books[i].remove(oid)?;
        self.forget(&node.order);
        Ok(node.order)
    }

    /// Engine.evict: remove on outside evidence, counted.
    pub fn evict(&mut self, oid: u64) -> R<Arc<OrderData>> {
        let o = self.remove(oid)?;
        self.bump("evicted_by_gate", 1);
        Ok(o)
    }

    fn fill(&mut self, maker: &OrderData, taker_order: Option<u64>, q: i64) -> FillRec {
        let f = FillRec {
            maker_order: maker.oid,
            taker_order,
            maker_user: maker.user.clone(),
            px: maker.px,
            sz: q,
            seq: self.next_fill_seq,
        };
        self.next_fill_seq += 1;
        f
    }

    /// Engine._place for one Place (or a Modify's new order). `p` = the _p_arg value (None when p_priority is off).
    #[allow(clippy::too_many_arguments)] // one argument per field of a Place, as in the Python call
    pub fn place(&mut self, user: Arc<str>, asset: u32, side: Side, px: i64, sz: i64, tif: u8,
                 cloid: Option<Arc<str>>, p: Option<i64>) -> R<(Vec<FillRec>, Vec<RejectCode>)> {
        let best_opp = {
            let book = self.book(asset)?;
            match side {
                Side::Buy => book.best_ask(),
                Side::Sell => book.best_bid(),
            }
        };
        if tif == TIF_ALO {
            if crosses(side, px, best_opp) {
                return Ok((vec![], vec![RejectCode::AloWouldCross]));
            }
            let o = self.new_order(user, asset, side, px, sz, tif, cloid);
            self.rest(o, sz, p)?;
            return Ok((vec![], vec![]));
        }
        let taker = self.new_order(user.clone(), asset, side, px, sz, tif, cloid);
        let mut remaining = sz;
        let mut fills = vec![];
        let mut rejects = vec![];
        let opp = opposite(side);
        while remaining > 0 {
            // re-read the best level every iteration
            let head = {
                let book = self.book(asset)?;
                let best = match opp {
                    Side::Buy => book.best_bid(),
                    Side::Sell => book.best_ask(),
                };
                match best {
                    Some(lpx) if crosses(side, px, Some(lpx)) => {
                        let n = &book.level(opp, lpx).expect("best level exists").nodes[0];
                        (n.order.clone(), n.remaining)
                    }
                    _ => break,
                }
            };
            let (maker, maker_remaining) = head;
            if maker.user == user {
                if !self.expire_maker {
                    rejects.push(RejectCode::SelfTrade);
                    remaining = 0; // cancel the taker's remainder
                    break;
                }
                self.remove(maker.oid)?; // expire_maker
                self.bump("self_trade_expired_maker", 1);
                continue;
            }
            let q = remaining.min(maker_remaining);
            fills.push(self.fill(&maker, Some(taker.oid), q));
            remaining -= q;
            if self.book_mut(asset)?.reduce(maker.oid, q)? == 0 {
                self.forget(&maker);
            }
        }
        if remaining > 0 && tif == TIF_GTC {
            self.rest(taker, remaining, p)?;
        }
        Ok((fills, rejects))
    }

    /// Engine._cancel_cloid: the one resting order with this (user, asset, cloid), else UNKNOWN_ORDER.
    pub fn cancel_cloid(&mut self, user: Arc<str>, asset: u32, cloid: Arc<str>) -> R<Option<RejectCode>> {
        let key = (user, asset, cloid);
        let oids: Vec<u64> = self.by_cloid.get(&key).map(|v| v.iter().copied().collect()).unwrap_or_default();
        if oids.len() != 1 {
            if !oids.is_empty() {
                self.bump("cloid_ambiguous", 1);
            }
            return Ok(Some(RejectCode::UnknownOrder));
        }
        self.remove(oids[0])?;
        Ok(None)
    }

    /// Engine._outside_fill: the named maker's orders at that price (queue order), then placeholders.
    pub fn outside_fill(&mut self, asset: u32, maker_user: &str, taker_side: Side, px: i64, sz: i64)
        -> R<(Vec<FillRec>, Option<RejectCode>)> {
        let mut fills = vec![];
        let mut remaining = sz;
        let order_list: Vec<(Arc<OrderData>, i64)> = match self.book(asset)?.level(opposite(taker_side), px) {
            None => vec![],
            Some(level) => {
                let named = level.nodes.iter().filter(|n| n.order.user.as_ref() == maker_user);
                let anon = level.nodes.iter().filter(|n| n.order.user.as_ref() == PLACEHOLDER);
                named.chain(anon).map(|n| (n.order.clone(), n.remaining)).collect()
            }
        };
        for (maker, rem) in order_list {
            if remaining == 0 {
                break;
            }
            let q = remaining.min(rem);
            fills.push(self.fill(&maker, None, q));
            remaining -= q;
            if self.book_mut(asset)?.reduce(maker.oid, q)? == 0 {
                self.forget(&maker);
            }
        }
        if remaining > 0 {
            self.bump("outside_fill_lots_unmatched", remaining);
            return Ok((fills, Some(RejectCode::NoMaker)));
        }
        Ok((fills, None))
    }

    /// Engine.reconcile (rule 8): trim placeholders toward a snapshot, optionally evict known orders.
    pub fn reconcile(&mut self, asset: u32, bids: &[(i64, i64)], asks: &[(i64, i64)], known: Option<&str>)
        -> R<Counts> {
        if !matches!(known, None | Some("oldest") | Some("newest")) {
            return Err(EngineError::Book(format!("unknown eviction order {known:?}")));
        }
        let (mut trimmed, mut drift, mut missing, mut evicted, mut evicted_orders) = (0, 0, 0, 0, 0);
        for (side, levels) in [(Side::Buy, bids), (Side::Sell, asks)] {
            if levels.is_empty() {
                continue;
            }
            // dict(levels): a repeated price keeps its first position and its last size
            let mut snap: Vec<(i64, i64)> = vec![];
            for &(px, sz) in levels {
                match snap.iter_mut().find(|(p, _)| *p == px) {
                    Some(e) => e.1 = sz,
                    None => snap.push((px, sz)),
                }
            }
            let snap_get = |px: i64| snap.iter().find(|(p, _)| *p == px).map(|e| e.1);
            let edge = match side {
                Side::Buy => snap.iter().map(|e| e.0).min().expect("non-empty"),
                Side::Sell => snap.iter().map(|e| e.0).max().expect("non-empty"),
            };
            let in_range: Vec<i64> = self
                .book(asset)?
                .prices(side)
                .into_iter()
                .filter(|&px| if side == Side::Buy { px >= edge } else { px <= edge })
                .collect();
            for px in in_range {
                let (total, placeholders) = match self.book(asset)?.level(side, px) {
                    None => continue,
                    Some(l) => (
                        l.total,
                        l.nodes
                            .iter()
                            .filter(|n| n.order.user.as_ref() == PLACEHOLDER)
                            .map(|n| (n.order.oid, n.remaining))
                            .collect::<Vec<_>>(),
                    ),
                };
                let mut gap = total - snap_get(px).unwrap_or(0);
                if gap <= 0 {
                    continue;
                }
                for &(oid, rem) in placeholders.iter().rev() {
                    if gap == 0 {
                        break;
                    }
                    let q = gap.min(rem);
                    self.book_mut(asset)?.reduce(oid, q)?;
                    trimmed += q;
                    gap -= q;
                }
                drift += gap;
                if known.is_none() || gap == 0 {
                    continue;
                }
                let mut nodes: Vec<(u64, i64, u64)> = match self.book(asset)?.level(side, px) {
                    None => vec![],
                    Some(l) => l
                        .nodes
                        .iter()
                        .filter(|n| n.order.user.as_ref() != PLACEHOLDER)
                        .map(|n| (n.order.oid, n.remaining, n.order.seq))
                        .collect(),
                };
                if self.evict_arrival {
                    nodes.sort_by_key(|n| n.2); // stable, as Python's sort
                }
                if known == Some("newest") {
                    nodes.reverse();
                }
                for (oid, rem, _) in nodes {
                    if gap == 0 {
                        break;
                    }
                    let q = gap.min(rem);
                    if q == rem {
                        self.remove(oid)?;
                        evicted_orders += 1;
                    } else {
                        self.book_mut(asset)?.reduce(oid, q)?;
                    }
                    evicted += q;
                    gap -= q;
                }
            }
            for &(px, sz) in &snap {
                let have = self.book(asset)?.level(side, px).map(|l| l.total).unwrap_or(0);
                if sz > have {
                    missing += sz - have;
                }
            }
        }
        let out = vec![
            ("trimmed", trimmed),
            ("drift", drift),
            ("missing", missing),
            ("evicted", evicted),
            ("evicted_orders", evicted_orders),
        ];
        for (k, v) in &out {
            self.bump(&format!("reconcile_{k}"), *v);
        }
        Ok(out)
    }

    /// Engine.refill: where a snapshot level holds more than we do, one placeholder for the difference, at the
    /// FRONT of that level (it rested before anything we saw placed).
    pub fn refill(&mut self, asset: u32, bids: &[(i64, i64)], asks: &[(i64, i64)]) -> R<Counts> {
        let (mut refilled, mut refilled_orders) = (0, 0);
        for (side, levels) in [(Side::Buy, bids), (Side::Sell, asks)] {
            for &(px, sz) in levels {
                let have = self.book(asset)?.level(side, px).map(|l| l.total).unwrap_or(0);
                let gap = sz - have;
                if gap > 0 {
                    let o = self.new_order(Arc::from(PLACEHOLDER), asset, side, px, gap, TIF_GTC, None);
                    self.book_mut(asset)?.add(Arc::new(o), gap, Some(0))?;
                    refilled += gap;
                    refilled_orders += 1;
                }
            }
        }
        self.assert_not_crossed(asset)?;
        let out = vec![("refilled", refilled), ("refilled_orders", refilled_orders)];
        for (k, v) in &out {
            self.bump(&format!("refill_{k}"), *v);
        }
        Ok(out)
    }

    /// Engine.clear: remove every resting order on one asset, best price first, queue order inside a level.
    pub fn clear(&mut self, asset: u32) -> R<Counts> {
        let mut oids = vec![];
        {
            let book = self.book(asset)?;
            for side in [Side::Buy, Side::Sell] {
                let mut prices = book.prices(side);
                if side == Side::Buy {
                    prices.reverse();
                }
                for px in prices {
                    oids.extend(book.level(side, px).expect("listed").nodes.iter().map(|n| n.order.oid));
                }
            }
        }
        let (mut known, mut ph) = (0, 0);
        for oid in oids {
            let o = self.remove(oid)?;
            if o.user.as_ref() == PLACEHOLDER {
                ph += 1;
            } else {
                known += 1;
            }
        }
        Ok(vec![("cleared_known", known), ("cleared_placeholder", ph)])
    }

    /// Engine.end_block: fire due scheduleCancels (sorted by (time, user), each user's oids ascending), then
    /// check every book.
    pub fn end_block(&mut self, time_ms: i64) -> R<()> {
        let mut due: Vec<(i64, Arc<str>)> =
            self.schedules.iter().filter(|(_, &at)| at <= time_ms).map(|(u, &at)| (at, u.clone())).collect();
        due.sort();
        for (_, user) in due {
            self.schedules.remove(&user);
            let mut oids = self.by_user.get(&user).map(|s| s.oids()).unwrap_or_default();
            oids.sort_unstable();
            for oid in oids {
                self.remove(oid)?;
                self.bump("schedule_cancelled", 1);
            }
        }
        for i in 0..self.assets.len() {
            self.assert_not_crossed(self.assets[i])?;
        }
        Ok(())
    }

    /// scheduleCancel action: set (Some) or clear (None) the user's dead-man time.
    pub fn schedule(&mut self, user: Arc<str>, at_ms: Option<i64>) {
        match at_ms {
            None => {
                self.schedules.remove(&user);
            }
            Some(t) => {
                self.schedules.insert(user, t);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn u(s: &str) -> Arc<str> {
        Arc::from(s)
    }

    fn eng() -> Engine {
        Engine::new(vec![0, 1], false, None, false)
    }

    #[test]
    fn gtc_rests_ioc_does_not_and_fills_at_maker_price() {
        let mut e = eng();
        e.place(u("a"), 0, Side::Sell, 101, 5, TIF_GTC, None, None).unwrap();
        let (f, r) = e.place(u("b"), 0, Side::Buy, 105, 3, TIF_IOC, None, None).unwrap();
        assert!(r.is_empty());
        assert_eq!((f[0].px, f[0].sz, f[0].maker_order), (101, 3, 1));
        let (f, _) = e.place(u("b"), 0, Side::Buy, 105, 4, TIF_IOC, None, None).unwrap();
        assert_eq!(f[0].sz, 2); // remainder dropped, nothing rests
        assert!(e.book(0).unwrap().best_bid().is_none());
    }

    #[test]
    fn alo_rejected_when_it_would_cross_equal_price_included() {
        let mut e = eng();
        e.place(u("a"), 0, Side::Sell, 101, 5, TIF_GTC, None, None).unwrap();
        let (_, r) = e.place(u("b"), 0, Side::Buy, 101, 1, TIF_ALO, None, None).unwrap();
        assert_eq!(r, vec![RejectCode::AloWouldCross]);
    }

    #[test]
    fn self_trade_modes() {
        let mut e = eng();
        e.place(u("a"), 0, Side::Sell, 101, 5, TIF_GTC, None, None).unwrap();
        let (f, r) = e.place(u("a"), 0, Side::Buy, 101, 2, TIF_GTC, None, None).unwrap();
        assert!(f.is_empty() && r == vec![RejectCode::SelfTrade]); // cancel_taker: maker stays, nothing rests
        assert_eq!(e.book(0).unwrap().best_ask(), Some(101));
        assert!(e.book(0).unwrap().best_bid().is_none());
        let mut e = Engine::new(vec![0, 1], true, None, false);
        e.place(u("a"), 0, Side::Sell, 101, 5, TIF_GTC, None, None).unwrap();
        e.place(u("c"), 0, Side::Sell, 101, 5, TIF_GTC, None, None).unwrap();
        let (f, r) = e.place(u("a"), 0, Side::Buy, 101, 2, TIF_GTC, None, None).unwrap();
        assert!(r.is_empty() && f[0].maker_user.as_ref() == "c"); // expire_maker: own order gone, keep matching
    }

    #[test]
    fn cloid_cancel_and_schedule() {
        let mut e = eng();
        e.place(u("a"), 0, Side::Buy, 99, 5, TIF_ALO, Some(u("x")), None).unwrap();
        assert_eq!(e.cancel_cloid(u("a"), 0, u("x")).unwrap(), None);
        assert_eq!(e.cancel_cloid(u("a"), 0, u("x")).unwrap(), Some(RejectCode::UnknownOrder));
        e.place(u("a"), 0, Side::Buy, 99, 5, TIF_ALO, None, None).unwrap();
        e.schedule(u("a"), Some(1000));
        e.end_block(999).unwrap();
        assert_eq!(e.book(0).unwrap().n_orders(), 1);
        e.end_block(1000).unwrap();
        assert_eq!(e.book(0).unwrap().n_orders(), 0);
    }

    #[test]
    fn docs_priority_jumps_only_young_lower_p_alos() {
        let mut e = Engine::new(vec![0, 1], false, Some(PMode::Docs), false);
        e.block_time_ms = 0;
        e.place(u("a"), 0, Side::Buy, 99, 1, TIF_ALO, None, Some(5)).unwrap(); // oid 1, p 5
        e.block_time_ms = 500;
        e.place(u("b"), 0, Side::Buy, 99, 1, TIF_ALO, None, Some(3)).unwrap(); // oid 2, p 3 (a is too old)
        e.place(u("c"), 0, Side::Buy, 99, 1, TIF_ALO, None, Some(9)).unwrap(); // oid 3 jumps b, not a
        let q: Vec<u64> = e.book(0).unwrap().level(Side::Buy, 99).unwrap().nodes.iter().map(|n| n.order.oid).collect();
        assert_eq!(q, vec![1, 3, 2]);
    }

    #[test]
    fn crossed_book_raises() {
        let mut e = eng();
        e.seed(0, Side::Sell, 100, 5).unwrap();
        assert!(matches!(e.seed(0, Side::Buy, 100, 5), Err(EngineError::Crossed(_))));
    }

    #[test]
    fn clone_is_independent() {
        let mut a = eng();
        a.place(u("a"), 0, Side::Buy, 99, 5, TIF_ALO, Some(u("x")), None).unwrap();
        let mut b = a.clone();
        b.cancel_cloid(u("a"), 0, u("x")).unwrap();
        assert_eq!(a.book(0).unwrap().n_orders(), 1);
        assert_eq!(b.book(0).unwrap().n_orders(), 0);
        assert!(a.user_oids("a").is_some() && b.user_oids("a").is_none());
    }
}
