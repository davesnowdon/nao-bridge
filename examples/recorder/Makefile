.PHONY: install fmt lint typecheck

install:
	uv sync --locked

fmt:
	uv run ruff format recorder
	uv run ruff check --fix recorder

lint:
	uv run ruff check recorder
	uv run ruff format --check recorder

typecheck:
	uv run pyright recorder
