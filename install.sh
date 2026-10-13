#!/usr/bin/env bash
# Xplogent installer (Linux / macOS). Installs once and puts `xplogent` on PATH.
set -e

# Usage: ./install.sh [--server]
#   (default)  desktop install: everything, including GUI/mic control and local embeddings
#   --server   headless Linux server: pinned lock file, no desktop packages, no torch.
#              For a full systemd + HTTPS deployment use deploy/install-server.sh instead.
MODE=desktop
[ "${1:-}" = "--server" ] && MODE=server

echo "🧠 Installing Xplogent…"

if ! command -v python3 >/dev/null 2>&1; then
  echo "✗ python3 not found. Install Python 3.11+ first." >&2
  exit 1
fi
echo "• Python $(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"

# Prefer pipx (isolated, auto-on-PATH); fall back to a venv.
if command -v pipx >/dev/null 2>&1; then
  echo "• Installing with pipx…"
  if [ "$MODE" = server ]; then pipx install --force ".[server]"; else pipx install --force ".[all]"; fi
  pipx ensurepath >/dev/null 2>&1 || true
  RUN="xplogent"
else
  echo "• pipx not found — using a virtualenv."
  python3 -m venv .venv
  # shellcheck disable=SC1091
  . .venv/bin/activate
  python -m pip install --upgrade pip >/dev/null
  if [ "$MODE" = server ]; then
    pip install -r requirements/server.lock && pip install --no-deps -e .
  else
    pip install -e ".[all]"
  fi
  RUN=".venv/bin/xplogent"
  echo "• Tip: add '$(pwd)/.venv/bin' to your PATH, or use pipx for a global command."
fi

# Build the dashboard if Node is available.
if command -v npm >/dev/null 2>&1; then
  echo "• Building the dashboard…"
  (cd web && npm install --no-audit --no-fund && npm run build)
else
  echo "• Node.js not found — skipping dashboard build (GUI needs 'cd web && npm install && npm run build')."
fi

cat <<DONE

✓ Installed.

Next:
  $RUN setup      # choose your provider/model (one time)
  $RUN start      # run in the background (survives closing the terminal)
  $RUN status     # check it
  # or: $RUN up   to run in the foreground and open the browser
DONE
