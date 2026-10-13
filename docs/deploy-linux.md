# Deploying Xplogent on a Linux server

Two supported ways. Both put **Caddy** in front for automatic HTTPS and both **refuse to run
without an access token**. Pick one:

| | Docker Compose | systemd on the host |
|---|---|---|
| Best for | quick, repeatable setup | no Docker, direct access to the host |
| Command | `docker compose up -d --build` | `sudo ./deploy/install-server.sh` |
| Data | Docker volume `xplogent-data` (`/data`) | `/var/lib/xplogent` |
| Update | `git pull && docker compose up -d --build` | `git pull && sudo ./deploy/install-server.sh` |

## Before you start

- A Linux server (Debian/Ubuntu tested paths; any systemd distro works) with ports **80 and 443**
  open and nothing else exposed. The app itself listens only on localhost / the private Docker network.
- A DNS **A record** for your hostname (for example `agent.example.com`) pointing at the server.
  Caddy needs it to obtain the HTTPS certificate.
- For the phone agent: the carrier needs that public `https://` address.

## Option 1: Docker Compose

```bash
git clone https://github.com/mubashiribstec/Ai-agent.git && cd Ai-agent/deploy/docker
cp .env.example .env
nano .env                      # set DOMAIN and XPLOGENT_AUTH_TOKEN (openssl rand -hex 32), add provider keys
docker compose up -d --build
docker compose logs -f xplogent
```

Open `https://<DOMAIN>` and paste the token. Compose refuses to start if `DOMAIN` or
`XPLOGENT_AUTH_TOKEN` is empty.

**GPU speech for the phone agent** (optional, needs the NVIDIA Container Toolkit on the host):

```bash
docker compose --profile gpu-speech up -d
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

## Option 2: systemd on the host

```bash
git clone https://github.com/mubashiribstec/Ai-agent.git && cd Ai-agent
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

## Phone agent (Telnyx)

1. Dashboard, **Calls**: set the public URL (`https://agent.example.com`), company name and
   hand-off number, generate a webhook secret, and switch the phone agent on.
2. In Telnyx, set your Call Control application's webhook to the URL shown there, and attach
   your number. Put `TELNYX_API_KEY` in the env file / `.env` / Settings.
3. See [voip.md](voip.md) for the call flow, cost notes and safety defaults.

Caddy forwards WebSockets automatically, which the call audio stream needs.

## Security: read this

- **The dashboard agent can run shell commands and Python, and control files.** Anyone with the
  access token controls that server. Keep the token secret and serve only over HTTPS. The
  deployment files make the token mandatory. To **rotate** it, change `XPLOGENT_AUTH_TOKEN` in
  `.env` / `/etc/xplogent/xplogent.env` and restart (the env variable overrides anything set with
  `xplogent token`).
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
  allowed versions. `mcp` is capped below 2 because the 2.x SDK changed its API.

## Troubleshooting

| Symptom | Check |
|---|---|
| Browser shows a certificate error | DNS not pointing at the server yet, or ports 80/443 blocked; see `docker compose logs caddy` / `journalctl -u caddy` |
| `401` everywhere | Paste the token from `.env` / `/etc/xplogent/xplogent.env` (not the one printed by an old `xplogent token`) |
| Dashboard blank, API works | Dashboard not built: install Node.js and re-run the installer (Docker builds it for you) |
| Calls never connect | Webhook URL in Telnyx must be exactly the one shown in Calls; `public_url` must be `https://` |
| WebSocket drops after a minute | Another proxy in front of Caddy with a short timeout; raise it or remove it |
