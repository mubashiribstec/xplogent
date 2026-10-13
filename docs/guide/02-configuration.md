# Configuration

All settings can be changed from the **Settings** tab in the dashboard, or by
editing files under `~/.xplogent/`.

## Where settings live

| Source | Purpose | Precedence |
|---|---|---|
| Environment variables (`XPLOGENT_*`, API keys) | per-shell overrides | highest |
| `~/.xplogent/.env` | your API keys (written by the GUI / wizard) | high |
| `~/.xplogent/config.yaml` | your saved settings | medium |
| packaged `src/xplogent/config/default.yaml` | defaults | lowest |

## Common settings

- **Model** — `provider:model`, e.g. `ollama:llama3.1`, `openai:gpt-4o`,
  `anthropic:claude-sonnet-4-6`, `openrouter:meta-llama/llama-3.1-70b-instruct`.
- **Reflection / embedding models** — usually a small/local model; embeddings
  default to `ollama:nomic-embed-text` so memory works offline.
- **Memory** — enable/disable, short-term token budget, retrieval depth.
- **Safety policy** — per risk tier (`low`/`medium`/`high`/`critical`) choose
  `auto`, `confirm`, or `deny`.
- **Orchestrator** — `max_concurrent_agents` (how many agents run at once).
- **Tools** — enable/disable tool groups (shell, filesystem, web, gui, browser…).

## API keys

Set them in **Settings → Providers** (stored masked in `~/.xplogent/.env`) or via
the environment. Ollama needs no key.
