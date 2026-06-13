#!/usr/bin/env bash
# ============================================================
# MiniDB Agent — One-Click Startup Script
# ============================================================
# Usage:
#   ./scripts/start.sh              # Interactive mode
#   ./scripts/start.sh dev          # Development server
#   ./scripts/start.sh cli          # CLI client
#   ./scripts/start.sh install      # Install only
#   ./scripts/start.sh test         # Run tests
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
VENV_DIR="$PROJECT_DIR/.venv"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

log()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()  { echo -e "${RED}[ERROR]${NC} $*"; }
header() { echo -e "\n${BLUE}═══ $* ═══${NC}\n"; }

# ── Check prerequisites ──────────────────────────────────────

check_prereqs() {
    header "Checking prerequisites"

    # Python 3.11+
    if command -v python3 &>/dev/null; then
        PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
        log "Python $PY_VERSION found"
        if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
            log "Python version OK (3.11+)"
        else
            err "Python 3.11+ required, found $PY_VERSION"
            exit 1
        fi
    else
        err "python3 not found. Install Python 3.11+ first."
        exit 1
    fi

    log "All prerequisites met."
}

# ── Setup virtual environment ────────────────────────────────

setup_venv() {
    if [ ! -d "$VENV_DIR" ]; then
        header "Creating virtual environment"
        python3 -m venv "$VENV_DIR"
        log "Virtual environment created at $VENV_DIR"
    else
        log "Virtual environment already exists"
    fi

    # Activate
    source "$VENV_DIR/bin/activate"

    # Upgrade pip
    header "Upgrading pip"
    pip install --upgrade pip -q

    # Install dependencies
    header "Installing dependencies"
    pip install -r "$PROJECT_DIR/requirements.txt" -q
    pip install -e "$PROJECT_DIR" -q

    log "Dependencies installed successfully"
}

# ── Setup environment ────────────────────────────────────────

setup_env() {
    if [ ! -f "$PROJECT_DIR/.env" ]; then
        header "Setting up environment"
        cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
        warn ".env created from .env.example"
        warn "Please edit .env and add your API key:"
        echo ""
        echo "  vim .env"
        echo ""
        echo "Required:"
        echo "  - DEEPSEEK_API_KEY=sk-...  # when LLM_PROVIDER=deepseek"
        echo "  - OPENAI_API_KEY=sk-...    # when LLM_PROVIDER=openai"
        echo ""
        echo "Recommended:"
        echo "  - LANGSMITH_API_KEY=ls__..."
        echo ""
        read -rp "Press Enter after configuring .env, or Ctrl+C to exit... "
    else
        log ".env already exists"
    fi

    # Export env vars
    set -a
    source "$PROJECT_DIR/.env"
    set +a

    # Check critical vars
    LLM_PROVIDER_VALUE="${LLM_PROVIDER:-deepseek}"
    if [ "$LLM_PROVIDER_VALUE" = "deepseek" ]; then
        if [ -z "${DEEPSEEK_API_KEY:-}" ] || [[ "${DEEPSEEK_API_KEY:-}" == "sk-xxxxxxxx"* ]]; then
            err "DEEPSEEK_API_KEY not configured in .env"
            err "Please edit .env and set a valid DeepSeek API key"
            exit 1
        fi
    elif [ "$LLM_PROVIDER_VALUE" = "openai" ]; then
        if [ -z "${OPENAI_API_KEY:-}" ] || [[ "${OPENAI_API_KEY:-}" == "sk-xxxxxxxx"* ]]; then
            err "OPENAI_API_KEY not configured in .env"
            err "Please edit .env and set a valid OpenAI API key"
            exit 1
        fi
    else
        err "Unsupported LLM_PROVIDER: $LLM_PROVIDER_VALUE"
        err "Supported providers: deepseek, openai"
        exit 1
    fi
}

# ── Create data directories ──────────────────────────────────

create_dirs() {
    mkdir -p "$PROJECT_DIR/data"
    log "Data directory ready"
}

# ── Start development server ─────────────────────────────────

start_dev() {
    header "Starting LangGraph Development Server"
    source "$VENV_DIR/bin/activate"

    echo ""
    echo -e "  ${CYAN}Server:${NC}   http://127.0.0.1:2024"
    echo -e "  ${CYAN}Studio:${NC}   https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2024"
    echo -e "  ${CYAN}Health:${NC}  http://127.0.0.1:2024/health"
    echo ""

    langgraph dev --host 0.0.0.0 --port 2024 --no-browser
}

# ── Start CLI client ─────────────────────────────────────────

start_cli() {
    header "Starting CLI Client"
    source "$VENV_DIR/bin/activate"

    minidb-agent "$@"
}

# ── Run tests ────────────────────────────────────────────────

run_tests() {
    header "Running Tests"
    source "$VENV_DIR/bin/activate"

    pytest "$PROJECT_DIR/tests/" -v --tb=short "$@"
}

# ── Show status ──────────────────────────────────────────────

show_status() {
    header "Environment Status"

    echo "Project:  $PROJECT_DIR"
    echo "Python:   $(python3 --version)"
    echo "venv:     $([ -d "$VENV_DIR" ] && echo '✓ exists' || echo '✗ missing')"
    echo ".env:     $([ -f "$PROJECT_DIR/.env" ] && echo '✓ exists' || echo '✗ missing')"
    echo ""

    if [ -f "$PROJECT_DIR/.env" ]; then
        echo "Configuration:"
        grep -E '^(LLM_MODEL|LANGSMITH_TRACING|AGENT_LOG_LEVEL|MAX_RETRIES)=' "$PROJECT_DIR/.env" 2>/dev/null || true
    fi
}

# ── Main ─────────────────────────────────────────────────────

main() {
    cd "$PROJECT_DIR"

    case "${1:-}" in
        dev|server)
            check_prereqs
            setup_venv
            setup_env
            create_dirs
            start_dev
            ;;
        cli)
            check_prereqs
            setup_venv
            setup_env
            shift
            start_cli "$@"
            ;;
        install)
            check_prereqs
            setup_venv
            setup_env
            create_dirs
            log "Installation complete. Run './scripts/start.sh dev' to start."
            ;;
        test)
            check_prereqs
            setup_venv
            setup_env
            shift
            run_tests "$@"
            ;;
        status|info)
            check_prereqs
            show_status
            ;;
        *)
            echo ""
            echo "MiniDB Agent — One-Click Startup"
            echo ""
            echo "Usage: $0 <command>"
            echo ""
            echo "Commands:"
            echo "  dev       Start LangGraph development server"
            echo "  cli       Start interactive CLI client"
            echo "  install   Install dependencies only"
            echo "  test      Run test suite"
            echo "  status    Show environment status"
            echo ""
            echo "Examples:"
            echo "  $0 dev              # Start server"
            echo "  $0 cli              # Start CLI (local)"
            echo "  $0 cli --url https://agent.example.com  # CLI (remote)"
            echo "  $0 test -k test_shell  # Run specific tests"
            ;;
    esac
}

main "$@"
