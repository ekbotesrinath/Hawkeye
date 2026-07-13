.DEFAULT_GOAL := help
PYTHON := py -3.12
VENV := .venv
VENV_PYTHON := $(VENV)\Scripts\python
VENV_PIP := $(VENV)\Scripts\pip
VENV_PYTEST := $(VENV)\Scripts\pytest
VENV_RUFF := $(VENV)\Scripts\ruff
VENV_MYPY := $(VENV)\Scripts\mypy

.PHONY: help setup test test-cov lint audit server connect status clean

help: ## Print available commands
	@echo.
	@echo ╔════════════════════════════════════════════════════╗
	@echo ║          HAWKEYE — Pipeline Observability          ║
	@echo ╚════════════════════════════════════════════════════╝
	@echo.
	@echo Available commands:
	@echo   make setup     — Create .venv, install deps + dev deps, verify Python 3.12
	@echo   make test      — Run full test suite with pytest -v --tb=short
	@echo   make test-cov  — Run tests with coverage report, fail under 80%%
	@echo   make lint      — Run ruff check + mypy
	@echo   make server    — Start MCP server (python main.py)
	@echo   make connect   — Interactive first-time Harness connection setup
	@echo   make status    — Show active profile, account, connectivity check
	@echo   make clean     — Remove .venv and __pycache__

setup: ## Create .venv, install deps + dev deps
	$(PYTHON) -m venv $(VENV)
	$(VENV_PIP) install --upgrade pip
	$(VENV_PIP) install -e ".[dev]"
	$(VENV_PYTHON) --version

test: ## Run full test suite
	$(VENV_PYTEST) tests/ -v --tb=short

test-cov: ## Run tests with coverage, fail under 80%
	$(VENV_PYTEST) tests/ --cov=. --cov-report=term-missing --cov-fail-under=80

lint: ## Run ruff check + mypy (full repo)
	$(VENV_RUFF) check .
	$(VENV_MYPY) core/ providers/ config/ tools/ main.py --ignore-missing-imports

audit: ## Audit dependencies for known vulnerabilities
	$(VENV)\Scripts\pip-audit

server: ## Start MCP server
	$(VENV_PYTHON) main.py

connect: ## Interactive first-time Harness connection setup
	$(VENV_PYTHON) -m config.connect

status: ## Show active profile and connectivity
	$(VENV_PYTHON) -m config.status

clean: ## Remove .venv and __pycache__
	if exist $(VENV) rmdir /s /q $(VENV)
	for /d /r . %%d in (__pycache__) do @if exist "%%d" rmdir /s /q "%%d"
