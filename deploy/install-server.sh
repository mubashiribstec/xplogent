#!/usr/bin/env bash
# Install Xplogent as a hardened systemd service on a Linux server (run as root).
#
#   sudo ./deploy/install-server.sh
#
# Idempotent: safe to re-run after `git pull` to update. It creates a dedicated
# `xplogent` user, installs pinned dependencies from requirements/server.lock into
# /opt/xplogent/.venv, builds the dashboard (if Node.js is present), generates an
# access token (kept on re-runs), and enables the service on 127.0.0.1:8765.
# Put Caddy/nginx in front for HTTPS (see deploy/caddy/Caddyfile, docs/deploy-linux.md).
#
# Options:  --no-start     install but do not start the service
#           --no-systemd   skip all systemd/user steps (used for testing)
# Overrides (env): APP_DIR (/opt/xplogent) DATA_DIR (/var/lib/xplogent) ETC_DIR (/etc/xplogent)
#                  SERVICE_USER (xplogent) PORT (8765) PYTHON (auto)

set -euo pipefail

START=1; SYSTEMD=1
for arg in "$@"; do
  case "$arg" in
    --no-start) START=0 ;;
    --no-systemd) SYSTEMD=0; START=0 ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

APP_DIR="${APP_DIR:-/opt/xplogent}"
DATA_DIR="${DATA_DIR:-/var/lib/xplogent}"
ETC_DIR="${ETC_DIR:-/etc/xplogent}"
SERVICE_USER="${SERVICE_USER:-xplogent}"
PORT="${PORT:-8765}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

say() { printf '• %s\n' "$*"; }
die() { printf '✗ %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root (sudo $0)"
if [ ! -f "$SRC/pyproject.toml" ] || [ ! -f "$SRC/requirements/server.lock" ]; then
  die "run this from a full Xplogent checkout (pyproject.toml / requirements/server.lock missing)"
fi

# --- Python 3.11+ -----------------------------------------------------------------
PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
  for c in python3.13 python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
      PYTHON="$c"; break
    fi
  done
fi
[ -n "$PYTHON" ] || die "Python 3.11+ not found (apt install python3 python3-venv)"
"$PYTHON" -c 'import venv, ensurepip' 2>/dev/null || die "python venv module missing (apt install python3-venv)"
say "Python: $($PYTHON --version)"

# --- service user + directories ----------------------------------------------------
if [ "$SYSTEMD" -eq 1 ] && ! id "$SERVICE_USER" >/dev/null 2>&1; then
  say "Creating system user $SERVICE_USER"
  useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi
mkdir -p "$APP_DIR" "$DATA_DIR" "$ETC_DIR"

# --- code --------------------------------------------------------------------------
if [ "$SRC" != "$(cd "$APP_DIR" && pwd)" ]; then
  say "Syncing code to $APP_DIR"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete --exclude '.venv' --exclude 'web/node_modules' --exclude 'deploy/docker/.env' \
      "$SRC"/ "$APP_DIR"/
  else
    cp -a "$SRC"/. "$APP_DIR"/
  fi
fi

# --- dependencies ------------------------------------------------------------------
say "Creating virtualenv and installing pinned dependencies (a few minutes the first time)"
"$PYTHON" -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/python" -m pip install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements/server.lock"
"$APP_DIR/.venv/bin/pip" install --quiet --no-deps -e "$APP_DIR"

# --- dashboard ---------------------------------------------------------------------
if command -v npm >/dev/null 2>&1; then
  say "Building the dashboard"
  (cd "$APP_DIR/web" && npm ci --no-audit --no-fund --silent && npm run build --silent)
else
  say "Node.js not found - skipping the dashboard build (API still works). Install nodejs/npm and re-run,"
  say "or build elsewhere and copy web/dist to $APP_DIR/web/dist."
fi

# --- environment file (token is generated once and kept) -----------------------------
ENV_FILE="$ETC_DIR/xplogent.env"
if [ ! -f "$ENV_FILE" ]; then
  say "Writing $ENV_FILE"
  TOKEN="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  umask 077
  cat > "$ENV_FILE" <<ENVEOF
# Xplogent server environment. Keep private (contains the access token and API keys).
XPLOGENT_HOME=$DATA_DIR
XPLOGENT_BEHIND_PROXY=1
XPLOGENT_AUTH_TOKEN=$TOKEN
PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring
XPLOGENT_API_PORT=$PORT

# Default model and provider keys (only the ones you use):
#XPLOGENT_MODEL=openai:gpt-4o-mini
#OLLAMA_HOST=http://localhost:11434
#OPENAI_API_KEY=
#ANTHROPIC_API_KEY=
#OPENROUTER_API_KEY=
#TELNYX_API_KEY=
ENVEOF
  NEW_TOKEN="$TOKEN"
else
  say "Keeping existing $ENV_FILE (and its access token)"
  NEW_TOKEN=""
fi

# --- permissions ---------------------------------------------------------------------
if [ "$SYSTEMD" -eq 1 ]; then
  chown -R "$SERVICE_USER":"$SERVICE_USER" "$APP_DIR" "$DATA_DIR"
  chown root:"$SERVICE_USER" "$ENV_FILE"; chmod 640 "$ENV_FILE"
fi

# --- systemd -------------------------------------------------------------------------
if [ "$SYSTEMD" -eq 1 ]; then
  say "Installing systemd unit"
  sed -e "s#/opt/xplogent#$APP_DIR#g" -e "s#/etc/xplogent#$ETC_DIR#g" \
      -e "s#User=xplogent#User=$SERVICE_USER#" -e "s#Group=xplogent#Group=$SERVICE_USER#" \
      -e "s#--port 8765#--port $PORT#" \
      "$APP_DIR/deploy/systemd/xplogent.service" > /etc/systemd/system/xplogent.service
  systemctl daemon-reload
  systemctl enable xplogent >/dev/null 2>&1
  if [ "$START" -eq 1 ]; then
    systemctl restart xplogent
    say "Waiting for the server to come up"
    for _ in $(seq 1 30); do
      if "$APP_DIR/.venv/bin/python" -c "import urllib.request as u; u.urlopen('http://127.0.0.1:$PORT/health', timeout=2)" 2>/dev/null; then
        UP=1; break
      fi
      sleep 1
    done
    [ "${UP:-0}" = 1 ] || die "server did not become healthy - check: journalctl -u xplogent -n 50"
  fi
fi

cat <<DONE

✓ Xplogent installed.

  App:      $APP_DIR          Data: $DATA_DIR          Config: $ENV_FILE
  Listens:  http://127.0.0.1:$PORT  (HTTPS via Caddy/nginx - see deploy/caddy/Caddyfile)
DONE
if [ -n "$NEW_TOKEN" ]; then
  printf '  Dashboard access token (also in %s):\n    %s\n' "$ENV_FILE" "$NEW_TOKEN"
fi
cat <<DONE

Next:
  1. Put HTTPS in front:  DOMAIN=agent.example.com caddy run --config $APP_DIR/deploy/caddy/Caddyfile
  2. Open https://agent.example.com and paste the token. Add provider keys in Settings.
  3. Logs: journalctl -u xplogent -f        Update: git pull, then re-run this script.
DONE
