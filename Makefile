PY ?= python3
DASHBOARD ?= home

.PHONY: setup uninstall serve stop serve-local preview test

setup: ## plug in the Kobo, tap Connect, then run this
	@$(PY) cli/kobodash.py setup

uninstall:
	@$(PY) cli/kobodash.py uninstall

serve: ## the dashboard server, in Docker
	docker compose up -d --build

stop:
	docker compose down

serve-local: .venv ## the dashboard server, without Docker (needs Chromium)
	set -a; [ ! -f .env ] || . ./.env; set +a; .venv/bin/python server/kobodash.py

preview: .venv ## render dashboards/$(DASHBOARD) with its sample data to preview.png
	.venv/bin/python server/kobodash.py --preview $(DASHBOARD) --sample --out preview.png

test: .venv
	.venv/bin/python -m unittest discover -s tests

.venv: server/requirements.txt
	$(PY) -m venv .venv && .venv/bin/pip install -q -r server/requirements.txt && touch .venv
