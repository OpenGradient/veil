.PHONY: install install-pii build publish check test serve

install:
	uv sync --all-groups

# Dev install with the optional PII-redaction stack (Presidio + spaCy) and its
# model. The model is fetched from the release wheel rather than `spacy download`,
# which can be unreliable in some environments.
	uv sync --all-groups --extra pii
	uv pip install https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl

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
	uv run og-veil serve
