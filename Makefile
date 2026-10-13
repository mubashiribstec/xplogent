.PHONY: install dev build test lint up wheel lock

install:
	pip install -e ".[all]"
	cd web && npm install && npm run build

dev:
	cd web && npm run dev

build:
	cd web && npm run build

test:
	pytest -q

lint:
	ruff check src tests

up:
	xplogent up

# Build a wheel that includes the dashboard (needs Node + `pip install build`).
wheel: build
	rm -rf src/xplogent/web_dist && cp -r web/dist src/xplogent/web_dist
	python -m build --wheel

# Re-generate the pinned server dependency lock (needs uv).
lock:
	uv pip compile pyproject.toml --extra server --python-version 3.12 -o requirements/server.lock
