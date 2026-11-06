.PHONY: install fmt lint typecheck test check run

install:
	uv sync --locked

fmt:
	uv run ruff format nao_bridge tests/core tests/container_smoke.py ../scripts
	uv run ruff check --fix nao_bridge tests/core tests/container_smoke.py ../scripts

lint:
	uv run ruff check nao_bridge tests/core tests/container_smoke.py ../scripts
	uv run ruff format --check nao_bridge tests/core tests/container_smoke.py ../scripts

typecheck:
	uv run pyright nao_bridge

test:
	uv run pytest

check: lint typecheck test

run:
	uv run nao-bridge --backend fake
