//! `replay_core`: Rust hot path for the order-book replay (MAHITO_PLAN M5).
//!
//! The Python engine (`engine_py/`) is the reference; every function here must return exactly what its Python
//! counterpart returns (checked in `tests/test_replay_core.py` and on dev20 via `replay/parity.py`).
//! Prices are integer ticks and sizes integer lots on both sides of the boundary: nothing is converted here.
//!
//! Modules:
//! - `subset`: the touch ledger's unique-subset search (`engine_py/replay.py::unique_subset`), the measured hot
//!   spot of a one-guess replay (85-92% of per-block time on dev20).
//! - `book`: one market's order book with copy-on-write price levels (`engine_py/book.py`).
//! - `pybook`: the Python class for `book` (wrapped by `replay/rust_book.py` into engine_py.book.Book's API).
//! - `engine`: the matching engine (`engine_py/matching.py`) on copy-on-write books.
//! - `pyengine`: the Python classes for `engine` (wrapped by `replay/rust_engine.py` into engine_py.matching.Engine's API).

pub mod book;
pub mod engine;
pub mod pybook;
pub mod pyengine;
pub mod subset;

use pyo3::prelude::*;
use pyo3::types::PyTuple;

/// unique_subset(sizes, k, target, budget=300000) -> tuple[int, ...] | "none" | "ambiguous" | "budget"
/// Same contract as engine_py.replay.unique_subset. Raises OverflowError if a sum could leave i64.
#[pyfunction]
#[pyo3(signature = (sizes, k, target, budget = 300_000))]
fn unique_subset(py: Python<'_>, sizes: Vec<i64>, k: i64, target: i64, budget: u64) -> PyResult<PyObject> {
    let mut total: i64 = 0;
    for &s in &sizes {
        total = total
            .checked_add(s.abs())
            .ok_or_else(|| pyo3::exceptions::PyOverflowError::new_err("sizes sum past i64"))?;
    }
    if target.checked_abs().is_none_or(|t| t.checked_add(total).is_none()) {
        return Err(pyo3::exceptions::PyOverflowError::new_err("target past i64"));
    }
    // The DP needs no Python objects: let other threads run while it searches.
    let res = py.allow_threads(|| subset::unique_subset(&sizes, k, target, budget));
    Ok(match res {
        subset::Subset::Unique(v) => PyTuple::new(py, v)?.into_any().unbind(),
        subset::Subset::None => "none".into_pyobject(py)?.into_any().unbind(),
        subset::Subset::Ambiguous => "ambiguous".into_pyobject(py)?.into_any().unbind(),
        subset::Subset::Budget => "budget".into_pyobject(py)?.into_any().unbind(),
    })
}

#[pymodule]
fn replay_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(unique_subset, m)?)?;
    m.add_class::<pybook::PyBook>()?;
    m.add_class::<pyengine::PyEngine>()?;
    m.add_class::<pyengine::BookRef>()?;
    Ok(())
}
