//! One market's order book with copy-on-write price levels (port of `engine_py/book.py`).
//!
//! Chain rules it implements (specs/01-book.md):
//! - price-time priority: per side a sorted map px -> level; best bid = highest px, best ask = lowest;
//!   inside a level a FIFO queue, oldest first (index 0 is the head, the next order to fill);
//! - a price is present iff its queue is non-empty (an emptied level is deleted in the same operation);
//! - every resting order is in the index exactly once, in exactly one queue, at its own side and price;
//! - a level's cached `total` equals the sum of its orders' remaining lots.
//!
//! Copy-on-write (MAHITO_PLAN M5/M2, Satya's decision 3): both sides are persistent maps (`im::OrdMap`) whose
//! values are `Arc<Level>`, and the order index is an `im::HashMap`. `Book::clone()` copies a few pointers;
//! a later change to one level copies only that level (`Arc::make_mut`) plus the map path to it. So the cost of
//! a copy grows with the number of levels the copy changes, not with the size of the book.
//!
//! Insertion position: `add` takes an explicit index into the queue (0 = head, len = tail). The Python engine's
//! priority rules ("jumps", the 400 ms priority window) decide that index; the book only stores the result.
//!
//! All quantities are integers: px in ticks, sizes in lots (no floats anywhere).

use im::{HashMap as PMap, OrdMap};
use std::sync::Arc;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Side {
    Buy,
    Sell,
}

/// A resting order as admitted (fields of `engine_py.types.Order`). Shared by every copy of the book that holds it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OrderData {
    pub oid: u64,
    pub user: Arc<str>,
    pub asset: u32,
    pub side: Side,
    pub px: i64,
    pub sz: i64,
    pub tif: u8, // 0 GTC, 1 IOC, 2 ALO (codes shared with the Python glue)
    pub cloid: Option<Arc<str>>,
    pub seq: u64,
    pub block: u64,
}

/// One queue entry: the order and its remaining lots.
#[derive(Debug, Clone)]
pub struct Node {
    pub order: Arc<OrderData>,
    pub remaining: i64,
}

#[derive(Debug, Clone)]
pub struct Level {
    pub px: i64,
    pub nodes: Vec<Node>, // index 0 = oldest = head
    pub total: i64,
}

#[derive(Debug, Clone)]
pub struct Book {
    pub asset: u32,
    bids: OrdMap<i64, Arc<Level>>,
    asks: OrdMap<i64, Arc<Level>>,
    index: PMap<u64, (Side, i64)>, // oid -> where it rests
}

/// The same failures `engine_py.book.BookError` reports (always a bug, never caught).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BookError(pub String);

impl Book {
    pub fn new(asset: u32) -> Self {
        Book { asset, bids: OrdMap::new(), asks: OrdMap::new(), index: PMap::new() }
    }

    fn side_map(&self, side: Side) -> &OrdMap<i64, Arc<Level>> {
        match side {
            Side::Buy => &self.bids,
            Side::Sell => &self.asks,
        }
    }

    fn side_map_mut(&mut self, side: Side) -> &mut OrdMap<i64, Arc<Level>> {
        match side {
            Side::Buy => &mut self.bids,
            Side::Sell => &mut self.asks,
        }
    }

    // ---------------------------------------------------------------- queries
    pub fn best_bid(&self) -> Option<i64> {
        self.bids.get_max().map(|(px, _)| *px)
    }

    pub fn best_ask(&self) -> Option<i64> {
        self.asks.get_min().map(|(px, _)| *px)
    }

    pub fn level(&self, side: Side, px: i64) -> Option<&Level> {
        self.side_map(side).get(&px).map(|l| l.as_ref())
    }

    /// Prices on one side, ascending.
    pub fn prices(&self, side: Side) -> Vec<i64> {
        self.side_map(side).keys().copied().collect()
    }

    pub fn n_levels(&self, side: Side) -> usize {
        self.side_map(side).len()
    }

    pub fn contains(&self, oid: u64) -> bool {
        self.index.contains_key(&oid)
    }

    pub fn n_orders(&self) -> usize {
        self.index.len()
    }

    /// The order and its queue position.
    pub fn node(&self, oid: u64) -> Result<(&Node, usize), BookError> {
        let (side, px) = *self.index.get(&oid).ok_or_else(|| BookError(format!("oid {oid} is not resting")))?;
        let level = self.level(side, px).expect("index points at a live level");
        let i = level.nodes.iter().position(|n| n.order.oid == oid).expect("index points into the queue");
        Ok((&level.nodes[i], i))
    }

    pub fn is_crossed(&self) -> bool {
        matches!((self.best_bid(), self.best_ask()), (Some(b), Some(a)) if b >= a)
    }

    // ---------------------------------------------------------------- mutations
    /// Rest `order` with `remaining` lots at queue position `at` (0 = head; None = tail).
    pub fn add(&mut self, order: Arc<OrderData>, remaining: i64, at: Option<usize>) -> Result<(), BookError> {
        if order.asset != self.asset {
            return Err(BookError(format!("order for asset {} added to book {}", order.asset, self.asset)));
        }
        if self.index.contains_key(&order.oid) {
            return Err(BookError(format!("oid {} already resting", order.oid)));
        }
        if !(0 < remaining && remaining <= order.sz) {
            return Err(BookError(format!("remaining {remaining} outside (0, {}]", order.sz)));
        }
        let (side, px, oid) = (order.side, order.px, order.oid);
        let levels = self.side_map_mut(side);
        let slot = levels.entry(px).or_insert_with(|| Arc::new(Level { px, nodes: Vec::new(), total: 0 }));
        let level = Arc::make_mut(slot); // copies this level only if another book copy shares it
        let at = at.unwrap_or(level.nodes.len());
        if at > level.nodes.len() {
            return Err(BookError(format!("insert position {at} past the queue end {}", level.nodes.len())));
        }
        level.nodes.insert(at, Node { order, remaining });
        level.total += remaining;
        self.index.insert(oid, (side, px));
        Ok(())
    }

    /// Take an order off the book (cancel, or fully filled). Returns its node (remaining unchanged).
    pub fn remove(&mut self, oid: u64) -> Result<Node, BookError> {
        let (side, px) = self.index.remove(&oid).ok_or_else(|| BookError(format!("oid {oid} is not resting")))?;
        let levels = self.side_map_mut(side);
        let slot = levels.get_mut(&px).expect("index points at a live level");
        let level = Arc::make_mut(slot);
        let i = level.nodes.iter().position(|n| n.order.oid == oid).expect("index points into the queue");
        let node = level.nodes.remove(i);
        level.total -= node.remaining;
        if level.nodes.is_empty() {
            levels.remove(&px);
        }
        Ok(node)
    }

    /// Fill `q` lots of a resting order; removes it when it reaches zero. Returns the lots left (0 = removed).
    pub fn reduce(&mut self, oid: u64, q: i64) -> Result<i64, BookError> {
        let (side, px) = *self.index.get(&oid).ok_or_else(|| BookError(format!("oid {oid} is not resting")))?;
        let remaining = self.node(oid)?.0.remaining;
        if !(0 < q && q <= remaining) {
            return Err(BookError(format!("cannot fill {q} of {remaining} remaining (oid {oid})")));
        }
        if q == remaining {
            self.remove(oid)?;
            return Ok(0);
        }
        let level = Arc::make_mut(self.side_map_mut(side).get_mut(&px).expect("live level"));
        let node = level.nodes.iter_mut().find(|n| n.order.oid == oid).expect("in queue");
        node.remaining -= q;
        level.total -= q;
        Ok(remaining - q)
    }

    // ---------------------------------------------------------------- checks
    pub fn check_invariants(&self) -> Result<(), BookError> {
        let mut seen = 0usize;
        for side in [Side::Buy, Side::Sell] {
            for (px, level) in self.side_map(side).iter() {
                if level.px != *px || level.nodes.is_empty() {
                    return Err(BookError(format!("bad level {px} ({} orders)", level.nodes.len())));
                }
                let mut total = 0i64;
                for node in &level.nodes {
                    let o = &node.order;
                    if o.side != side || o.px != *px {
                        return Err(BookError(format!("oid {} queued at the wrong place", o.oid)));
                    }
                    if self.index.get(&o.oid) != Some(&(side, *px)) {
                        return Err(BookError(format!("oid {} not indexed to its node", o.oid)));
                    }
                    if !(0 < node.remaining && node.remaining <= o.sz) {
                        return Err(BookError(format!("oid {} remaining {}", o.oid, node.remaining)));
                    }
                    total += node.remaining;
                }
                if total != level.total {
                    return Err(BookError(format!(
                        "level {px}: cached {}/{}, real {total}/{}",
                        level.total,
                        level.nodes.len(),
                        level.nodes.len()
                    )));
                }
                seen += level.nodes.len();
            }
        }
        if seen != self.index.len() {
            return Err(BookError(format!("index has {} orders, queues have {seen}", self.index.len())));
        }
        if self.is_crossed() {
            return Err(BookError(format!(
                "crossed book: bid {:?} >= ask {:?}",
                self.best_bid(),
                self.best_ask()
            )));
        }
        Ok(())
    }

    /// True when `self` and `other` share the level at (side, px) in memory (copy-on-write check, tests).
    pub fn shares_level_with(&self, other: &Book, side: Side, px: i64) -> bool {
        match (self.side_map(side).get(&px), other.side_map(side).get(&px)) {
            (Some(a), Some(b)) => Arc::ptr_eq(a, b),
            _ => false,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;

    fn order(oid: u64, side: Side, px: i64, sz: i64) -> Arc<OrderData> {
        Arc::new(OrderData {
            oid,
            user: Arc::from(format!("u{}", oid % 3)),
            asset: 0,
            side,
            px,
            sz,
            tif: 0,
            cloid: None,
            seq: oid,
            block: 1,
        })
    }

    fn queue(b: &Book, side: Side, px: i64) -> Vec<u64> {
        b.level(side, px).map(|l| l.nodes.iter().map(|n| n.order.oid).collect()).unwrap_or_default()
    }

    #[test]
    fn add_cancel_head_middle_tail_last() {
        let mut b = Book::new(0);
        for oid in 1..=3 {
            b.add(order(oid, Side::Buy, 100, 5), 5, None).unwrap();
        }
        assert_eq!(queue(&b, Side::Buy, 100), vec![1, 2, 3]);
        b.remove(2).unwrap(); // middle
        b.check_invariants().unwrap();
        b.remove(1).unwrap(); // head
        b.remove(3).unwrap(); // last in level: the level goes
        assert_eq!(b.n_levels(Side::Buy), 0);
        assert_eq!(b.best_bid(), None);
        b.check_invariants().unwrap();
    }

    #[test]
    fn front_and_positioned_inserts() {
        let mut b = Book::new(0);
        b.add(order(1, Side::Sell, 101, 5), 5, None).unwrap();
        b.add(order(2, Side::Sell, 101, 5), 5, Some(0)).unwrap(); // front
        b.add(order(3, Side::Sell, 101, 5), 5, Some(1)).unwrap(); // between
        assert_eq!(queue(&b, Side::Sell, 101), vec![2, 3, 1]);
        assert_eq!(b.node(1).unwrap().1, 2);
    }

    #[test]
    fn errors_match_python() {
        let mut b = Book::new(0);
        b.add(order(1, Side::Buy, 100, 5), 5, None).unwrap();
        assert!(b.add(order(1, Side::Buy, 100, 5), 5, None).unwrap_err().0.contains("already resting"));
        assert!(b.add(order(2, Side::Buy, 100, 5), 6, None).unwrap_err().0.contains("outside (0, 5]"));
        assert!(b.reduce(1, 6).unwrap_err().0.contains("cannot fill 6 of 5"));
        assert!(b.remove(9).unwrap_err().0.contains("is not resting"));
        let mut other = order(3, Side::Buy, 100, 5);
        Arc::make_mut(&mut other).asset = 1;
        assert!(b.add(other, 5, None).unwrap_err().0.contains("added to book 0"));
    }

    #[test]
    fn reduce_partial_and_full() {
        let mut b = Book::new(0);
        b.add(order(1, Side::Buy, 100, 5), 5, None).unwrap();
        assert_eq!(b.reduce(1, 2).unwrap(), 3);
        assert_eq!(b.level(Side::Buy, 100).unwrap().total, 3);
        assert_eq!(b.reduce(1, 3).unwrap(), 0);
        assert!(!b.contains(1));
        assert_eq!(b.n_levels(Side::Buy), 0);
    }

    #[test]
    fn copy_shares_untouched_levels() {
        let mut a = Book::new(0);
        for (oid, px) in [(1, 100), (2, 99), (3, 98)] {
            a.add(order(oid, Side::Buy, px, 5), 5, None).unwrap();
        }
        let mut b = a.clone();
        for px in [100, 99, 98] {
            assert!(a.shares_level_with(&b, Side::Buy, px));
        }
        b.reduce(2, 1).unwrap(); // touch only px 99 in the copy
        assert!(a.shares_level_with(&b, Side::Buy, 100));
        assert!(!a.shares_level_with(&b, Side::Buy, 99));
        assert!(a.shares_level_with(&b, Side::Buy, 98));
        assert_eq!(a.level(Side::Buy, 99).unwrap().total, 5); // the original is unchanged
        assert_eq!(b.level(Side::Buy, 99).unwrap().total, 4);
    }

    /// (oid, side, px, remaining) for every resting order, in book order.
    type Flat = Vec<(u64, Side, i64, i64)>;

    // Random operation sequences: invariants hold after every step and a model (plain Vec of queues) agrees.
    #[derive(Debug, Clone)]
    enum Op {
        Add { side: bool, px: i64, sz: i64, at: Option<usize> },
        Remove(usize),
        Reduce(usize, i64),
        Copy,
    }

    fn op() -> impl Strategy<Value = Op> {
        prop_oneof![
            (any::<bool>(), 95i64..106, 1i64..9, proptest::option::of(0usize..6))
                .prop_map(|(side, px, sz, at)| Op::Add { side, px, sz, at }),
            (0usize..40).prop_map(Op::Remove),
            ((0usize..40), 1i64..9).prop_map(|(i, q)| Op::Reduce(i, q)),
            Just(Op::Copy),
        ]
    }

    proptest! {
        #[test]
        fn invariants_and_copies(ops in proptest::collection::vec(op(), 1..80)) {
            let mut b = Book::new(0);
            let mut snaps: Vec<(Book, Flat)> = vec![];
            let mut next = 1u64;
            for o in ops {
                match o {
                    Op::Add { side, px, sz, at } => {
                        // bids below 100, asks from 100 up: never crossed
                        let (side, px) = if side { (Side::Buy, px.min(99)) } else { (Side::Sell, px.max(100)) };
                        let len = b.level(side, px).map(|l| l.nodes.len()).unwrap_or(0);
                        let at = at.map(|a| a.min(len));
                        b.add(order(next, side, px, sz), sz, at).unwrap();
                        next += 1;
                    }
                    Op::Remove(i) => {
                        if b.n_orders() > 0 {
                            let oid = all(&b)[i % b.n_orders()].0;
                            b.remove(oid).unwrap();
                        }
                    }
                    Op::Reduce(i, q) => {
                        if b.n_orders() > 0 {
                            let (oid, _, _, rem) = all(&b)[i % b.n_orders()];
                            b.reduce(oid, q.min(rem)).unwrap();
                        }
                    }
                    Op::Copy => snaps.push((b.clone(), all(&b))),
                }
                b.check_invariants().unwrap();
            }
            for (copy, frozen) in snaps {          // later changes never leak into earlier copies
                copy.check_invariants().unwrap();
                prop_assert_eq!(all(&copy), frozen);
            }
        }
    }

    fn all(b: &Book) -> Flat {
        let mut out = vec![];
        for side in [Side::Buy, Side::Sell] {
            for px in b.prices(side) {
                for n in &b.level(side, px).unwrap().nodes {
                    out.push((n.order.oid, side, px, n.remaining));
                }
            }
        }
        out
    }
}
