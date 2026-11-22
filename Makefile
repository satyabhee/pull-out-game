.PHONY: install test cpp check

install:
	uv sync

test:
	uv run pytest -q

cpp:
	./engine_cpp/build.sh

# MAHITO_PLAN §0b: must pass before every commit (scripts/git-hooks/pre-commit runs it).
#   secrets check on staged files; pytest (includes tests/test_lookahead.py = rule 1 and tests/test_adapter.py);
#   Rust-vs-Python parity and cargo test/clippy join here in M5 (replay_core does not exist yet).
PY ?= uv run python
check:
	$(PY) scripts/check_secrets.py
	$(PY) -m pytest -q -p no:cacheprovider
	@if [ -d replay_core ]; then cd replay_core && cargo test && cargo clippy -- -D warnings; \
	else echo "make check: no replay_core yet (M5): Rust parity skipped"; fi
