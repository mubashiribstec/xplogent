# Roadmap & advanced ideas

Xplogent already covers local + hosted models, memory with self-improvement,
sandboxed shell execution, backup, and a phone agent with mandatory-field
enforcement, recruitment templates and CRM delivery. Here are researched next
steps, roughly by value/effort.

## Near-term, high value
- **RAG over your documents** — a `documents` tool + loader (PDF/markdown/text) that
  chunks files into the existing embedding store, so the agent can answer from your
  own files. Reuses `MemoryManager`/`Embedder` and the FTS5 index for hybrid search.
- **Cost dashboard** — the per-turn/session cost estimate (already shown in chat)
  rolled up per day/model in a Settings panel, using the real token usage now captured.
- **Chat gateways** — reach the agent from **Telegram/Discord** (a gateway process that
  bridges messages to a runtime). Needs a bot token; great for an always-on assistant.

## Medium-term
- **Encrypted secrets at rest** — encrypt `~/.xplogent/.env` with an OS keyring instead
  of plaintext.
- **Local-model auto-pull** — detect a missing Ollama model and `ollama pull` it on first use.
- **Streaming voice** — real-time STT/TTS for a spoken back-and-forth (current voice is batch).
- **Skill hub** — import/share skills with a community registry (export/import already exists).

## Larger bets
- **Trajectory export for fine-tuning** — dump successful runs as training data.

Have a request? These are starting points, not commitments — open an issue or just ask
the agent to build one.
