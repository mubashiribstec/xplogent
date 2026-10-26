# Changelog

All notable changes to Xplogent. Format follows
[Keep a Changelog](https://keepachangelog.com); versions are tagged on GitHub.

## [Unreleased]

### Added
- **Queue context (FreePBX)**: on calls from the PBX, the caller's number comes from
  P-Asserted-Identity / Remote-Party-ID, and the number they dialled from the oldest
  Diversion (or History-Info). A call is marked "via queue" when it comes from a
  configured queue number or carries `X-Xplogent-Via: queue`; the queue wait comes from
  `X-Xplogent-Queue-Wait`. These headers are believed only from the PBX itself. The
  agent thanks the caller once for waiting, `voip.queue_greeting` can replace the
  greeting, the call row shows a badge, and the CRM payload carries the fields. A
  tested custom-destination dialplan is in `docs/voip.md`.
- **Capacity safety**: each SIP line counts callers who got "busy" (last 24 h). The
  readiness checklist warns when a line is nearly full, and the docs describe the
  FreePBX fallback behind the AI.
- **Speech upgrade**:
  - real per-stage timings on every turn (speech-to-text, model, voice, total), with the
    provider that served each stage and any fallback or error;
  - Deepgram streaming speech-to-text, which never loses words: the recorded utterance
    goes through the batch chain if the stream fails;
  - built-in Whisper `auto` (large-v3-turbo on a GPU, small.en on CPU);
  - ElevenLabs and Cartesia voices with a voice picker.
- **Call Debug view**: per call, a timeline, stage timings, the SIP ladder (login
  headers never stored), RTP quality and Telnyx events. The redacted debug bundle
  download is audited; from a terminal, `xplogent voip debug <id>`.
- **Pre-call CRM lookup**: a signed `contact-lookup` runs while the call rings or
  before dialling, capped at 1.5 s and failing open. The caller is greeted by name,
  CRM notes are treated as data only, and the CRM's do-not-call blocks dialling.
- **CRM delivery status on each call**, with a **Resend** that sends the stored payload
  again (the receiver dedupes on `call_id`).
- **Sale-ready dashboard**:
  - glass theme with a tested contrast floor;
  - a "Go live" checklist and health row;
  - a product tour;
  - demo mode (badged; never counted or sent);
  - branding (logo, colour, company name);
  - actionable empty states;
  - phone layout.
- **Realtime speech-to-speech mode** (optional, `voip.mode: realtime`, OpenAI). The
  greeting, field guard, Pause AI, transfers and recording are kept, and the call falls
  back to the normal mode if OpenAI can't be reached.

### Fixed
- Training recordings failed to transcribe in the Docker image (PyAV 19 vs
  faster-whisper 1.2): pinned `av<19`.
- Windows ignored the contact's time zone for calling hours: added `tzdata`.
- A caller speaking over the greeting cut off the AI/recording disclosure; the
  greeting now always plays to the end.

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
