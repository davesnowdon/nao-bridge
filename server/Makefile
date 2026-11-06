.PHONY: install fmt lint typecheck test check run

install:
	uv sync --locked

fmt:
	uv run ruff format modern tests/core tests/container_smoke.py ../scripts
	uv run ruff check --fix modern tests/core tests/container_smoke.py ../scripts

lint:
	uv run ruff check modern tests/core tests/container_smoke.py ../scripts
	uv run ruff format --check modern tests/core tests/container_smoke.py ../scripts

typecheck:
	uv run pyright modern

test:
	uv run pytest

check: lint typecheck test

run:
	uv run nao-bridge --backend fake
