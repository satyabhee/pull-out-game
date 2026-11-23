.PHONY: install test cpp check rust checkpoint

install:
	uv sync

test:
	uv run pytest -q

cpp:
	./engine_cpp/build.sh

# MAHITO_PLAN §0b: must pass before every commit (scripts/git-hooks/pre-commit runs it).
#   secrets check on staged files; pytest (incl. rule 1 = tests/test_lookahead.py, adapter, Rust-vs-Python
#   parity on the fast test set = tests/test_replay_core.py + test_adapter.py); cargo test + clippy for replay_core.
#   Per-switch parity (tests/test_switch_parity.py) and full dev20 parity (replay/parity.py) run at checkpoints:
#   make checkpoint.
PY ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,uv run python)
export PYO3_PYTHON ?= $(abspath $(if $(wildcard .venv/bin/python),.venv/bin/python,$(shell command -v python3)))
check:
	$(PY) scripts/check_secrets.py
	$(PY) -m pytest -q -p no:cacheprovider
	@if [ -d replay_core ]; then cd replay_core && cargo test --release && cargo clippy --all-targets -- -D warnings; \
	else echo "make check: no replay_core yet"; fi

rust:
	cd replay_core && VIRTUAL_ENV=$(abspath .venv) ../.venv/bin/maturin develop --uv --release

M3 ?= $(HOME)/hl-data/mac3
checkpoint: check
	RUN_SLOW=1 $(PY) -m pytest -q -p no:cacheprovider tests/test_switch_parity.py
	$(PY) -m replay.parity $(M3)/dev20 --hl-data $(M3)/hl_data --out report/validate/parity_rust20 \
		--baseline report/validate/m1_ref_blockend20 --backend rust
	$(PY) -m replay.parity $(M3)/dev20 --hl-data $(M3)/hl_data --out report/validate/parity_rust20_mid \
		--baseline report/validate/m1_ref_mid20 --backend rust --correction midblock
