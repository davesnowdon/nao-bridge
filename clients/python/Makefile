.PHONY: install fmt lint typecheck test check
presentation = ../../examples/presentation

install:
	uv sync --locked

fmt:
	uv run ruff format --config pyproject.toml nao_bridge_client/ tests/ examples/ example_usage.py bootstrap.py $(presentation)
	uv run ruff check --config pyproject.toml --fix nao_bridge_client/ tests/ examples/ example_usage.py bootstrap.py $(presentation)

lint:
	uv run ruff check --config pyproject.toml nao_bridge_client/ tests/ examples/ example_usage.py bootstrap.py $(presentation)
	uv run ruff format --config pyproject.toml --check nao_bridge_client/ tests/ examples/ example_usage.py bootstrap.py $(presentation)

typecheck:
	uv run pyright nao_bridge_client/ examples/ example_usage.py bootstrap.py $(presentation)

test:
	uv run pytest

check: lint typecheck test
