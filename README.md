# 🧠 Xplogent — a self-improving AI agent framework

Xplogent is a personal AI agent you fully own and run yourself. It works with **any
LLM provider** (including **local Ollama**, fully offline), keeps **its own
persistent memory**, **improves itself** by reflecting on tasks and writing
reusable skills, and can **control your whole PC** (shell, files, mouse/keyboard,
screen, browser) — all behind a configurable **safety/approval layer**.

It's inspired by [Hermes](https://github.com/NousResearch/hermes-agent) and the
OpenClaw ecosystem, but built as a leaner, cleanly-architected core that's easy
to extend with plugins and MCP servers. It runs on **Windows and Linux** (and macOS).

## Why Xplogent

| Capability | Xplogent |
|---|---|
| **Providers** | Ollama (local/offline), OpenAI, Anthropic, Gemini, OpenRouter (200+ models), and **Claude via your subscription** (no API key, through the `claude` CLi) — switch with one config value |
| **Council chat** | Ask several models the same question at once and get each answer side-by-side **plus a synthesized final** |
| **Token & cost** | Real token usage, a context-window gauge, and a live cost estimate in the chat |
| **Backup** | One-click `.tar.gz` backup/restore and JSON export/import of skills + memory |
| **Memory** | SQLite + local embeddings; short-term, long-term, and episodic recall |
| **Self-improvement** | Reflects after tasks, consolidates memory, auto-creates & reuses skills |
| **Multi-agent** | Run many agents at once (you set the limit); orchestrator auto-decomposes a goal, or define named agents |
| **Collaboration** | Agents broadcast status and send direct messages to each other |
| **Deep monitoring** | Live per-agent telemetry, kanban task board, agent chatter, persisted run traces |
| **Agent rights** | Per-agent role profiles: allowed tools, risk policy, filesystem path scope, network |
| **PC control** | Shell, filesystem, Python, web, GUI (mouse/keyboard/screenshots), browser |
| **Vision** | `analyze_image` sends screenshots to a vision model — the agent can *see* and act on what's on screen |
| **Scheduling** | Natural-language cron ("every day at 9am") for unattended, recurring jobs that survive restarts |
| **Sandboxing** | Run `shell`/`python_exec` locally, in **Docker**, or over **SSH** — swap backends without touching tools |
| **Resilience** | Transient errors (network/timeout/429) auto-retry with backoff; agents can `delegate_task` to sub-agents |
| **Safety** | Every risky action is risk-classified and gated (`auto` / `confirm` / `deny`) |
| **Interfaces** | CLI/TUI, REST + WebSocket API, Web dashboard (Chat · Mission Control · Schedules), Voice |
| **Extensible** | Drop-in Python plugins + MCP servers via one unified tool registry |

## Multi-agent teams

```bash
# Auto: the orchestrator plans subtasks and runs a team (max 3 at once)
xplogent orchestrate "research the top 3 vector DBs and write a comparison file" --max 3

# Manual: define named agents (name:role:task), run them concurrently
xplogent team -a "scout:researcher:find facts about X" \
              -a "writer:coder:write a summary to notes.md"
```

Agents share a **message bus** (broadcast + direct messages) and a **task board**.
Each agent runs under a **role profile** (see `roles:` in `src/xplogent/config/default.yaml`)
that limits its tools, risk policy, filesystem paths, and network access — so you
control exactly what every agent may do. Watch it all live in the **Mission
Control** dashboard (agent cards with pause/cancel, kanban, agent chatter) or in
the terminal.

## Architecture

```
              CLI/TUI · Voice · Web (Chat + Mission Control)
                                  │
                  REST + WebSocket API (FastAPI)  ── monitor ──┐
                                  │                            │
                          Event bus (async)            TraceRecorder → SQLite
                                  │
                       ┌──────────┴──────────┐  semaphore(max_concurrent)
                       │ Orchestrator + Plan │  auto-decompose OR named agents
                       └──────────┬──────────┘
            ┌─────────────────────┼─────────────────────┐
        Agent(researcher)    Agent(coder)        Agent(reviewer)
            │ permission profile · own memory · collab tools │
            └──── MessageBus (broadcast + direct) · TaskBoard ┘
                                  │
        per agent: providers │ memory │ tools │ safety │ skills │ plugins
```

See `src/xplogent/` for the implementation; each subsystem is a self-contained
package behind a small interface, so providers, tools, and memory backends are
swappable.

## Quick start (easy path)

```bash
bash install.sh        # Windows: ./install.ps1   (pipx/venv + PATH + build dashboard)
xplogent setup         # one time: pick provider/model, enter a key (or use local Ollama)
xplogent up            # foreground: opens the dashboard in your browser
# or run it in the background (survives closing the terminal):
xplogent start         # xplogent status / xplogent stop ; service install for boot auto-start
```

Setup is one-time (saved in `~/.xplogent`). The dashboard has four tabs:
- **Chat** — talk to a single agent, **switch models on the fly**, set effort /
  thinking, and your conversation **persists** across reloads (with New chat).
- **Mission Control** — launch multi-agent teams and watch them live.
- **Settings** — change *everything* from the GUI: model, API keys (masked),
  safety policy, concurrency, tool toggles, role/permission editor, memory &
  skills, and **one-click Update** (git pull → reinstall → auto-restart).
- **Guide** — this documentation, in-app.

### Terminal usage

```bash
xplogent chat                              # single-agent REPL
xplogent orchestrate "research X, write a summary" --max 3
xplogent model openai:gpt-4o               # switch provider/model
xplogent update                            # pull + reinstall from git
xplogent serve                             # API only (no browser)
```

### Linux server (Docker or systemd + HTTPS)

```bash
# Docker Compose + automatic HTTPS (Caddy):
cd deploy/docker && cp .env.example .env && nano .env && docker compose up -d --build
# or directly on the host as a hardened systemd service:
sudo ./deploy/install-server.sh
```

Both require an access token and install from the pinned `requirements/server.lock`.
Full guide (DNS, firewall, phone agent, GPU speech, backups, security): [docs/deploy-linux.md](docs/deploy-linux.md).

### Manual install

```bash
pip install -e ".[all]"
ollama pull llama3.1 && ollama pull nomic-embed-text   # local models
cd web && npm install && npm run build                 # build the dashboard
```

### Web dashboard

```bash
xplogent serve            # backend API on :8765
cd web && npm install && npm run dev    # dashboard on :5173
```

### Use Xplogent from Claude Desktop / Claude Code (MCP)

Xplogent can run **as an MCP server**, so any MCP client can delegate tasks to
it — run a single agent, launch a multi-agent team, query memory, or drive the
PC tools (all through Xplogent's safety/rights layer).

```bash
pip install -e ".[mcp]"
xplogent mcp                                   # stdio (default)
xplogent mcp --transport streamable-http --port 8766   # remote/HTTP
```

Register it with **Claude Code**:

```bash
claude mcp add xplogent -- xplogent mcp
```

…or in **Claude Desktop** (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "xplogent": { "command": "xplogent-mcp" }
  }
}
```

Exposed MCP tools: `xplogent_run_agent`, `xplogent_orchestrate`,
`xplogent_search_memory`, `xplogent_list_skills`, `xplogent_remember`, plus each
PC-control tool (toggle with `mcp.server.expose_raw_tools`). MCP-driven runs use
the `mcp.server.agent_role` profile; confirm-tier actions are blocked unless
`mcp.server.auto_approve` is set — use a `restricted` role for untrusted clients.

## Installation extras

| Extra | Adds |
|---|---|
| `providers` | OpenAI + Anthropic SDKs |
| `control` | `pyautogui`, `mss`, Playwright (GUI + browser control) |
| `memory` | `sqlite-vec` + local embeddings |
| `api` | FastAPI + Uvicorn (REST/WebSocket) |
| `voice` | Whisper STT + TTS (microphone/speaker, desktop) |
| `voip` | Phone-call agent dependencies (server side: faster-whisper, no audio devices) |
| `server` | Headless Linux server set: providers, api, voip, mcp, scheduler, rag, security (no desktop/torch) |
| `mcp` | MCP client **and** server (run `xplogent mcp`) |
| `all` | everything above + dev tools |

## Safety

Full PC control is powerful, so it is **safe by default**. Every tool call is
risk-classified (`low`/`medium`/`high`/`critical`) and routed through an
approval policy you control in `src/xplogent/config/default.yaml`. Destructive commands match
a deny-list and are blocked outright. In the CLI you approve inline; over the API
the agent emits an `approval_required` event your UI resolves.

> ⚠️ GUI automation (`pyautogui`) needs a real display. On a headless/remote
> machine, drive Xplogent through the API/dashboard instead.

## Status

Active development. Build order and design live in the project plan. Contributions
and issues welcome on the `claude/custom-ai-agent-framework-wxq891` branch.

## License

MIT
