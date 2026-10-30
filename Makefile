.PHONY: setup run chat test lint

setup:
	uv sync --group dev

run:
	uv run nicole stage

chat:
	uv run nicole

test:
	uv run --group dev pytest -q

lint:
	uv run --group dev ruff check .
	uv run --group dev ruff format --check .
