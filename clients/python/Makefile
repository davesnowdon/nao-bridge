.PHONY: install fmt lint typecheck test check

install:
	uv sync

fmt:
	uv run ruff format nao_bridge_client/ tests/
	uv run ruff check --fix nao_bridge_client/ tests/

lint:
	uv run ruff check nao_bridge_client/ tests/
	uv run ruff format --check nao_bridge_client/ tests/

typecheck:
	uv run pyright nao_bridge_client/

test:
	uv run pytest

check: lint typecheck test
