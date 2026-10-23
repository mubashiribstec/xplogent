# Changelog

All notable changes to Xplogent. Format follows
[Keep a Changelog](https://keepachangelog.com); versions are tagged on GitHub.

## [Unreleased]

### Added
- **Second SIP line**: the phone agent can now also register as an extension
  on an existing FreePBX/Asterisk (alongside the Telnyx trunk line), with its
  own settings card, readiness checks and per-call line tagging — internal
  extensions can dial the agent and queues can overflow to it.
- **Retell-parity agent layer**: system-injected AI disclosure in every
  greeting (cannot be edited away), greeting-once guard, silence recovery
  (max 2 prompts, then a polite goodbye), dynamic template variables,
  knowledge-gap logging, per-turn stage timings.
- **Mandatory-field enforcement**: `end_call` is refused while required
  fields are open; a phone number only counts once read back and confirmed.
- **Recruitment templates**: inbound enquiry + outbound screener packs with
  field schemas and a scoring rubric (the AI proposes, a human decides).
- **CRM webhook**: signed (HMAC-SHA256) post-call delivery with retries,
  backoff and a delivery log.
- **`xplogent doctor`**: plain-English health checks (config, keys, model
  reachability, database, dashboard build, ports, VoIP) with a fix per
  failure and a non-zero exit when something essential is broken.
- Setup wizard: Gemini provider, live Ollama verification with exact
  `ollama pull` commands, warnings for skipped API keys.
- Dashboard: first-run onboarding now also triggers when the local Ollama
  model is unreachable; Chat shows a "local model isn't answering" banner
  with the fix.

### Fixed
- `/status` returned 500 on a malformed Ollama host (or an unparsable proxy
  environment), so the dashboard showed "Backend unreachable" while the
  server was fine.
- Do-not-call and contact matching silently failed for 7–9 digit numbers.
- Memory recall's keyword fallback (embeddings unavailable) matched almost
  nothing; re-ingesting an edited file left the stale version searchable;
  the document FTS index was never backfilled.
- The context window wasn't bounded: the fold summary was neither counted
  nor capped.
- Mid-stream provider error payloads were swallowed as empty answers
  (OpenAI/Gemini/Ollama); truncated streams never reached the backup model.
- RAG ingest followed symlinks into credential files and let a direct file
  path bypass the extension allowlist; imported skill names could escape
  the skills folder on export.
- Docker execution backend bind-mounted any model-supplied folder
  read-write; mounts are now limited to home, /tmp and the data folder.
- pipx installs never received the built dashboard; `service install` wrote
  the systemd unit but never enabled it.
- Shipped role profiles named tools removed in the slim-down (roles ran
  almost tool-less); the default system prompt advertised removed
  PC-control abilities.
- Assorted CLI honesty fixes: restart/stop/service report real results,
  voip commands don't traceback on non-JSON errors, `skills new` can't
  write outside the skills folder.

### Removed
- Multi-agent teams/orchestrator, scheduler + triggers, MCP server, browser
  extension, Live Canvas, desktop voice, PC-control tools (GUI/browser/
  vision/python-exec), git self-updater, operator channel. The product is
  one chat agent + one phone agent, done well.
- The dead `memory` pip extra (its packages were never imported;
  embeddings run through the configured provider).

## [0.1.0] — pre-release development

Initial public development: chat agent with providers (Ollama/OpenAI/
Anthropic/Gemini/OpenRouter), persistent memory + self-learned skills,
safety/approval layer, web dashboard, Telnyx phone agent with transcripts,
training uploads and a test-call panel, Docker + systemd deployment.
