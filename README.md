# 🧠 Xplogent — a self-improving AI agent framework

[![CI](https://github.com/mubashiribstec/xplogent/actions/workflows/ci.yml/badge.svg)](https://github.com/mubashiribstec/xplogent/actions/workflows/ci.yml)
[![Docker image](https://github.com/mubashiribstec/xplogent/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/mubashiribstec/xplogent/pkgs/container/xplogent)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](#license)

Xplogent is a personal AI agent you fully own and run yourself. It works with **any
LLM provider** (including **local Ollama**, fully offline), keeps **its own
persistent memory**, and **improves itself** by reflecting on tasks and writing
reusable skills — all behind a configurable **safety/approval layer**.

Its flagship is a **phone-calling agent**: inbound and outbound AI calls over a
Telnyx SIP trunk or your own **FreePBX/Asterisk** (a call that overflows a PBX
queue lands on the AI), with transcripts, training-data upload, a test-call
panel, mandatory-field enforcement (a call cannot close "successfully" with
required data missing), and a signed post-call webhook to your CRM.

It's inspired by [Hermes](https://github.com/NousResearch/hermes-agent) and the
OpenClaw ecosystem, but built as a leaner, cleanly-architected core that's easy
to extend with drop-in plugins. It runs on **Windows and Linux** (and macOS).

## Install

### Linux server: one command (Docker)

```bash
curl -fsSL https://raw.githubusercontent.com/mubashiribstec/xplogent/main/get.sh | sudo bash
```

It installs Docker if needed, asks a few questions (your domain for automatic HTTPS, or
no-domain mode; local models with Ollama; your model and API key), generates an access
token, starts Xplogent and prints the address and token. Then manage it with:

```bash
xplogent-docker status        # health + address
xplogent-docker update        # backup, pull the new image, restart
xplogent-docker backup        # backup to /opt/xplogent-docker/backups
xplogent-docker token         # show the token (--rotate for a new one)
xplogent-docker help          # logs, restore, https <domain>, local, ollama on|off, ...
```

Non-interactive: `... | sudo bash -s -- --yes --domain agent.example.com` (or `--mode local --lan`,
`--ollama`, `--model openai:gpt-4o-mini`). Details: [docs/deploy-linux.md](docs/deploy-linux.md).

### Desktop (Windows, macOS, Linux)

```bash
git clone https://github.com/mubashiribstec/xplogent.git && cd xplogent
bash install.sh               # Windows (PowerShell): ./install.ps1
xplogent setup                # one time: pick provider/model, enter a key (or use local Ollama)
xplogent up                   # opens the dashboard in your browser
```

### Other ways

| | |
|---|---|
| Docker Compose by hand | `cd deploy/docker && cp .env.example .env` (set the token), `docker compose up -d` |
| Prebuilt image | `docker run -e XPLOGENT_AUTH_TOKEN=... -p 127.0.0.1:8765:8765 -v xplogent:/data ghcr.io/mubashiribstec/xplogent` |
| systemd, no Docker | `sudo ./deploy/install-server.sh` |
| From source | `pip install -e ".[all]"` and `cd web && npm install && npm run build` |

## Why Xplogent

| Capability | Xplogent |
|---|---|
| **Providers** | Ollama (local, or your own server with a bearer token), OpenAI, Anthropic, Gemini, OpenRouter (200+ models) — switch with one config value |
| **Token & cost** | Real token usage, a context-window gauge, and a live cost estimate in the chat |
| **Backup** | One-click `.tar.gz` backup/restore and JSON export/import of skills + memory |
| **Memory** | SQLite + local embeddings; short-term, long-term, and episodic recall |
| **Self-improvement** | Reflects after tasks, consolidates memory, auto-creates & reuses skills |
| **Agent rights** | Role profiles: allowed tools, risk policy, filesystem path scope, network |
| **Shell & files** | Shell and filesystem tools for the chat agent, behind the safety layer; shell runs locally, in **Docker**, or over **SSH** |
| **Phone agent** | Inbound + outbound AI calls (Telnyx or built-in SIP trunk for FreePBX/Asterisk), barge-in, DTMF, transfers, do-not-call + calling-hours gates |
| **Mandatory fields** | Declare required call fields in config; `end_call` is **refused** until they are collected — a phone number only counts once it has been read back and confirmed by the caller |
| **Honest by design** | System-injected AI disclosure in every greeting (cannot be edited away), transcripts-not-recordings wording, max-2 silence prompts then a polite goodbye |
| **Templates** | Built-in recruitment packs (inbound enquiry + outbound screener) with field schemas and a scoring rubric — the AI proposes, a human decides |
| **CRM delivery** | Signed (HMAC-SHA256) post-call webhook with retries and a delivery log: outcome, summary, fields, transcript |
| **Resilience** | Transient errors (network/timeout/429) auto-retry with backoff; STT/TTS fallbacks per call |
| **Safety** | Every risky action is risk-classified and gated (`auto` / `confirm` / `deny`) |
| **Interfaces** | CLI/TUI, REST + WebSocket API, Web dashboard (Chat · Calls · Test · Training · Settings) |
| **Extensible** | Drop-in Python plugins via one unified tool registry |

## Architecture

```
                CLI/TUI · Web dashboard (Chat + Calls)
                                  │
                    REST + WebSocket API (FastAPI)
                                  │
        ┌─────────────────────────┴─────────────────────────┐
   Chat agent                                        Phone agent
   providers │ memory │ skills      VAD → STT → CallBrain → TTS → caller
   tools │ safety │ plugins         Telnyx SIP · FreePBX · test panel
```

See `src/xplogent/` for the implementation; each subsystem is a self-contained
package behind a small interface, so providers, tools, and memory backends are
swappable.

## Using it

```bash
xplogent up            # foreground: opens the dashboard in your browser
# or run it in the background (survives closing the terminal):
xplogent start         # xplogent status / xplogent stop ; service install for boot auto-start
```

Setup is one-time (saved in `~/.xplogent`). The main dashboard views:
- **Chat** — talk to a single agent, **switch models on the fly**, set effort /
  thinking, and your conversation **persists** across reloads (with New chat).
- **Call training** — teach the phone agent from recordings, scripts and Q&A sheets.
- **Calls / Contacts** — the phone agent ([docs/voip.md](docs/voip.md)).
- **Settings** — change *everything* from the GUI: model, API keys (masked),
  safety policy, tool toggles, role/permission editor, memory & skills.
- **Guide** — this documentation, in-app.

### Terminal usage

```bash
xplogent chat                              # single-agent REPL
xplogent model openai:gpt-4o               # switch provider/model
xplogent serve                             # API only (no browser)
```

### Developing the dashboard

```bash
xplogent serve            # backend API on :8765
cd web && npm install && npm run dev    # dashboard on :5173 (hot reload)
```

## Installation extras

| Extra | Adds |
|---|---|
| `providers` | OpenAI + Anthropic SDKs |
| `memory` | `sqlite-vec` + local embeddings |
| `api` | FastAPI + Uvicorn (REST/WebSocket) |
| `voip` | Phone-call agent dependencies (server side: faster-whisper, no audio devices) |
| `server` | Headless Linux server set: providers, api, voip, rag, security |
| `all` | everything above + dev tools |

## Safety

Shell and filesystem access is powerful, so it is **safe by default**. Every tool call is
risk-classified (`low`/`medium`/`high`/`critical`) and routed through an
approval policy you control in `src/xplogent/config/default.yaml`. Destructive commands match
a deny-list and are blocked outright. In the CLI you approve inline; over the API
the agent emits an `approval_required` event your UI resolves.

## Updating

| Install | How |
|---|---|
| Docker (get.sh) | `xplogent-docker update` (backs up first; image tags pinned via `XPLOGENT_IMAGE`) |
| Desktop / git | `git pull` and reinstall (`pip install -e .`) |
| systemd | `git pull && sudo ./deploy/install-server.sh` |

Releases: pushing a `v*` tag publishes `ghcr.io/mubashiribstec/xplogent:<version>`; `main`
publishes `:latest`.

## Contributing

Issues and pull requests are welcome on
[github.com/mubashiribstec/xplogent](https://github.com/mubashiribstec/xplogent). Before a PR:
`ruff check src tests`, `pytest -q`, and `cd web && npm run build` (CI runs the same, plus the
Docker build and installer checks).

## License

MIT
