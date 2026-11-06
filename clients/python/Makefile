.PHONY: install fmt lint typecheck test check

install:
	uv sync --locked

fmt:
	uv run ruff format nao_bridge_client/ tests/ examples/ example_usage.py bootstrap.py
	uv run ruff check --fix nao_bridge_client/ tests/ examples/ example_usage.py bootstrap.py

lint:
	uv run ruff check nao_bridge_client/ tests/ examples/ example_usage.py bootstrap.py
	uv run ruff format --check nao_bridge_client/ tests/ examples/ example_usage.py bootstrap.py

typecheck:
	uv run pyright nao_bridge_client/ examples/ example_usage.py bootstrap.py

test:
	uv run pytest

check: lint typecheck test
