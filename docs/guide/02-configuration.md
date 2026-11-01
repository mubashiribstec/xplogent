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

- **Model** — `provider:model`, e.g. `openai:gpt-4o`,
  `anthropic:claude-sonnet-4-6`, `gemini:gemini-3.6-flash`,
  `openrouter:google/gemma-4-31b-it:free`, or through the **Vercel AI Gateway**
  `vercel:<creator>/<model>` (e.g. `vercel:anthropic/claude-opus-5`; key
  `AI_GATEWAY_API_KEY`). Settings → Browse provider models → *vercel* lists every model the
  gateway offers (about 500), and **Test key** tries a cheap one. A model on your own
  Ollama server (Settings → Ollama servers) is `ollama:<model>@<server>`, e.g.
  `ollama:qwen2.5:14b@gpu`; Xplogent doesn't run Ollama itself.
- **Reflection / embedding models** — reflection uses the main model unless you set a
  cheaper one. Embeddings are off by default: memory and knowledge search use full-text
  matching. For semantic search set `openai:text-embedding-3-small` or a model on your
  Ollama server (`ollama:nomic-embed-text@gpu`).
- **Backup model** — Settings → Backup model: any model that answers when the main one
  can't (down, no key, rate limit). Best on a different provider.
- **Memory** — enable/disable, short-term token budget, retrieval depth.
- **Safety policy** — per risk tier (`low`/`medium`/`high`/`critical`) choose
  `auto`, `confirm`, or `deny`.
- **Tools** — enable/disable tool groups (shell, filesystem, memory). Plugins
  can add more tools to the same registry.

## API keys

Set them in **Settings → Providers** (stored masked in `~/.xplogent/.env`) or via
the environment. An Ollama server needs no key (only a bearer token if it sits behind
an authenticating proxy).
