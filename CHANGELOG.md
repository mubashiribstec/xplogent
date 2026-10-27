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
- **Named Ollama servers**: any number of Ollama servers, each with its own bearer token
  (stored encrypted), in Settings → Ollama servers. Use one for any task as
  `ollama:<model>@<server>`; the backup model can run on one too.
- **Free OpenRouter models**: Gemma 4 31B (free) and Nemotron 3 Ultra (free) as presets
  (both support tools), and a **Model for calls** picker in Calls. The picker lists free
  models, your presets and each Ollama server's models.
- **Browser test call**: Start asks for the microphone; a failing voice is reported once,
  then the call continues as text; an "agent is thinking" status; clear reasons when the
  call can't start (bad model, WebSocket blocked by a reverse proxy, expired sign-in);
  over plain HTTP a disabled mic button explains HTTPS or an SSH tunnel.
- **`xplogent-docker https`** checks DNS and ports 80/443 first, waits for the Let's
  Encrypt certificate and explains a failure (e.g. port 80 not forwarded), and sets the
  phone agent's public address. Docs: HTTPS on a home or office connection.
- **Run checks** in the test-call card. **Test this model** and **Test key** buttons send one
  request the way a call does and explain any failure with a fix: bad key, credits, the
  upstream rate limit vs. the free daily cap (with today's free requests left), unknown model,
  provider down.
- The running build is shown in Settings → Status, in `/health` and in
  `xplogent-docker status`. The dashboard page is never served from a stale cache. A card
  that fails to draw shows its error alone, instead of blanking the whole page.
- Go-live checklist: "Call model speed" warns when calls run on a CPU-only local model.
- **Realtime speech-to-speech mode** (optional, `voip.mode: realtime`, OpenAI). The
  greeting, field guard, Pause AI, transfers and recording are kept, and the call falls
  back to the normal mode if OpenAI can't be reached.

### Fixed
- Training recordings failed to transcribe in the Docker image (PyAV 19 vs
  faster-whisper 1.2): pinned `av<19`.
- Windows ignored the contact's time zone for calling hours: added `tzdata`.
- The Calls page crashed with newer Kokoro images (their voice list has objects).
- API keys pasted with a stray space or newline were refused by the provider; they are trimmed.
- Fixes from a deployment audit:
  - The Guide page was empty in Docker: the image now ships the guide pages.
  - Dashboard chat ran file tools on a greeting and learned junk facts. Small talk now gets
    no tools; only tool calls that worked feed reflection, which may no longer learn from
    errors or the assistant's own claims. Settings → Memory shows where each fact came
    from and can forget the facts learned in the last 24 h.
  - The PBX/SIP password label flashed "not set" on every load. Disabled buttons now say why,
    and an extension that's switched off says how to register it.
  - Go-live shows "Model speed" (a CPU-only call model, including a named server on this
    machine) and the placeholder company name in words. A slow test call (first word
    after more than 3 s) says why and what to change.
  - Greetings are no longer logged as knowledge gaps.
  - The debug view explains why a browser test call has no SIP or audio data.
  - One accent colour (Branding) instead of two.
  - Run checks no longer leaves a test call behind (it uses `/ws/voip/ping`); model tests say
    they use a free OpenRouter request.
- A provider error now includes the provider's reason (e.g. "temporarily rate-limited
  upstream"), and a turn whose voice failed still records what the agent said.
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
