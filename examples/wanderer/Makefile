.PHONY: install fmt lint typecheck

install:
	uv sync --locked

fmt:
	uv run ruff format wanderer
	uv run ruff check --fix wanderer

lint:
	uv run ruff check wanderer
	uv run ruff format --check wanderer

typecheck:
	uv run pyright wanderer
