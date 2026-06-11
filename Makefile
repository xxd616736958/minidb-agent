.PHONY: help install dev cli test reset-db clean docker-up docker-down

# Default target
help:
	@echo "zuixiaoagent — Terminal Operating Intelligent Agent"
	@echo ""
	@echo "Targets:"
	@echo "  install     Create venv and install all dependencies"
	@echo "  dev         Start LangGraph dev server (http://localhost:2024)"
	@echo "  cli         Start interactive CLI client"
	@echo "  test        Run all tests"
	@echo "  reset-db    Delete local SQLite databases"
	@echo "  clean       Remove venv, caches, and data"
	@echo "  docker-up   Start production stack (Docker Compose)"
	@echo "  docker-down Stop production stack"
	@echo ""
	@echo "Requires Python 3.11+. Use 'make install PYTHON=python3.12' if needed."

# Python executable (override if needed: make install PYTHON=python3.12)
PYTHON := python3
# PyPI mirror (set empty for default, or use Tsinghua: https://pypi.tuna.tsinghua.edu.cn/simple)
PIP_INDEX := https://pypi.tuna.tsinghua.edu.cn/simple

# ── Install ──────────────────────────────────────────────
install:
	@echo "==> Creating virtual environment with $(PYTHON)..."
	$(PYTHON) -m venv .venv
	@echo "==> Installing dependencies..."
	.venv/bin/pip install --upgrade pip $(if $(PIP_INDEX),-i $(PIP_INDEX),) -q
	.venv/bin/pip install -r requirements.txt $(if $(PIP_INDEX),-i $(PIP_INDEX),)
	.venv/bin/pip install -e . $(if $(PIP_INDEX),-i $(PIP_INDEX),)
	@echo "==> Copying .env.example to .env (if not exists)..."
	cp -n .env.example .env 2>/dev/null || true
	@echo ""
	@echo "✅ Installation complete!"
	@echo "   Next: edit .env with your API keys, then run 'make dev'"

# ── Development Server ────────────────────────────────────
dev:
	@echo "==> Starting LangGraph dev server..."
	@mkdir -p data
	@if [ ! -f .env ]; then cp .env.example .env; fi
	@echo "   Server:   http://localhost:2024"
	@echo "   Studio:   https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2024"
	.venv/bin/langgraph dev --host 0.0.0.0 --port 2024 --no-browser

# ── CLI Client ────────────────────────────────────────────
cli:
	@echo "==> Starting CLI client..."
	@if [ ! -f .env ]; then cp .env.example .env; fi
	.venv/bin/python -m cli.main

# ── Testing ───────────────────────────────────────────────
test:
	@echo "==> Running test suite..."
	.venv/bin/pytest tests/ -v --tb=short

# ── Database Reset ────────────────────────────────────────
reset-db:
	@echo "==> Resetting local databases..."
	rm -f data/agent_checkpoints.sqlite
	rm -f data/agent_memory.sqlite
	@echo "✅ Databases reset."

# ── Clean ─────────────────────────────────────────────────
clean:
	@echo "==> Cleaning..."
	rm -rf .venv
	rm -rf __pycache__
	rm -rf .pytest_cache
	rm -rf *.egg-info
	rm -rf data/
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name '*.pyc' -delete 2>/dev/null || true
	@echo "✅ Clean complete."

# ── Docker (Production) ───────────────────────────────────
docker-up:
	@echo "==> Starting Docker Compose stack..."
	docker compose up -d
	@echo "   Server: http://localhost:2024"

docker-down:
	@echo "==> Stopping Docker Compose stack..."
	docker compose down
