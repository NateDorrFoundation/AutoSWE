#!/bin/bash
# setup.sh — autoSWE Interactive Setup Wizard
#
# Runs the first-run setup wizard to configure credentials and settings.
# Creates config/repos.json and config/autoswe.env.
#
# Usage:
#   ./setup.sh          # guided first-run setup
#   ./setup.sh --force  # overwrite existing config without prompting
#   ./setup.sh --no-warmup   # skip the pi-mcp cold-start warm-up

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export AUTOSWE_DIR="${AUTOSWE_DIR:-$SCRIPT_DIR}"
AUTOSWE_PY="$AUTOSWE_DIR/autoswe.py"
PYTHON="${PYTHON:-python3}"

# Prefer venv if present
if [ -f "$AUTOSWE_DIR/.venv/bin/python" ]; then
    PYTHON="$AUTOSWE_DIR/.venv/bin/python"
fi

# Run the setup wizard, preserving any user-supplied flags (e.g. --force).
"$PYTHON" "$AUTOSWE_PY" setup "$@"

# pi Phase 4 cold-start warm-up (docs/autoswe/PLAN-pi-mcp.md): populate each pi
# profile's mcp-cache.json so the first real run uses the direct
# mcp__autoswe_comment_* tools. Skips cleanly (exit 0) when no pi profile with
# an agent_dir is configured, so non-pi setups are unaffected. Skipped entirely
# with --no-warmup.
if [[ " $* " != *" --no-warmup "* ]]; then
    "$PYTHON" "$AUTOSWE_PY" warmup || echo "  pi-mcp warm-up not complete (first pi run will use proxy tool shapes)"
fi

# Antigravity CLI check (Phase 6 §6.2): NO install step — agy is a user-level
# install that needs interactive auth once. Warn-only: never fails setup. There
# is deliberately no warm-up call for agy (spike M5: eager MCP tools work on a
# cold cache; unlike pi, no warm-up is needed).
if grep -Eq '"backend"[[:space:]]*:[[:space:]]*"antigravity"' "$AUTOSWE_DIR/config/harnesses.json" 2>/dev/null; then
    AGY_BIN="$("$PYTHON" - <<'PY'
import json, os, shutil
p = os.path.join(os.environ["AUTOSWE_DIR"], "config", "harnesses.json")
try:
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
except Exception:
    data = {}
for prof in data.values():
    if isinstance(prof, dict) and str(prof.get("backend", "")).lower() == "antigravity":
        cp = str(prof.get("cli_path") or "").strip()
        if cp:
            print(os.path.expanduser(cp))
            break
else:
    print(shutil.which("agy") or "")
PY
)" || AGY_BIN=""
    if [ -n "$AGY_BIN" ] && { command -v "$AGY_BIN" >/dev/null 2>&1 || [ -x "$AGY_BIN" ]; }; then
        AGY_VER="$("$AGY_BIN" --version 2>&1 | head -n 1)" || AGY_VER=""
        echo "  antigravity backend: found ${AGY_BIN}${AGY_VER:+ (${AGY_VER})}"
    else
        echo "  antigravity backend: no agy binary found (PATH or profile cli_path) — install the Antigravity CLI first"
    fi
    echo "  antigravity backend: run \`agy\` interactively once to authenticate (headless dispatch reuses that session; see docs/autoswe/harnesses.md)"
fi
