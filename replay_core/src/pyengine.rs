//! Python glue for `engine::Engine` (no chain rules here: see engine.rs).
//!
//! `replay/rust_engine.py` wraps `Engine` into the exact API of `engine_py.matching.Engine` (the replay and the
//! oid resolver use its internals: books, by_user, next_oid, ...). Each of the engine's books is reachable from
//! Python as a `BookRef`, which has the same methods as the standalone `Book` (pybook.rs).
//!
//! Codes: side 0 BUY / 1 SELL; tif 0 GTC / 1 IOC / 2 ALO; reject 0 ALO_WOULD_CROSS / 1 SELF_TRADE /
//! 2 UNKNOWN_ORDER / 3 NO_MAKER. A fill crosses as (maker_order, taker_order | None, maker_user, px, sz, seq).

use crate::engine::{Engine, FillRec, PMode, RejectCode};
use crate::pybook::{
    engine_err, h_add, h_level_info, h_level_node, h_level_nodes, h_node, h_reduce, h_remove, order_from,
    order_tuple, side_of, OrderIn, OrderTuple,
};
use pyo3::prelude::*;
use std::sync::Arc;

type FillOut = (u64, Option<u64>, String, i64, i64, u64);

fn fill_out(f: &FillRec) -> FillOut {
    (f.maker_order, f.taker_order, f.maker_user.to_string(), f.px, f.sz, f.seq)
}

fn code(r: RejectCode) -> u8 {
    match r {
        RejectCode::AloWouldCross => 0,
        RejectCode::SelfTrade => 1,
        RejectCode::UnknownOrder => 2,
        RejectCode::NoMaker => 3,
    }
}

fn counts(c: Vec<(&'static str, i64)>) -> Vec<(String, i64)> {
    c.into_iter().map(|(k, v)| (k.to_string(), v)).collect()
}

#[pyclass(name = "Engine", module = "replay_core")]
#[derive(Clone)]
pub struct PyEngine {
    pub inner: Engine,
}

#[pymethods]
impl PyEngine {
    /// Engine(assets, expire_maker, p_mode, p_window, evict_arrival). p_mode: None | "always" | "known" |
    /// "knownpos" | "docs" | "docsall" | "window" (then p_window = W ms).
    #[new]
    #[pyo3(signature = (assets, expire_maker = false, p_mode = None, p_window = None, evict_arrival = false))]
    fn new(assets: Vec<u32>, expire_maker: bool, p_mode: Option<String>, p_window: Option<i64>,
           evict_arrival: bool) -> PyResult<Self> {
        let p = match p_mode.as_deref() {
            None => None,
            Some("always") => Some(PMode::Always),
            Some("known") => Some(PMode::Known),
            Some("knownpos") => Some(PMode::KnownPos),
            Some("docs") => Some(PMode::Docs),
            Some("docsall") => Some(PMode::DocsAll),
            Some("window") => Some(PMode::Window(p_window.ok_or_else(|| {
                pyo3::exceptions::PyValueError::new_err("p_mode window needs p_window")
            })?)),
            Some(other) => return Err(pyo3::exceptions::PyValueError::new_err(format!("p_mode {other}"))),
        };
        Ok(PyEngine { inner: Engine::new(assets, expire_maker, p, evict_arrival) })
    }

    #[getter]
    fn get_next_oid(&self) -> u64 {
        self.inner.next_oid
    }
    #[setter]
    fn set_next_oid(&mut self, v: u64) {
        self.inner.next_oid = v;
    }
    #[getter]
    fn get_next_seq(&self) -> u64 {
        self.inner.next_seq
    }
    #[setter]
    fn set_next_seq(&mut self, v: u64) {
        self.inner.next_seq = v;
    }
    #[getter]
    fn get_next_fill_seq(&self) -> u64 {
        self.inner.next_fill_seq
    }
    #[setter]
    fn set_next_fill_seq(&mut self, v: u64) {
        self.inner.next_fill_seq = v;
    }
    #[getter]
    fn get_block(&self) -> u64 {
        self.inner.block
    }
    #[setter]
    fn set_block(&mut self, v: u64) {
        self.inner.block = v;
    }
    #[getter]
    fn get_block_time_ms(&self) -> i64 {
        self.inner.block_time_ms
    }
    #[setter]
    fn set_block_time_ms(&mut self, v: i64) {
        self.inner.block_time_ms = v;
    }

    fn stats(&self) -> Vec<(String, i64)> {
        self.inner.stats.iter().map(|(k, v)| (k.clone(), *v)).collect()
    }

    fn schedules(&self) -> Vec<(String, i64)> {
        let mut v: Vec<(String, i64)> = self.inner.schedules.iter().map(|(u, t)| (u.to_string(), *t)).collect();
        v.sort();
        v
    }

    #[pyo3(signature = (user, asset, side, px, sz, tif, cloid))]
    #[allow(clippy::too_many_arguments)]
    fn new_order<'py>(&mut self, py: Python<'py>, user: String, asset: u32, side: u8, px: i64, sz: i64, tif: u8,
                      cloid: Option<String>) -> PyResult<OrderTuple<'py>> {
        let o = self.inner.new_order(Arc::from(user), asset, side_of(side)?, px, sz, tif, cloid.map(Arc::from));
        order_tuple(py, &o)
    }

    #[pyo3(signature = (order, remaining, p = None))]
    fn rest(&mut self, py: Python<'_>, order: OrderIn, remaining: i64, p: Option<i64>) -> PyResult<()> {
        self.inner.rest(order_from(order)?, remaining, p).map_err(|e| engine_err(py, e))
    }

    fn remove<'py>(&mut self, py: Python<'py>, oid: u64) -> PyResult<OrderTuple<'py>> {
        let o = self.inner.remove(oid).map_err(|e| engine_err(py, e))?;
        order_tuple(py, &o)
    }

    fn evict<'py>(&mut self, py: Python<'py>, oid: u64) -> PyResult<OrderTuple<'py>> {
        let o = self.inner.evict(oid).map_err(|e| engine_err(py, e))?;
        order_tuple(py, &o)
    }

    fn seed(&mut self, py: Python<'_>, asset: u32, side: u8, px: i64, sz: i64) -> PyResult<u64> {
        self.inner.seed(asset, side_of(side)?, px, sz).map_err(|e| engine_err(py, e))
    }

    #[pyo3(signature = (user, asset, side, px, sz, tif, cloid, p))]
    #[allow(clippy::too_many_arguments)]
    fn place(&mut self, py: Python<'_>, user: String, asset: u32, side: u8, px: i64, sz: i64, tif: u8,
             cloid: Option<String>, p: Option<i64>) -> PyResult<(Vec<FillOut>, Vec<u8>)> {
        let (fills, rejects) = self
            .inner
            .place(Arc::from(user), asset, side_of(side)?, px, sz, tif, cloid.map(Arc::from), p)
            .map_err(|e| engine_err(py, e))?;
        Ok((fills.iter().map(fill_out).collect(), rejects.into_iter().map(code).collect()))
    }

    fn cancel_cloid(&mut self, py: Python<'_>, user: String, asset: u32, cloid: String) -> PyResult<Option<u8>> {
        let r = self.inner.cancel_cloid(Arc::from(user), asset, Arc::from(cloid)).map_err(|e| engine_err(py, e))?;
        Ok(r.map(code))
    }

    fn outside_fill(&mut self, py: Python<'_>, asset: u32, maker_user: String, taker_side: u8, px: i64, sz: i64)
        -> PyResult<(Vec<FillOut>, Option<u8>)> {
        let (fills, r) = self
            .inner
            .outside_fill(asset, &maker_user, side_of(taker_side)?, px, sz)
            .map_err(|e| engine_err(py, e))?;
        Ok((fills.iter().map(fill_out).collect(), r.map(code)))
    }

    #[pyo3(signature = (asset, bids, asks, known = None))]
    fn reconcile(&mut self, py: Python<'_>, asset: u32, bids: Vec<(i64, i64)>, asks: Vec<(i64, i64)>,
                 known: Option<String>) -> PyResult<Vec<(String, i64)>> {
        let c = self.inner.reconcile(asset, &bids, &asks, known.as_deref()).map_err(|e| engine_err(py, e))?;
        Ok(counts(c))
    }

    fn refill(&mut self, py: Python<'_>, asset: u32, bids: Vec<(i64, i64)>, asks: Vec<(i64, i64)>)
        -> PyResult<Vec<(String, i64)>> {
        let c = self.inner.refill(asset, &bids, &asks).map_err(|e| engine_err(py, e))?;
        Ok(counts(c))
    }

    fn clear(&mut self, py: Python<'_>, asset: u32) -> PyResult<Vec<(String, i64)>> {
        let c = self.inner.clear(asset).map_err(|e| engine_err(py, e))?;
        Ok(counts(c))
    }

    fn end_block(&mut self, py: Python<'_>, time_ms: i64) -> PyResult<()> {
        self.inner.end_block(time_ms).map_err(|e| engine_err(py, e))
    }

    #[pyo3(signature = (user, at_ms = None))]
    fn schedule(&mut self, user: String, at_ms: Option<i64>) {
        self.inner.schedule(Arc::from(user), at_ms);
    }

    fn assert_not_crossed(&self, py: Python<'_>, asset: u32) -> PyResult<()> {
        self.inner.assert_not_crossed(asset).map_err(|e| engine_err(py, e))
    }

    /// by_user[user] as a list of oids in insertion order, or None when the user has no resting order.
    fn user_oids(&self, user: &str) -> Option<Vec<u64>> {
        self.inner.user_oids(user).map(|s| s.oids())
    }

    fn user_contains(&self, user: &str, oid: u64) -> bool {
        self.inner.user_oids(user).is_some_and(|s| s.contains(oid))
    }

    fn user_len(&self, user: &str) -> usize {
        self.inner.user_oids(user).map(|s| s.len()).unwrap_or(0)
    }

    /// by_user.setdefault(user, {})[oid] = None
    fn user_add(&mut self, user: String, oid: u64) {
        self.inner.user_add(Arc::from(user), oid);
    }

    fn assets(&self) -> Vec<u32> {
        self.inner.assets.clone()
    }

    /// Copy-on-write copy of the whole engine (books share untouched levels).
    fn copy(&self) -> PyEngine {
        self.clone()
    }

    /// A view of the engine's book for `asset` (same methods as replay_core.Book).
    fn book(slf: &Bound<'_, Self>, asset: u32) -> PyResult<BookRef> {
        slf.borrow().inner.book_index(asset).map_err(|e| engine_err(slf.py(), e))?;
        Ok(BookRef { engine: slf.clone().unbind(), asset })
    }
}

/// One of a Rust Engine's books, seen from Python. Direct book operations (as Python code does with
/// `engine.books[a].add(...)`) touch only the book, never the engine's indexes: same as engine_py.book.Book.
#[pyclass(module = "replay_core")]
pub struct BookRef {
    engine: Py<PyEngine>,
    asset: u32,
}

impl BookRef {
    fn with<T>(&self, py: Python<'_>, f: impl FnOnce(&crate::book::Book) -> PyResult<T>) -> PyResult<T> {
        let eng = self.engine.bind(py).borrow();
        let b = eng.inner.book(self.asset).map_err(|e| engine_err(py, e))?;
        f(b)
    }

    fn with_mut<T>(&self, py: Python<'_>, f: impl FnOnce(&mut crate::book::Book) -> PyResult<T>) -> PyResult<T> {
        let mut eng = self.engine.bind(py).borrow_mut();
        let b = eng.inner.book_mut(self.asset).map_err(|e| engine_err(py, e))?;
        f(b)
    }
}

#[pymethods]
impl BookRef {
    #[getter]
    fn asset(&self) -> u32 {
        self.asset
    }

    #[pyo3(signature = (order, remaining, at = None))]
    fn add(&self, py: Python<'_>, order: OrderIn, remaining: i64, at: Option<usize>) -> PyResult<()> {
        self.with_mut(py, |b| h_add(py, b, order, remaining, at))
    }

    fn remove<'py>(&self, py: Python<'py>, oid: u64) -> PyResult<(OrderTuple<'py>, i64)> {
        self.with_mut(py, |b| h_remove(py, b, oid))
    }

    fn reduce<'py>(&self, py: Python<'py>, oid: u64, q: i64) -> PyResult<(OrderTuple<'py>, i64)> {
        self.with_mut(py, |b| h_reduce(py, b, oid, q))
    }

    fn best_bid(&self, py: Python<'_>) -> PyResult<Option<i64>> {
        self.with(py, |b| Ok(b.best_bid()))
    }

    fn best_ask(&self, py: Python<'_>) -> PyResult<Option<i64>> {
        self.with(py, |b| Ok(b.best_ask()))
    }

    fn prices(&self, py: Python<'_>, side: u8) -> PyResult<Vec<i64>> {
        self.with(py, |b| Ok(b.prices(side_of(side)?)))
    }

    fn n_levels(&self, py: Python<'_>, side: u8) -> PyResult<usize> {
        self.with(py, |b| Ok(b.n_levels(side_of(side)?)))
    }

    fn level_info(&self, py: Python<'_>, side: u8, px: i64) -> PyResult<Option<(i64, usize)>> {
        self.with(py, |b| h_level_info(b, side, px))
    }

    fn level_nodes<'py>(&self, py: Python<'py>, side: u8, px: i64) -> PyResult<Vec<(OrderTuple<'py>, i64)>> {
        self.with(py, |b| h_level_nodes(py, b, side, px))
    }

    fn level_node<'py>(&self, py: Python<'py>, side: u8, px: i64, i: usize) -> PyResult<(OrderTuple<'py>, i64)> {
        self.with(py, |b| h_level_node(py, b, side, px, i))
    }

    fn node<'py>(&self, py: Python<'py>, oid: u64) -> PyResult<(OrderTuple<'py>, i64)> {
        self.with(py, |b| h_node(py, b, oid))
    }

    fn contains(&self, py: Python<'_>, oid: u64) -> PyResult<bool> {
        self.with(py, |b| Ok(b.contains(oid)))
    }

    fn n_orders(&self, py: Python<'_>) -> PyResult<usize> {
        self.with(py, |b| Ok(b.n_orders()))
    }

    fn is_crossed(&self, py: Python<'_>) -> PyResult<bool> {
        self.with(py, |b| Ok(b.is_crossed()))
    }

    fn check_invariants(&self, py: Python<'_>) -> PyResult<()> {
        self.with(py, |b| b.check_invariants().map_err(|e| crate::pybook::book_err(py, e)))
    }
}
