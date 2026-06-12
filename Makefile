.PHONY: install build publish check test serve

install:
	uv sync --all-groups

build:
	uv build

publish:
	@echo "Current version:" $$(grep 'version = ' pyproject.toml | head -1 | cut -d'"' -f2)
	rm -rf dist/*
	uv build
	uv publish

check:
	uv run ruff format --check veil tests
	uv run ruff check veil tests
	uv run mypy veil

test:
	uv run pytest -q

serve:
	uv run veil serve
