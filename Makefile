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
	uv run ruff format --check og_local tests
	uv run ruff check og_local tests
	uv run mypy og_local

test:
	uv run pytest -q

serve:
	uv run veil serve
