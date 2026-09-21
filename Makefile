.DEFAULT_GOAL := help
PY ?= python

.PHONY: help install run router cluster cli test cov lint format typecheck check docker-build up down clean

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-14s %s\n", $$1, $$2}'

install:  ## Install the package with dev dependencies (editable)
	$(PY) -m pip install -e ".[dev]"
	pre-commit install

run:  ## Run a single shard (HTTP :8000, TCP :6379)
	$(PY) -m kvstore

cluster:  ## Run 3 shards + router locally
	$(PY) scripts/run_cluster.py

cli:  ## Open the CLI against the router
	$(PY) -m kvstore.cli --port 7000

test:  ## Run the test suite
	$(PY) -m pytest

cov:  ## Run tests with coverage (fails under 80%)
	$(PY) -m pytest --cov --cov-report=term-missing

lint:  ## Lint and check formatting
	ruff check .
	ruff format --check .

format:  ## Auto-fix lint issues and format
	ruff check --fix .
	ruff format .

typecheck:  ## Static type check (mypy --strict)
	mypy

check: lint typecheck cov  ## Everything CI runs

docker-build:  ## Build the container image
	docker build -t kvstore:dev .

up:  ## Start the 3-shard cluster in Docker
	docker compose up --build -d

down:  ## Stop the Docker cluster
	docker compose down

clean:  ## Remove caches and local data
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov dist build data
