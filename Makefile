# One-command workflow. Needs only `uv` (https://docs.astral.sh/uv/) - it installs Python 3.12 and every library.
PORT ?= 8000

.PHONY: help dev install env test lint tick daily train models seed clean

help:
	@echo "make dev      install everything, seed + train demo data, start API + UI, open browser"
	@echo "              (make dev RELOAD=1 restarts on code changes; PORT=9000 changes the port)"
	@echo "make test     run the test suite (no API key needed)"
	@echo "make lint     ruff checks"
	@echo "make tick     run due follow-ups once   (cron: every 5 min)"
	@echo "make daily    retrain + analytics + insights (cron: once a day)"
	@echo "make train    relearn every company's data now (catalog, FAQ, intents, lead scoring)"
	@echo "make models   list models available from the configured LLM provider"
	@echo "make seed     create the demo tenant and print its API key"
	@echo "make clean    delete the local SQLite database and trained models"

dev: install env
	uv run python -m salescore dev --port $(PORT) $(if $(RELOAD),--reload,)

install:
	uv sync

env:
	uv run python -c "import pathlib, shutil; p = pathlib.Path('.env'); p.exists() or shutil.copy('.env.example', p)"

test: install
	uv run pytest -q

lint:
	uv run --with ruff ruff check salescore tests

tick: install
	uv run python -m salescore tick

daily: install
	uv run python -m salescore tick --daily

train: install
	uv run python -m salescore train

models: install
	uv run python -m salescore models

seed: install
	uv run python -m salescore seed

clean:
	uv run python -m salescore clean
