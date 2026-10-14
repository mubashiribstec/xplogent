#!/usr/bin/env bash
# Xplogent one-command installer for Linux servers (Docker).
#
#   curl -fsSL https://raw.githubusercontent.com/mubashiribstec/xplogent/main/get.sh | sudo bash
#
# Installs Docker if needed, downloads the deploy files (no git, no build), asks a few
# questions (domain or no-domain mode, local Ollama, model + API key), writes a private
# .env with a generated access token, starts Xplogent and installs the `xplogent-docker`
# helper (update, backup, logs, token, ...).
#
# Re-running it updates an existing install and keeps its settings and token.
#
# Options (or the XPL_* environment variables in brackets):
#   --yes                  no questions; use flags/defaults (also when there is no terminal)
#   --mode https|local     https = your domain + automatic HTTPS; local = no domain   [XPL_MODE]
#   --domain NAME          public hostname (implies --mode https)                    [XPL_DOMAIN]
#   --lan                  local mode: listen on all interfaces (default: this machine only)
#   --ollama | --no-ollama run local models with Ollama (GPU used when available)    [XPL_OLLAMA]
#   --model SPEC           default model, e.g. openai:gpt-4o-mini                   [XPL_MODEL]
#   --reconfigure          ask the questions again for an existing install (token kept)
#   --dir PATH             install directory (default /opt/xplogent-docker)          [XPL_DIR]
#   --ref REF              git branch/tag to take the deploy files from (default main) [XPL_REF]
#   --no-docker-install    fail instead of installing Docker
#   --dry-run              write files only; no Docker commands (for testing)
# Testing: XPL_SRC=/path/to/checkout copies the files from a local checkout instead.

set -euo pipefail

REPO="${XPL_REPO:-mubashiribstec/xplogent}"
REF="${XPL_REF:-main}"
DIR="${XPL_DIR:-/opt/xplogent-docker}"
SRC="${XPL_SRC:-}"
MODE="${XPL_MODE:-}"
DOMAIN="${XPL_DOMAIN:-}"
OLLAMA="${XPL_OLLAMA:-}"
MODEL="${XPL_MODEL:-}"
BIND=""
YES=0; DRY=0; INSTALL_DOCKER=1; RECONFIGURE=0
BIN_DIR="${XPL_BIN_DIR:-/usr/local/bin}"

while [ $# -gt 0 ]; do
  case "$1" in
    --yes|-y) YES=1 ;;
    --mode) MODE="${2:?--mode needs https or local}"; shift ;;
    --domain) DOMAIN="${2:?--domain needs a hostname}"; MODE=https; shift ;;
    --lan) BIND=0.0.0.0 ;;
    --ollama) OLLAMA=1 ;;
    --no-ollama) OLLAMA=0 ;;
    --model) MODEL="${2:?--model needs a value}"; shift ;;
    --reconfigure) RECONFIGURE=1 ;;
    --dir) DIR="${2:?--dir needs a path}"; shift ;;
    --ref) REF="${2:?--ref needs a branch or tag}"; shift ;;
    --no-docker-install) INSTALL_DOCKER=0 ;;
    --dry-run) DRY=1 ;;
    -h|--help) sed -n '2,30p' "$0" 2>/dev/null || true; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done

say()  { printf '\033[1;36m•\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m✗\033[0m %s\n' "$*" >&2; exit 1; }

# Questions come from the terminal even though this script arrives on stdin (curl | bash).
TTY=0
if [ "$YES" -eq 0 ] && [ -r /dev/tty ] && { : </dev/tty; } 2>/dev/null; then TTY=1; fi

ask() {  # ask "question" default -> echoes the answer
  local reply=""
  if [ "$TTY" -eq 1 ]; then
    printf '%s ' "$1" >/dev/tty
    IFS= read -r reply </dev/tty || true
  fi
  printf '%s' "${reply:-$2}"
}
ask_secret() {
  local reply=""
  if [ "$TTY" -eq 1 ]; then
    printf '%s ' "$1" >/dev/tty
    IFS= read -rs reply </dev/tty || true
    printf '\n' >/dev/tty
  fi
  printf '%s' "$reply"
}
yes_no() {  # yes_no "question" y|n
  local a
  a="$(ask "$1" "$2")"
  case "$a" in [Yy]*) return 0 ;; *) return 1 ;; esac
}

gen_token() {
  if command -v openssl >/dev/null 2>&1; then openssl rand -hex 32
  else head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; fi
}

# set_env FILE KEY VALUE: replace KEY=... (or append) without sed escaping problems.
set_env() {
  local file="$1" key="$2" tmp
  case "$3" in *$'\n'*) die "value for $key must be a single line" ;; esac
  tmp="$(mktemp "$file.XXXXXX")"
  XPL_V="$3" awk -v k="$key" '
    BEGIN { v = ENVIRON["XPL_V"]; done = 0 }
    index($0, k "=") == 1 { if (!done) print k "=" v; done = 1; next }
    { print }
    END { if (!done) print k "=" v }' "$file" >"$tmp"
  chmod 600 "$tmp"
  mv "$tmp" "$file"
}
get_env() {  # get_env FILE KEY
  awk -v k="$2" 'index($0, k "=") == 1 { print substr($0, length(k) + 2); exit }' "$1"
}

run() { if [ "$DRY" -eq 1 ]; then say "(dry run) $*"; else "$@"; fi; }

# ── checks ─────────────────────────────────────────────────────────────────────
[ "$(uname -s)" = "Linux" ] || die "this installer is for Linux servers (on a desktop use install.sh / install.ps1)"
if [ "$DRY" -eq 0 ] && [ "$(id -u)" -ne 0 ]; then
  die "run as root:  curl -fsSL https://raw.githubusercontent.com/$REPO/$REF/get.sh | sudo bash"
fi
case "$(uname -m)" in
  x86_64|amd64|aarch64|arm64) ;;
  *) die "unsupported CPU architecture $(uname -m) (x86_64 or arm64 needed)" ;;
esac
command -v curl >/dev/null 2>&1 || [ -n "$SRC" ] || die "curl is required (apt install curl)"

echo
printf '\033[1m🧠 Xplogent installer\033[0m  (repo %s, ref %s)\n\n' "$REPO" "$REF"

# ── Docker ────────────────────────────────────────────────────────────────────
if [ "$DRY" -eq 0 ]; then
  if ! command -v docker >/dev/null 2>&1; then
    [ "$INSTALL_DOCKER" -eq 1 ] || die "Docker is not installed (remove --no-docker-install to install it)"
    if [ "$TTY" -eq 1 ] && ! yes_no "Docker is not installed. Install it now with the official script? [Y/n]" y; then
      die "Docker is required: https://docs.docker.com/engine/install/"
    fi
    say "Installing Docker (get.docker.com)…"
    curl -fsSL https://get.docker.com | sh
  fi
  if command -v systemctl >/dev/null 2>&1; then systemctl enable --now docker >/dev/null 2>&1 || true; fi
  docker info >/dev/null 2>&1 || die "the Docker daemon is not running (systemctl start docker)"
  docker compose version >/dev/null 2>&1 \
    || die "the Docker Compose plugin is missing (apt install docker-compose-plugin)"
  ok "Docker $(docker version --format '{{.Server.Version}}' 2>/dev/null || echo ready)"
fi

# ── files ─────────────────────────────────────────────────────────────────────
FILES="deploy/docker/docker-compose.yml deploy/docker/compose.https.yml deploy/docker/compose.local.yml
deploy/docker/.env.example deploy/docker/xplogent-docker deploy/caddy/Caddyfile"
umask 077
mkdir -p "$DIR/deploy/docker" "$DIR/deploy/caddy" "$DIR/backups"
chmod 755 "$DIR" "$DIR/deploy" "$DIR/deploy/docker" "$DIR/deploy/caddy"
say "Fetching deploy files into $DIR"
for f in $FILES; do
  tmp="$DIR/$f.download"
  if [ -n "$SRC" ]; then cp "$SRC/$f" "$tmp"
  else curl -fsSL "https://raw.githubusercontent.com/$REPO/$REF/$f" -o "$tmp" \
         || die "could not download $f (is the repository public and the ref '$REF' correct?)"
  fi
  chmod 644 "$tmp"
  mv "$tmp" "$DIR/$f"
done
chmod 755 "$DIR/deploy/docker/xplogent-docker"
printf 'REPO=%s\nREF=%s\n' "$REPO" "$REF" >"$DIR/.install-info"

COMPOSE_DIR="$DIR/deploy/docker"
ENV_FILE="$COMPOSE_DIR/.env"

# ── settings ──────────────────────────────────────────────────────────────────
FRESH=1
if [ -f "$ENV_FILE" ] && [ -n "$(get_env "$ENV_FILE" XPLOGENT_AUTH_TOKEN)" ]; then
  FRESH=0
  ok "Existing install found: keeping its settings and access token"
fi

if [ "$FRESH" -eq 1 ] || [ "$RECONFIGURE" -eq 1 ]; then
  [ -f "$ENV_FILE" ] || cp "$COMPOSE_DIR/.env.example" "$ENV_FILE"
  chmod 600 "$ENV_FILE"

  if [ -z "$MODE" ]; then
    echo
    echo "How should people reach Xplogent?"
    echo "  - with a domain (e.g. agent.example.com) pointing at this server: automatic HTTPS"
    echo "  - without a domain: plain HTTP on port 8765 (this machine or your LAN)"
    DOMAIN="$(ask "Domain name (leave empty for no-domain mode):" "$DOMAIN")"
    if [ -n "$DOMAIN" ]; then MODE=https; else MODE=local; fi
  fi
  case "$MODE" in
    https)
      [ -n "$DOMAIN" ] || DOMAIN="$(ask "Domain name:" "")"
      [ -n "$DOMAIN" ] || die "https mode needs a domain (--domain agent.example.com)"
      case "$DOMAIN" in *[!A-Za-z0-9.-]*|.*|*.) die "not a valid hostname: $DOMAIN" ;; esac
      set_env "$ENV_FILE" COMPOSE_FILE "docker-compose.yml:compose.https.yml"
      set_env "$ENV_FILE" DOMAIN "$DOMAIN"
      if [ "$DRY" -eq 0 ] && command -v getent >/dev/null 2>&1; then
        resolved="$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk 'NR==1{print $1}')"
        public="$(curl -fsS -m 5 https://api.ipify.org 2>/dev/null || true)"
        if [ -z "$resolved" ]; then
          warn "$DOMAIN does not resolve yet: create a DNS A record pointing at ${public:-this server}. HTTPS starts working once it does."
        elif [ -n "$public" ] && [ "$resolved" != "$public" ]; then
          warn "$DOMAIN points at $resolved but this server's public IP looks like $public; the certificate will fail until DNS is fixed."
        fi
      fi
      ;;
    local)
      if [ -z "$BIND" ]; then
        if yes_no "Allow other machines on your network to open it (plain HTTP)? [y/N]" n; then BIND=0.0.0.0; else BIND=127.0.0.1; fi
      fi
      set_env "$ENV_FILE" COMPOSE_FILE "docker-compose.yml:compose.local.yml"
      set_env "$ENV_FILE" XPLOGENT_BIND "$BIND"
      ;;
    *) die "--mode must be https or local" ;;
  esac

  if [ -z "$OLLAMA" ]; then
    if yes_no "Run local models with Ollama on this server (needs ~8 GB RAM; GPU used if present)? [y/N]" n; then OLLAMA=1; else OLLAMA=0; fi
  fi
  profile=""
  if [ "$OLLAMA" = 1 ]; then
    profile=ollama
    if command -v nvidia-smi >/dev/null 2>&1 && { [ "$DRY" -eq 1 ] || docker info 2>/dev/null | grep -qi nvidia; }; then
      profile=ollama-gpu; say "NVIDIA GPU + container toolkit found: Ollama will use the GPU"
    elif command -v nvidia-smi >/dev/null 2>&1; then
      warn "NVIDIA GPU found but not the NVIDIA Container Toolkit; Ollama will use the CPU. Install the toolkit and run: xplogent-docker ollama on --gpu"
    fi
    set_env "$ENV_FILE" COMPOSE_PROFILES "$profile"
    set_env "$ENV_FILE" OLLAMA_HOST "http://ollama:11434"
    [ -n "$MODEL" ] || MODEL="ollama:llama3.1"
  else
    set_env "$ENV_FILE" COMPOSE_PROFILES ""
  fi

  if [ -z "$MODEL" ]; then
    MODEL="$(ask "Default model (e.g. openai:gpt-4o-mini, anthropic:claude-sonnet-4-6, gemini:gemini-2.5-flash, openrouter:...):" "openai:gpt-4o-mini")"
  fi
  set_env "$ENV_FILE" XPLOGENT_MODEL "$MODEL"
  case "${MODEL%%:*}" in
    openai) key_var=OPENAI_API_KEY ;;
    anthropic) key_var=ANTHROPIC_API_KEY ;;
    gemini) key_var=GEMINI_API_KEY ;;
    openrouter) key_var=OPENROUTER_API_KEY ;;
    *) key_var="" ;;
  esac
  if [ -n "$key_var" ] && [ -z "$(get_env "$ENV_FILE" "$key_var")" ]; then
    key="${!key_var:-}"
    [ -n "$key" ] || key="$(ask_secret "$key_var (input hidden; leave empty to add it later in Settings):")"
    [ -z "$key" ] || set_env "$ENV_FILE" "$key_var" "$key"
  fi

  if [ -z "$(get_env "$ENV_FILE" XPLOGENT_AUTH_TOKEN)" ]; then
    set_env "$ENV_FILE" XPLOGENT_AUTH_TOKEN "$(gen_token)"
  fi
fi
chmod 600 "$ENV_FILE"

# ── helper command ─────────────────────────────────────────────────────────────
mkdir -p "$BIN_DIR"
helper="$BIN_DIR/xplogent-docker"
tmp="$(mktemp "$helper.XXXXXX")"
XPL_D="$DIR" awk 'BEGIN { d = ENVIRON["XPL_D"] }
  /^DEFAULT_DIR=/ { print "DEFAULT_DIR=\"" d "\""; next } { print }' "$COMPOSE_DIR/xplogent-docker" >"$tmp"
chmod 755 "$tmp"
mv "$tmp" "$helper"
ok "Installed the helper: $helper"

# ── start ─────────────────────────────────────────────────────────────────────
cd "$COMPOSE_DIR"
say "Pulling images…"
if [ "$DRY" -eq 0 ] && ! docker compose pull --quiet; then
  # Not fatal when the image is already on this machine (offline re-run, local build).
  docker image inspect "$(get_env "$ENV_FILE" XPLOGENT_IMAGE)" >/dev/null 2>&1 \
    || die "could not download the Xplogent image; check the network (or that the GHCR package is public)"
  warn "image pull failed; using the copy already on this machine"
fi
[ "$DRY" -eq 1 ] && run docker compose pull --quiet
say "Starting Xplogent…"
run docker compose up -d --remove-orphans
if [ "$DRY" -eq 0 ]; then
  healthy=0
  for _ in $(seq 1 60); do
    if docker compose exec -T xplogent python -c \
        "import urllib.request as u; u.urlopen('http://127.0.0.1:8765/health', timeout=3)" >/dev/null 2>&1; then
      healthy=1; break
    fi
    sleep 2
  done
  [ "$healthy" -eq 1 ] || die "Xplogent did not become healthy; see: xplogent-docker logs"
  ok "Xplogent is running"
  profiles="$(get_env "$ENV_FILE" COMPOSE_PROFILES)"
  case ",$profiles," in
    *,ollama*)
      say "Downloading the local models (first time only; this can take a while)…"
      for m in "$(get_env "$ENV_FILE" XPLOGENT_MODEL | sed 's/^ollama://')" nomic-embed-text; do
        docker compose exec -T ollama ollama pull "$m" || warn "could not pull $m; later: xplogent-docker pull-model $m"
      done ;;
  esac
fi

# ── summary ───────────────────────────────────────────────────────────────────
token="$(get_env "$ENV_FILE" XPLOGENT_AUTH_TOKEN)"
files="$(get_env "$ENV_FILE" COMPOSE_FILE)"
case "$files" in
  *https*) url="https://$(get_env "$ENV_FILE" DOMAIN)"; ports="80,443" ;;
  *)
    bind="$(get_env "$ENV_FILE" XPLOGENT_BIND)"; port="$(get_env "$ENV_FILE" XPLOGENT_PORT)"
    host=127.0.0.1
    if [ "$bind" = "0.0.0.0" ]; then host="$(hostname -I 2>/dev/null | awk '{print $1}')"; host="${host:-<server-ip>}"; fi
    url="http://$host:${port:-8765}"; ports="${port:-8765}" ;;
esac

echo
ok "Done."
echo
echo "  Open:          $url"
echo "  Access token:  $token"
echo "                 (shown again any time with: xplogent-docker token)"
echo
echo "  Manage:        xplogent-docker status | logs | update | backup | token --rotate | help"
echo "  Settings:      $ENV_FILE  (then: xplogent-docker restart)"
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  echo
  warn "The ufw firewall is on. If needed: ufw allow ${ports//,/ && ufw allow }"
fi
case "$files" in
  *local*)
    echo
    echo "  No-domain mode serves plain HTTP. Don't expose it to the internet; to add HTTPS later:"
    echo "  point a domain at this server and run: xplogent-docker https your.domain.com" ;;
esac
echo
