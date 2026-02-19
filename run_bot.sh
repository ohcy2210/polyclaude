#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
#  run_bot.sh — Launch wrapper for the Polymarket HFT Bot
# ─────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# ── 1. Check Python (prefer 3.12 — 3.14 has wheel issues) ─────
if command -v python3.12 &>/dev/null; then
    PYTHON="python3.12"
elif command -v python3.13 &>/dev/null; then
    PYTHON="python3.13"
else
    PYTHON="${PYTHON:-python3}"
fi
echo "  Using: $($PYTHON --version)"
if ! command -v "$PYTHON" &>/dev/null; then
    echo "❌  $PYTHON not found. Install Python 3.10+ first."
    exit 1
fi

# ── 2. Virtual-env (persistent, iCloud-excluded via .nosync) ──
VENV_DIR="$SCRIPT_DIR/.venv.nosync"
if [ ! -d "$VENV_DIR" ]; then
    echo "📦  Creating virtual environment (.venv.nosync — iCloud-excluded) …"
    "$PYTHON" -m venv "$VENV_DIR"
    "$VENV_DIR/bin/python" -m pip install -q --upgrade pip
fi
source "$VENV_DIR/bin/activate"

# ── 3. Install deps (skip if unchanged) ───────────────────────
REQ_HASH=$(md5 -q requirements.txt 2>/dev/null || md5sum requirements.txt | cut -d' ' -f1)
HASH_FILE="$VENV_DIR/.req_hash"
if [ ! -f "$HASH_FILE" ] || [ "$(cat "$HASH_FILE")" != "$REQ_HASH" ]; then
    echo "📦  Installing dependencies …"
    python -m pip install -q -r requirements.txt
    echo "$REQ_HASH" > "$HASH_FILE"
else
    echo "✅  Dependencies up to date"
fi

# ── OS Tuning (HFT Network Limits) ────────────────────────────
ulimit -n 65536 2>/dev/null || true

# ── 4. Env-file check ─────────────────────────────────────────
if [ ! -f .env ]; then
    echo "⚠️   No .env file found.  Copy .env.example and fill in your keys:"
    echo "     cp .env.example .env"
    exit 1
fi

# ── 5. Launch ──────────────────────────────────────────────────
echo ""
echo "🚀  Starting Polymarket HFT Bot …"
echo "    Log file: bot.log"
echo "    Press Ctrl-C to stop gracefully."
echo ""
export PYTHONUNBUFFERED=1
export PYTHONOPTIMIZE=1
while true; do
    if command -v caffeinate &>/dev/null; then
        caffeinate -s -i "$PYTHON" bot.py "$@"
    else
        "$PYTHON" bot.py "$@"
    fi
    EXIT_CODE=$?
    echo "⚠️   Bot crashed (exit code $EXIT_CODE). Restarting in 5 seconds..."
    sleep 5
done
