#!/usr/bin/env bash
# First run in one command (macOS/Linux): Ollama -> Gemma 4 -> the sample receipts -> the app in your browser.
#   bash scripts/first_run.sh
#   JEONGSAN_MODEL=gemma4:e4b bash scripts/first_run.sh   # pick the model yourself
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${PORT:-8000}"
OLLAMA_URL="${OLLAMA_URL:-http://127.0.0.1:11434}"
MODEL="${JEONGSAN_MODEL:-}"

step() { printf '\n▶ %s\n' "$*"; }
ollama_up() { curl -fsS "$OLLAMA_URL/api/version" >/dev/null 2>&1; }

# 1. Ollama
if ! command -v ollama >/dev/null 2>&1; then
  if [[ "$(uname)" == "Darwin" ]] && command -v brew >/dev/null 2>&1; then
    step "Installing Ollama (brew)"
    brew install ollama
  else
    echo "Ollama is missing. Install it from https://ollama.com/download and run this again."
    exit 1
  fi
fi
if ! ollama_up; then
  step "Starting Ollama"
  if [[ "$(uname)" == "Darwin" && -d /Applications/Ollama.app ]]; then
    open -a Ollama
  else
    nohup ollama serve >first-run-ollama.log 2>&1 &
  fi
  for _ in $(seq 1 30); do ollama_up && break; sleep 1; done
  ollama_up || { echo "Ollama does not answer. See first-run-ollama.log."; exit 1; }
fi

# 2. Model: the 12B reads small print noticeably better if there's room for it.
if [[ -z "$MODEL" ]]; then
  if [[ "$(uname)" == "Darwin" ]]; then
    mem_gb=$(( $(sysctl -n hw.memsize) / 1024 / 1024 / 1024 ))
  else
    mem_gb=$(( $(awk '/MemTotal/ {print $2}' /proc/meminfo) / 1024 / 1024 ))
  fi
  if (( mem_gb >= 16 )); then MODEL="gemma4:12b"; else MODEL="gemma4:e4b"; fi
  step "${mem_gb} GB of memory, so using $MODEL"
fi
step "Pulling $MODEL (first time only; it is several GB)"
ollama pull "$MODEL"

# 3. uv
if ! command -v uv >/dev/null 2>&1; then
  step "Installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

# 4. Lazy mode on the sample receipts: one folder in, one Excel file out.
step "Reading the sample receipts in samples/"
JEONGSAN_MODEL="$MODEL" OLLAMA_URL="$OLLAMA_URL" uv run python -m app.cli samples --out first-run-report.xlsx

# 5. The app
step "Starting Jeongsan on port $PORT"
JEONGSAN_MODEL="$MODEL" OLLAMA_URL="$OLLAMA_URL" uv run uvicorn app.main:app --port "$PORT" >first-run-server.log 2>&1 &
server_pid=$!
trap 'kill "$server_pid" 2>/dev/null || true' EXIT INT TERM
for _ in $(seq 1 60); do curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break; sleep 1; done
curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 || { echo "The app did not start. See first-run-server.log."; exit 1; }

url="http://localhost:$PORT"
if command -v open >/dev/null 2>&1; then open "$url"; elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$url"; fi
step "Open $url and drop your receipt photos in, or press 'Try the sample receipts'. Ctrl+C stops the app."
wait "$server_pid"
