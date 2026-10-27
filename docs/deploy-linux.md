# Deploying Xplogent on a Linux server

Three supported ways. All of them **refuse to run without an access token**.

| | One command (Docker) | Docker Compose by hand | systemd on the host |
|---|---|---|---|
| Best for | most people | custom setups, existing Docker hosts | no Docker |
| Command | `curl … get.sh \| sudo bash` | `docker compose up -d` | `sudo ./deploy/install-server.sh` |
| HTTPS | built in (Caddy) or no-domain mode | same | add Caddy yourself |
| Data | Docker volume `xplogent_xplogent-data` | same | `/var/lib/xplogent` |
| Update | `xplogent-docker update` | `docker compose pull && docker compose up -d` | `git pull && sudo ./deploy/install-server.sh` |

## Before you start

- A Linux server (x86_64 or arm64; Debian/Ubuntu tested) with at least 2 GB RAM
  (8 GB+ if you run local models with Ollama).
- **With a domain (recommended for anything reachable from the internet):** a DNS **A record**
  for your hostname (for example `agent.example.com`) pointing at the server, and ports
  **80 and 443** open. Caddy uses them to get the HTTPS certificate.
- **Without a domain:** Xplogent serves plain HTTP on port 8765, either only on the server itself
  (reach it through an SSH tunnel: `ssh -L 8765:127.0.0.1:8765 you@server`) or on your LAN.
  Don't expose no-domain mode to the internet.
- For the phone agent: the carrier needs a public `https://` address, so use a domain.

## Option 1: one command (Docker)

```bash
curl -fsSL https://raw.githubusercontent.com/mubashiribstec/xplogent/main/get.sh | sudo bash
```

The installer:

1. installs Docker Engine + the Compose plugin if they are missing (official get.docker.com script);
2. downloads only the deploy files into `/opt/xplogent-docker` (no git, no build);
3. asks: **domain** (empty = no-domain mode, then whether to allow your LAN), **local Ollama**
   (GPU used automatically when the NVIDIA Container Toolkit is installed), **model** and its
   **API key** (optional; can be set later in Settings);
4. writes `/opt/xplogent-docker/deploy/docker/.env` (readable by root only) with a random
   access token, pulls the prebuilt image `ghcr.io/mubashiribstec/xplogent`, starts it and waits
   until it is healthy;
5. prints the address and the token, and installs the `xplogent-docker` command.

Running it again updates the install and keeps your settings and token
(`--reconfigure` asks the questions again).

Without questions (scripts, cloud-init):

```bash
curl -fsSL https://raw.githubusercontent.com/mubashiribstec/xplogent/main/get.sh \
  | sudo OPENAI_API_KEY=sk-... bash -s -- --yes --domain agent.example.com --model openai:gpt-4o-mini
# no domain, reachable on the LAN, local models:
curl -fsSL https://raw.githubusercontent.com/mubashiribstec/xplogent/main/get.sh | sudo bash -s -- --yes --mode local --lan --ollama
```

Options: `--mode https|local`, `--domain NAME`, `--lan`, `--ollama` / `--no-ollama`,
`--model SPEC`, `--speech` / `--no-speech`, `--backup-model [MODEL]` (local Ollama that answers
when the main model can't; default `llama3.2:3b`, fine on a CPU with 8 GB RAM), `--reconfigure`,
`--dir PATH`, `--ref BRANCH_OR_TAG`, `--no-docker-install`, `--dry-run`.

### Managing it: `xplogent-docker`

| Command | What it does |
|---|---|
| `xplogent-docker status` | containers, health, address |
| `xplogent-docker logs [-f] [service]` | logs (default service: xplogent) |
| `xplogent-docker update` | backup, refresh the deploy files, pull the new image, restart (rolls the files back if the pull fails) |
| `xplogent-docker backup` | consistent backup incl. encrypted API keys into `/opt/xplogent-docker/backups` (newest 10 kept) |
| `xplogent-docker restore FILE` | restore a backup (overwrites current data) |
| `xplogent-docker token [--rotate]` | show the access token, or replace it (the old one stops working) |
| `xplogent-docker config` | edit `.env` in your editor, then apply |
| `xplogent-docker https DOMAIN [--force]` | switch to HTTPS with a free Let's Encrypt certificate: checks DNS and ports first, waits for the certificate, says why if it fails, and sets the phone agent's public address |
| `xplogent-docker local [--lan]` | switch to no-domain mode |
| `xplogent-docker ollama on [--gpu] \| off` | run local models (then `pull-model llama3.1`) |
| `xplogent-docker speech on [--gpu] \| off` | local voices for the phone agent (Kokoro) |
| `xplogent-docker backup-model on [model] \| off` | small local model that answers when the main one can't (default `llama3.2:3b`) |
| `xplogent-docker sip on [--ip ADDR] \| off` | open UDP 5060 + 10000-10100 for the built-in SIP trunk client |
| `xplogent-docker restart \| stop \| start` | |
| `xplogent-docker uninstall [--purge]` | remove containers; `--purge` also deletes all data |

### HTTPS on a home or office connection

Let's Encrypt proves you own the domain by connecting to it on **port 80** (or 443). A forward of
another port (such as 8765) alone can't get a certificate.

1. At your DNS provider, add an **A record**: the host (e.g. `xplogent` for
   `xplogent.example.com`) → your public IP.
2. On your router, forward **TCP 80** and **TCP 443** (UDP 443 optional) to the server's local IP.
3. `sudo xplogent-docker https xplogent.example.com`
4. Open `https://xplogent.example.com`. You can now remove the old 8765 forward: in HTTPS mode the
   dashboard is only served through Caddy on 443. The browser microphone (test calls) needs
   this HTTPS address.

Caddy renews the certificate by itself (port 80 must stay forwarded). If your provider blocks
port 80, ask them to open it, or use a DNS challenge with your own Caddy build (not built in).

Pin a version for production: set `XPLOGENT_IMAGE=ghcr.io/mubashiribstec/xplogent:1.2.3` in `.env`
(`xplogent-docker config`), then `xplogent-docker update`.

## Option 1b: Docker Compose by hand

```bash
git clone https://github.com/mubashiribstec/xplogent.git && cd xplogent/deploy/docker
cp .env.example .env
nano .env        # XPLOGENT_AUTH_TOKEN (openssl rand -hex 32), mode, DOMAIN, keys
docker compose up -d
docker compose logs -f xplogent
```

The base `docker-compose.yml` only defines the app. `COMPOSE_FILE` in `.env` picks how it is
reached, and `COMPOSE_PROFILES` adds extras (docker compose reads both from `.env`):

| Setting | Meaning |
|---|---|
| `COMPOSE_FILE=docker-compose.yml:compose.https.yml` | Caddy on 80/443 with automatic HTTPS for `DOMAIN` |
| `COMPOSE_FILE=docker-compose.yml:compose.local.yml` | port `XPLOGENT_PORT` (8765) on `XPLOGENT_BIND` (127.0.0.1, or 0.0.0.0 for LAN) |
| `COMPOSE_PROFILES=ollama` / `ollama-gpu` | local models; set `OLLAMA_HOST=http://ollama:11434` |
| `COMPOSE_PROFILES=gpu-speech` | Whisper + Kokoro speech servers for the phone agent |

Compose pulls the prebuilt image; `docker compose build` (or `up --build`) builds it from your
checkout instead. Compose refuses to start without `XPLOGENT_AUTH_TOKEN` (and, in https mode, `DOMAIN`).

### Local models (Ollama)

```bash
xplogent-docker ollama on            # or: on --gpu   (needs the NVIDIA Container Toolkit)
xplogent-docker pull-model llama3.1
xplogent-docker pull-model nomic-embed-text   # embeddings for memory/knowledge search
```

Then choose `ollama:llama3.1` as the model in Settings (or `XPLOGENT_MODEL` in `.env`).

### GPU speech for the phone agent

```bash
# in .env: COMPOSE_PROFILES=gpu-speech   (comma-separate to combine, e.g. ollama-gpu,gpu-speech)
xplogent-docker restart
```

This starts a Whisper speech-to-text server and a Kokoro text-to-speech server, both with
OpenAI-compatible APIs. Then add this to `config.yaml` in the data directory (`/data/config.yaml`
inside the container; `docker compose exec xplogent sh -c 'cat >> /data/config.yaml'`), and restart:

```yaml
voip:
  stt: { provider: openai-compatible, base_url: "http://whisper:8000/v1", model: "Systran/faster-whisper-small.en" }
  tts: { base_url: "http://kokoro:8880/v1", model: kokoro, voice: af_heart }
```

The Whisper image has been renamed upstream (faster-whisper-server is now "speaches"); if the
default image fails to pull, set `WHISPER_IMAGE` in `.env` to the current one from its README.
Pin a Kokoro release tag for production.

### The prebuilt image

`.github/workflows/docker-publish.yml` builds `ghcr.io/mubashiribstec/xplogent` for amd64 and
arm64, smoke-tests it, and publishes `:latest` from `main` and `:1.2.3` / `:1.2` / `:1` from
`v1.2.3` tags. **One-time step for the repository owner:** after the first run, open GitHub →
your profile → Packages → `xplogent` → Package settings → *Change visibility* → **Public**.
New packages start private, and servers then can't pull without `docker login`.

## Option 2: systemd on the host

```bash
git clone https://github.com/mubashiribstec/xplogent.git && cd xplogent
sudo ./deploy/install-server.sh
```

The script creates an `xplogent` system user, installs the pinned dependencies from
`requirements/server.lock` into `/opt/xplogent/.venv`, builds the dashboard (if Node.js is
installed), writes `/etc/xplogent/xplogent.env` with a generated access token, and starts the
service on `127.0.0.1:8765`. It prints the token once; it is also in the env file. Re-running
keeps the existing token.

Install Caddy (see caddyserver.com/docs/install), then:

```bash
sudo DOMAIN=agent.example.com caddy run --config /opt/xplogent/deploy/caddy/Caddyfile
# or copy the file to /etc/caddy/Caddyfile, set DOMAIN in Caddy's environment, and `systemctl reload caddy`
```

Useful: `journalctl -u xplogent -f`, `systemctl restart xplogent`, `systemctl status xplogent`.
Provider keys go in Settings (stored encrypted in the data directory) or in `/etc/xplogent/xplogent.env`.

## Phone agent (Telnyx Voice API or SIP trunk)

Everything is set in the dashboard's **Calls** view; its checklist shows what is still missing.
Choose the **Phone line** under Setup:

- **Telnyx Voice API** needs a public `https://` address (webhook + call audio): point a domain
  at the server, open ports 80/443, run `xplogent-docker https agent.example.com`, then fill in
  the Telnyx card (API key, application ID, number) and press **Test Telnyx**.
- **SIP trunk** (FreePBX/Asterisk extension, Telnyx SIP connection, other providers) needs no
  domain: run `sudo xplogent-docker sip on` (add `--ip <public IP>` if the provider reaches you
  over the internet through a router, and forward the UDP ports), allow UDP 5060 and
  10000-10100 in the firewall, then enter server, extension and password in the SIP trunk card
  and press **Save & connect**.

Then: `xplogent-docker speech on` and **Local on this server** in the Speech card; pick the call
model; switch the phone agent on. Use **Test call in the browser** before going live.

See [voip.md](voip.md) for both phone lines, training uploads, live call control, manual and test
calls, cost notes and safety defaults.

## Your own Ollama server

Settings → **Ollama server**: enter its URL (for example `https://ai.example.com`) and, if it is
behind a proxy that requires one, the bearer token (stored encrypted). **Test connection** lists
its models; use the buttons to pick the chat, embedding, reflection and call models. Settings
made in the dashboard take precedence over `XPLOGENT_MODEL` / `OLLAMA_HOST` in `.env`.

Caddy forwards WebSockets automatically, which the call audio stream needs.

## Security: read this

- **The dashboard agent can run shell commands and Python, and control files** (inside the
  container with Docker; on the host with systemd). Anyone with the access token controls it.
  Keep the token secret and serve it over HTTPS. The deployment files make the token mandatory.
  To **rotate** it: `xplogent-docker token --rotate`, or change `XPLOGENT_AUTH_TOKEN` in
  `/etc/xplogent/xplogent.env` and restart (the env variable overrides `xplogent token`).
- `.env` (Docker) and `/etc/xplogent/xplogent.env` (systemd) hold your API keys and token and are
  readable by root only. Backups made by `xplogent-docker backup` include the encrypted keys and
  their key file: store them privately.
- Only ports 80/443 should be reachable. Do not publish port 8765.
- To limit the blast radius, in Settings you can switch the **execution backend** to Docker or
  SSH (so shell/python run in a container), disable tool groups you don't need, and tighten the
  approval policy (`safety.policy`). The phone agent itself uses only a small fixed tool set
  (log outcome, callback, transfer, lookup, end call) and cannot run commands.
- Back up the data directory (database, skills, secrets, key file) together; secrets are useless
  without the key file next to them, and the key file is useless without being kept private.
- The carrier webhook is public by necessity and is protected by its secret path; use a long
  random secret and regenerate it if exposed.

## Packages and reproducibility

- `requirements/server.lock` pins every Python dependency (Python 3.11 to 3.13, all platforms).
  Both deployments install from it, so two servers always get identical versions.
  Regenerate with `make lock`; review the diff and run the tests before committing.
- `web/package-lock.json` pins the dashboard build (`npm ci`).
- Installing without the lock (`pip install xplogent[server]`) works too but picks the newest
  allowed versions.

## Troubleshooting

| Symptom | Check |
|---|---|
| Browser shows a certificate error | DNS not pointing at the server yet, or ports 80/443 not forwarded / blocked; `xplogent-docker https DOMAIN` checks both and explains, or see `xplogent-docker logs caddy` / `journalctl -u caddy` |
| `401` everywhere | Paste the token from `.env` / `/etc/xplogent/xplogent.env` (not the one printed by an old `xplogent token`) |
| Dashboard blank, API works | Dashboard not built: install Node.js and re-run the installer (the Docker image includes it) |
| `could not download the Xplogent image` | The GHCR package is still private (see "The prebuilt image"), or no internet; `docker compose build` builds it locally from a checkout |
| No-domain mode not reachable from another PC | `xplogent-docker local --lan`, and allow port 8765 in the firewall (`ufw allow 8765`) |
| Lost the token | `xplogent-docker token` (Docker) or `/etc/xplogent/xplogent.env` (systemd) |
| Calls never connect | Webhook URL in Telnyx must be exactly the one shown in Calls; `public_url` must be `https://` |
| WebSocket drops after a minute | Another proxy in front of Caddy with a short timeout; raise it or remove it |
