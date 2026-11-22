//! Python glue for `book::Book` (no chain rules here: see book.rs).
//!
//! The Python side (`replay/rust_book.py`) wraps these classes so they have exactly the API of
//! `engine_py.book.Book`. Two classes expose the same methods: `Book`, a standalone book (tests, and the
//! book-only Rust backend), and `BookRef` (pyengine.rs), a view of one book owned by a Rust `Engine`.
//! Both call the helper functions below, so they behave identically.
//!
//! Orders cross the boundary as plain tuples `(oid, user, asset, side, px, sz, tif, cloid, seq, block)` with
//! side 0 BUY / 1 SELL and tif 0 GTC / 1 IOC / 2 ALO. Failures raise the Python engine's own exception classes
//! (`engine_py.book.BookError`, `engine_py.matching.CrossedBookError`), so callers see the same exception types.

use crate::book::{Book, BookError, Node, OrderData, Side};
use crate::engine::EngineError;
use pyo3::prelude::*;
use pyo3::types::PyTuple;
use std::sync::Arc;

pub type OrderTuple<'py> = Bound<'py, PyTuple>;
pub type OrderIn = (u64, String, u32, u8, i64, i64, u8, Option<String>, u64, u64);

pub fn side_of(code: u8) -> PyResult<Side> {
    match code {
        0 => Ok(Side::Buy),
        1 => Ok(Side::Sell),
        _ => Err(pyo3::exceptions::PyValueError::new_err(format!("side code {code}"))),
    }
}

pub fn side_code(side: Side) -> u8 {
    match side {
        Side::Buy => 0,
        Side::Sell => 1,
    }
}

fn py_exception(py: Python<'_>, module: &str, class: &str, msg: String) -> PyErr {
    match py.import(module).and_then(|m| m.getattr(class)) {
        Ok(cls) => PyErr::from_type(cls.downcast_into().expect("exception classes are types"), msg),
        Err(err) => err,
    }
}

pub fn book_err(py: Python<'_>, e: BookError) -> PyErr {
    py_exception(py, "engine_py.book", "BookError", e.0)
}

pub fn engine_err(py: Python<'_>, e: EngineError) -> PyErr {
    match e {
        EngineError::Book(m) => py_exception(py, "engine_py.book", "BookError", m),
        EngineError::Crossed(m) => py_exception(py, "engine_py.matching", "CrossedBookError", m),
    }
}

pub fn order_from(t: OrderIn) -> PyResult<OrderData> {
    let (oid, user, asset, side, px, sz, tif, cloid, seq, block) = t;
    Ok(OrderData {
        oid,
        user: Arc::from(user),
        asset,
        side: side_of(side)?,
        px,
        sz,
        tif,
        cloid: cloid.map(Arc::from),
        seq,
        block,
    })
}

pub fn order_tuple<'py>(py: Python<'py>, o: &OrderData) -> PyResult<OrderTuple<'py>> {
    PyTuple::new(
        py,
        [
            o.oid.into_pyobject(py)?.into_any(),
            o.user.as_ref().into_pyobject(py)?.into_any(),
            o.asset.into_pyobject(py)?.into_any(),
            side_code(o.side).into_pyobject(py)?.into_any(),
            o.px.into_pyobject(py)?.into_any(),
            o.sz.into_pyobject(py)?.into_any(),
            o.tif.into_pyobject(py)?.into_any(),
            match &o.cloid {
                Some(c) => c.as_ref().into_pyobject(py)?.into_any(),
                None => py.None().into_bound(py),
            },
            o.seq.into_pyobject(py)?.into_any(),
            o.block.into_pyobject(py)?.into_any(),
        ],
    )
}

pub fn node_pair<'py>(py: Python<'py>, n: &Node) -> PyResult<(OrderTuple<'py>, i64)> {
    Ok((order_tuple(py, &n.order)?, n.remaining))
}

// ---------------------------------------------------------------- helpers shared by Book and BookRef
pub fn h_add(py: Python<'_>, b: &mut Book, order: OrderIn, remaining: i64, at: Option<usize>) -> PyResult<()> {
    b.add(Arc::new(order_from(order)?), remaining, at).map_err(|e| book_err(py, e))
}

pub fn h_remove<'py>(py: Python<'py>, b: &mut Book, oid: u64) -> PyResult<(OrderTuple<'py>, i64)> {
    let node = b.remove(oid).map_err(|e| book_err(py, e))?;
    node_pair(py, &node)
}

pub fn h_reduce<'py>(py: Python<'py>, b: &mut Book, oid: u64, q: i64) -> PyResult<(OrderTuple<'py>, i64)> {
    let order = b.node(oid).map_err(|e| book_err(py, e))?.0.order.clone();
    let left = b.reduce(oid, q).map_err(|e| book_err(py, e))?;
    Ok((order_tuple(py, &order)?, left))
}

pub fn h_level_info(b: &Book, side: u8, px: i64) -> PyResult<Option<(i64, usize)>> {
    Ok(b.level(side_of(side)?, px).map(|l| (l.total, l.nodes.len())))
}

pub fn h_level_nodes<'py>(py: Python<'py>, b: &Book, side: u8, px: i64) -> PyResult<Vec<(OrderTuple<'py>, i64)>> {
    match b.level(side_of(side)?, px) {
        None => Ok(vec![]),
        Some(l) => l.nodes.iter().map(|n| node_pair(py, n)).collect(),
    }
}

pub fn h_level_node<'py>(py: Python<'py>, b: &Book, side: u8, px: i64, i: usize) -> PyResult<(OrderTuple<'py>, i64)> {
    let level = b
        .level(side_of(side)?, px)
        .ok_or_else(|| pyo3::exceptions::PyIndexError::new_err(format!("no level at {px}")))?;
    let n = level.nodes.get(i).ok_or_else(|| pyo3::exceptions::PyIndexError::new_err(i))?;
    node_pair(py, n)
}

pub fn h_node<'py>(py: Python<'py>, b: &Book, oid: u64) -> PyResult<(OrderTuple<'py>, i64)> {
    let (n, _) = b.node(oid).map_err(|e| book_err(py, e))?;
    node_pair(py, n)
}

#[pyclass(name = "Book", module = "replay_core")]
#[derive(Clone)]
pub struct PyBook {
    pub inner: Book,
}

#[pymethods]
impl PyBook {
    #[new]
    fn new(asset: u32) -> Self {
        PyBook { inner: Book::new(asset) }
    }

    #[getter]
    fn asset(&self) -> u32 {
        self.inner.asset
    }

    /// add(order_tuple, remaining, at=None): rest an order at queue position `at` (0 = head, None = tail).
    #[pyo3(signature = (order, remaining, at = None))]
    fn add(&mut self, py: Python<'_>, order: OrderIn, remaining: i64, at: Option<usize>) -> PyResult<()> {
        h_add(py, &mut self.inner, order, remaining, at)
    }

    /// remove(oid) -> (order_tuple, remaining)
    fn remove<'py>(&mut self, py: Python<'py>, oid: u64) -> PyResult<(OrderTuple<'py>, i64)> {
        h_remove(py, &mut self.inner, oid)
    }

    /// reduce(oid, q) -> (order_tuple, remaining after; 0 = removed)
    fn reduce<'py>(&mut self, py: Python<'py>, oid: u64, q: i64) -> PyResult<(OrderTuple<'py>, i64)> {
        h_reduce(py, &mut self.inner, oid, q)
    }

    fn best_bid(&self) -> Option<i64> {
        self.inner.best_bid()
    }

    fn best_ask(&self) -> Option<i64> {
        self.inner.best_ask()
    }

    /// Prices on one side, ascending.
    fn prices(&self, side: u8) -> PyResult<Vec<i64>> {
        Ok(self.inner.prices(side_of(side)?))
    }

    fn n_levels(&self, side: u8) -> PyResult<usize> {
        Ok(self.inner.n_levels(side_of(side)?))
    }

    /// level_info(side, px) -> (total lots, order count) or None
    fn level_info(&self, side: u8, px: i64) -> PyResult<Option<(i64, usize)>> {
        h_level_info(&self.inner, side, px)
    }

    /// level_nodes(side, px) -> [(order_tuple, remaining)] head first ([] if no level)
    fn level_nodes<'py>(&self, py: Python<'py>, side: u8, px: i64) -> PyResult<Vec<(OrderTuple<'py>, i64)>> {
        h_level_nodes(py, &self.inner, side, px)
    }

    /// level_node(side, px, i) -> (order_tuple, remaining), the i-th order from the head
    fn level_node<'py>(&self, py: Python<'py>, side: u8, px: i64, i: usize) -> PyResult<(OrderTuple<'py>, i64)> {
        h_level_node(py, &self.inner, side, px, i)
    }

    /// node(oid) -> (order_tuple, remaining)
    fn node<'py>(&self, py: Python<'py>, oid: u64) -> PyResult<(OrderTuple<'py>, i64)> {
        h_node(py, &self.inner, oid)
    }

    fn contains(&self, oid: u64) -> bool {
        self.inner.contains(oid)
    }

    fn n_orders(&self) -> usize {
        self.inner.n_orders()
    }

    fn is_crossed(&self) -> bool {
        self.inner.is_crossed()
    }

    fn check_invariants(&self, py: Python<'_>) -> PyResult<()> {
        self.inner.check_invariants().map_err(|e| book_err(py, e))
    }

    /// Copy-on-write copy: O(1) now; later changes copy only the levels they touch.
    fn copy(&self) -> PyBook {
        self.clone()
    }

    /// True when this book and `other` share the level at (side, px) in memory (tests of copy-on-write).
    fn shares_level_with(&self, other: &PyBook, side: u8, px: i64) -> PyResult<bool> {
        Ok(self.inner.shares_level_with(&other.inner, side_of(side)?, px))
    }
}
