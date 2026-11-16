.PHONY: install test cpp

install:
	uv sync

test:
	uv run pytest -q

cpp:
	./engine_cpp/build.sh
