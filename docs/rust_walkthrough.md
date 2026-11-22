# replay_core: a walkthrough (M5 · Isomer)

`replay_core/` is a small Rust crate that re-implements the hot parts of the Python engine. The Python engine stays
the reference: every Rust function must give exactly the same answer, and the tests check that.

## Layout

```
replay_core/
  Cargo.toml        crate settings and pinned dependencies (pyo3, im; proptest for tests)
  pyproject.toml    tells maturin how to build the Python module
  src/
    lib.rs          the Python module `replay_core`: registers the functions and classes below
    subset.rs       the touch ledger's unique-subset search      (port of engine_py/replay.py::unique_subset)
    book.rs         one market's order book, copy-on-write       (port of engine_py/book.py)
    engine.rs       the matching engine                           (port of engine_py/matching.py)
    pybook.rs       Python glue for book.rs   (classes Book, and helpers shared with BookRef)
    pyengine.rs     Python glue for engine.rs (classes Engine, BookRef)
replay/
  rust_book.py      makes replay_core.Book look exactly like engine_py.book.Book
  rust_engine.py    makes replay_core.Engine look exactly like engine_py.matching.Engine
  adapter.py        EngineAdapter(backend="py" | "rust") picks which one the replay uses
```

The rule of thumb: `book.rs`, `engine.rs`, `subset.rs` hold the chain rules and know nothing about Python.
`py*.rs` translate between Rust and Python and hold no rules. `replay/rust_*.py` make the Rust objects look like the
Python ones, so the Python replay, scoring and oid resolver run on them unchanged.

## How a call travels

1. Python: `engine.apply(place)` on a `RustEngine` (`replay/rust_engine.py`).
2. That wrapper calls `self._core.place(user, asset, side_code, px, sz, tif_code, cloid, p)`: plain ints and strings.
3. PyO3 (`pyengine.rs`, `#[pymethods] fn place`) converts the arguments to Rust types and calls `Engine::place`.
4. `Engine::place` (engine.rs) runs the match loop on the Rust book and returns fills and reject codes.
5. PyO3 converts them back to Python tuples; the wrapper builds `engine_py.types.Fill` / `Reject` objects.

Errors travel the same way back: Rust returns `Err(EngineError::Crossed(msg))`, and `pybook.rs::engine_err`
raises the Python engine's own `CrossedBookError(msg)`.

## Copy-on-write, in one paragraph

A side of the book is an `im::OrdMap<price, Arc<Level>>`. `Arc` is a shared pointer: two books can point at the same
`Level`. Cloning a `Book` copies the map's root (a few pointers), not the levels. When one copy changes a level,
`Arc::make_mut` sees that the level is shared and copies only that level first. So a copy costs nothing up front and
then pays per level it touches (test: `book::tests::copy_shares_untouched_levels`). The engine's own maps
(`by_user`, `by_cloid`, ...) are `im` maps too, so copying a whole engine is just as cheap. M2's particles will use
this.

## Commands

```
make rust            # build the Python module into .venv (maturin develop --uv --release)
make check           # what the pre-commit hook runs: secrets check, all pytest, cargo test, cargo clippy
make checkpoint      # + Rust-vs-Python parity for every switch combination and full dev20 parity (slow)
cd replay_core && cargo test --release          # Rust unit and property tests only
cd replay_core && cargo clippy --all-targets -- -D warnings
```
`cargo` needs `PYO3_PYTHON` to point at the venv's python; the Makefile sets it.

## Tests that prove "same as Python"

| test | what it compares |
|---|---|
| `replay_core/src/*.rs` `#[cfg(test)]` | Rust unit tests + proptest (subset vs brute force; book invariants and copies) |
| `tests/test_replay_core.py` | subset search, Rust vs Python, 3,000 generated + 400 real-size cases |
| `tests/test_rust_book.py` | random book operation sequences, Rust vs Python book, after every step |
| `tests/test_rust_engine.py` | random action sequences under every engine setting, Rust vs Python engine, after every step |
| `tests/test_adapter.py` | first 40 blocks of a real dev20 chunk, both backends, identical fills; copies |
| `tests/test_switch_parity.py` | the same for each of the 8 stp x order x correction combinations (`RUN_SLOW=1`) |
| `replay/parity.py --backend rust` | all 20 dev20 chunks scored; every output CSV byte-identical to the Python baseline |

## Reading order for a Rust beginner

1. `book.rs`: structs (`OrderData`, `Node`, `Level`, `Book`), `Result<_, BookError>` instead of exceptions, `?`
   to pass errors up, `Arc::make_mut` for copy-on-write. The tests at the bottom show how it is used.
2. `engine.rs`: same style; each function names the Python function it ports. Compare side by side with
   `engine_py/matching.py`.
3. `pybook.rs` / `pyengine.rs`: `#[pyclass]`, `#[pymethods]`, `PyResult`, how errors become Python exceptions.
4. `subset.rs`: a small dynamic program with a property test against brute force.

No `unsafe` code anywhere in the crate.
