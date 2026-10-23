# Xplogent — Setup Guide

From zero to a working agent. Pick **one** install path, run the wizard once,
and you're chatting. Everything after that (models, phone lines, CRM) is
optional and can be done from the dashboard's Settings page.

## Requirements

| | Minimum | Notes |
|---|---|---|
| Python | 3.11+ (3.12/3.13 fine) | Not needed for the Docker path |
| Node.js | 18+ | Only to build the dashboard from source; the Docker image and pipx installs via `install.sh` handle it for you |
| RAM | 2 GB for the app itself | Local models need more, per model |
| An LLM | Any of: local **Ollama** (free, offline), OpenAI, Anthropic, Google Gemini, OpenRouter | Ollama is the default |

## Path 1 — Linux server, one command (Docker)

```bash
curl -fsSL https://raw.githubusercontent.com/mubashiribstec/xplogent/main/get.sh | sudo bash
```

It installs Docker if needed, asks about a domain (automatic HTTPS) or
no-domain mode, sets up Ollama if you want local models, generates an access
token, and prints the dashboard address + token. Manage it afterwards with
`xplogent-docker` (`status`, `logs`, `update`, `backup`, `token`). Full
deployment detail: [docs/deploy-linux.md](docs/deploy-linux.md).

## Path 2 — Desktop (Windows / macOS / Linux)

```bash
git clone https://github.com/mubashiribstec/xplogent.git
cd xplogent
bash install.sh            # Windows PowerShell: ./install.ps1
xplogent setup             # one-time wizard (see below)
xplogent up                # dashboard opens in your browser
```

`install.sh` prefers **pipx** (isolated, on your PATH) and falls back to a
`.venv` in the checkout. It installs the app and, when Node is present, builds
the dashboard and installs it into the pipx environment too.

> No Node? The API still runs; build the dashboard later with
> `cd web && npm install && npm run build`, or run from the checkout.

## Path 3 — From source (developers)

```bash
pip install -e ".[all]"
cd web && npm install && npm run build && cd ..
xplogent setup
xplogent up
```

## The first-run wizard (`xplogent setup`)

The wizard asks four things and saves them under `~/.xplogent/`:

1. **Provider** — `ollama` (local, free), `openai`, `anthropic`, `gemini`,
   `openrouter`. Typing an unknown name warns instead of saving nonsense.
2. **Model** — a sensible default per provider (e.g. `ollama:llama3.1`,
   `gemini:gemini-2.0-flash`); type your own to override.
3. **API key** — only for hosted providers. Skipping is allowed, but the
   wizard warns you chats will fail until a key is added in Settings.
4. **Ollama check** — if you picked Ollama, the wizard verifies the server
   answers and prints the exact `ollama pull …` commands for any model that
   isn't downloaded yet.

Prefer clicking? Skip the wizard: `xplogent up` opens the dashboard, and a
fresh install shows the same setup as a welcome dialog (it appears whenever
no key is set, or when the local Ollama model can't answer, and there are no
chats yet). In Chat, a warning banner tells you if your local model is down
and how to fix it.

**Verify any install at any time:**

```bash
xplogent doctor
```

Doctor checks your config file, API keys (presence only), whether your model
answers, the memory database, the dashboard build, the port, and — when the
phone agent is enabled — the VoIP configuration. Every failure comes with a
plain-English fix, and it exits non-zero if anything essential is broken.

## Connecting an LLM

- **Ollama (free, offline):** install from [ollama.com](https://ollama.com),
  then `ollama pull llama3.1` and `ollama pull nomic-embed-text` (the second
  powers semantic memory; without it memory uses keyword search). Remote or
  authenticated Ollama servers are supported — set the URL/token in
  Settings → Ollama server, or `OLLAMA_HOST` / `OLLAMA_API_KEY` in
  `~/.xplogent/.env`.
- **Hosted:** paste the key in the wizard, in Settings → API keys, or via the
  environment (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY` or
  `GEMINI_API_KEY`, `OPENROUTER_API_KEY`). Keys are stored encrypted at rest.
- **Switching later:** `xplogent model openai:gpt-4o`, or the model switcher
  in Chat. A backup model can be configured (`fallback` in config) so a local
  model answers when the main one is down.

Environment variables live in **`~/.xplogent/.env`** (or `$XPLOGENT_HOME/.env`)
— a `.env` anywhere else is not read. See `.env.example` for the full list.

## Connecting telephony (optional)

The phone agent answers and places real calls. Two independent lines, either
or both:

- **Telnyx SIP trunk** — external calls. Point your Telnyx connection at the
  agent, or let the agent register to Telnyx with the trunk credentials.
- **FreePBX extension** — the agent registers as an ordinary extension on
  your existing FreePBX/Asterisk, so internal extensions can dial it and a
  queue can overflow to it.

Both are configured in the dashboard (Calls view) or `config/default.yaml`
under `voip:`. **Full wiring, NAT notes, dial-plan examples and the readiness
checklist: [docs/voip.md](docs/voip.md).** Outbound dialling is gated by a
do-not-call list and calling-hours window; every greeting discloses the AI.

## Troubleshooting

| Symptom | Likely cause → fix |
|---|---|
| Chat fails / "local model isn't answering" banner | Ollama isn't running or the model isn't pulled → start Ollama, `ollama pull <model>`, or switch provider in Settings. `xplogent doctor` pinpoints it. |
| Dashboard says "Backend unreachable" or the status dot stays red | The server isn't running, or (older installs) a malformed Ollama URL made `/status` fail → check the address in Settings → Ollama server is a valid `http://host:port`. |
| "Dashboard not built yet" (API works) | The web build is missing → `cd web && npm install && npm run build`. pipx installs: re-run `install.sh` (it copies the build into the pipx env). Docker: set `XPLOGENT_WEB_DIR` or rebuild the image. |
| 401 / login loop | Access token mismatch → the token is in `~/.xplogent/.env` (`XPLOGENT_AUTH_TOKEN`); `xplogent-docker token` on Docker installs. |
| Port 8765 already in use | Something else (or an old Xplogent) holds it → `xplogent status`, `xplogent stop`, or serve on another port: `xplogent serve --port 8766`. |
| Chats work but memory feels dumb | The embedding model isn't pulled → `ollama pull nomic-embed-text` (memory falls back to keyword search without embeddings). |
| Calls: no audio / one-way audio | NAT: the PBX extension needs `rtp_symmetric`, `force_rport`, `rewrite_contact` on; check the readiness list in the Calls view and [docs/voip.md](docs/voip.md). |
| `xplogent` command not found after install.sh | pipx path not in your shell yet → open a new terminal, or run `pipx ensurepath`. |
| Windows: service won't register | The scheduled task needs an elevated PowerShell — the CLI prints the exact command to run. |

## Uninstall

- **Desktop:** `xplogent service uninstall` (if installed), then
  `pipx uninstall xplogent` (or delete the checkout/`.venv`). Your data is in
  `~/.xplogent/` — delete that folder to remove everything, or keep it for a
  future install. Take a backup first (Settings → Backup) if in doubt.
- **Docker:** `xplogent-docker uninstall` (keeps data unless you confirm
  deletion).
- **systemd (deploy script):** `sudo systemctl disable --now xplogent` and
  remove `/opt/xplogent`.
