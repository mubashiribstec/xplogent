# Roadmap & advanced ideas

Xplogent already covers local + hosted models, persistent memory with
self-improvement (reflection + learned skills), role-scoped shell/file tools
behind a safety layer, encrypted secrets at rest, RAG over your own documents
(`xplogent docs ingest`), a cost/usage analytics view, backup/restore and
skill export/import, and a phone agent with two SIP lines (Telnyx trunk +
FreePBX extension), mandatory-field enforcement, recruitment templates and
signed CRM delivery. Here are the researched next steps, roughly by
value/effort.

## Near-term, high value
- **Call recording** — store call audio (with the existing AI-disclosure and
  transcription wording kept honest), retention controls, playback in the
  Calls view.
- **Streaming speech for calls** — streaming STT and lower-latency TTS so
  phone conversations feel less turn-based.
- **Chat gateways** — reach the agent from **Telegram/Discord** (a gateway
  process that bridges messages to a runtime). Needs a bot token; great for
  an always-on assistant.

## Medium-term
- **Answering-machine detection + campaign dialler** — detect voicemail on
  outbound calls, leave a message, and run compliant call lists (calling
  hours + do-not-call already exist).
- **Per-line transfer targets** — different human hand-off numbers for the
  Telnyx trunk vs the FreePBX extension line.
- **Community skill registry** — publish/discover skill packs beyond the
  bundled library (export/import already exists).

## Larger bets
- **Trajectory export for fine-tuning** — dump successful runs as training data.

Have a request? These are starting points, not commitments — open an issue or just ask
the agent to build one.
