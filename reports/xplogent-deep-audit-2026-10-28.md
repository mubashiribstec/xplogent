# Xplogent repository audit

**Reviewed:** 28 October 2026  
**Repository:** [mubashiribstec/xplogent](https://github.com/mubashiribstec/xplogent)  
**Revision:** `9f1fbb7bbc578261582390e658ff04e2eed640fe`  
**Review type:** Read-only source and configuration review; no live deployment penetration test.

## Executive assessment

Xplogent has an unusually broad feature set for a young, self-hosted project: it combines chat agents, persistent memory, policy-gated tools, a dashboard, SIP/Telnyx calling, call recording, CRM lookup and delivery, callbacks, and Docker deployment. The direct FreePBX/Asterisk integration and local ownership model are meaningful strengths.

I would treat this revision as a **promising pilot system, not yet a production-grade multi-customer voice platform**. The strongest blockers are on the media security boundary and outbound fetch protection. Other material risks concern cost controls, permission scoping, CRM delivery/dial coordination, and handling of large uploads and sensitive call data.

### Priority summary

| Priority | Count | Main areas |
|---|---:|---|
| P1 — address before exposing to untrusted networks | 2 | RTP packet source validation; DNS rebinding in outbound fetch guard |
| P2 — address before routine production use | 7 | CRM TLS; test-call spend bypass; filesystem scope; uploads; SIP dialog validation; CRM outbox; CRM dial coordination |
| P3 — readiness and product quality | 5 | recording protection, operational scale, campaign automation, compliance controls, release/license hygiene |

## Confirmed findings

### P1. RTP accepts packets from arbitrary sources and lets the first packet redirect media

[`rtp.py`](https://github.com/mubashiribstec/xplogent/blob/9f1fbb7bbc578261582390e658ff04e2eed640fe/src/xplogent/voip/telephony/rtp.py#L84-L115) accepts any syntactically valid RTP packet. It does not compare the sender address with the negotiated SDP peer. When `latched` is false, it replaces `remote` with the address of the first packet received. The SIP layer sets `remote` from SDP, but the RTP latch does not honor that initial address. Later packets from other sources are still queued as caller audio.

**Impact:** A party able to send UDP to a call’s RTP port can inject audio/DTMF into the live agent. A first packet can also redirect the agent’s outbound audio to an attacker-controlled address. This undermines caller identity and can leak the agent’s side of the conversation.

**Fix direction:** Pin the RTP source to the SDP-negotiated address/port or permit symmetric RTP learning only after validating it against the SIP dialog and a narrow, documented NAT policy. Reject source changes after latching unless an authenticated renegotiation authorizes them. Add spoofed-source, first-packet, and source-change tests. Consider SRTP where supported.

### P1. The SSRF guard checks DNS before the HTTP client resolves the host again

[`netguard.py`](https://github.com/mubashiribstec/xplogent/blob/9f1fbb7bbc578261582390e658ff04e2eed640fe/src/xplogent/core/netguard.py#L36-L48) resolves a hostname and rejects non-public answers. [`safe_get`](https://github.com/mubashiribstec/xplogent/blob/9f1fbb7bbc578261582390e658ff04e2eed640fe/src/xplogent/core/netguard.py#L64-L83) then gives the hostname to `httpx`, which performs a separate resolution for the actual connection. A rebinding hostname can return a public address at validation time and a private address at connection time. Redirects are rechecked, but the validation-to-connect gap remains on each hop.

**Impact:** A model-directed fetch can potentially reach loopback, private-network, or cloud metadata services despite the guard.

**Fix direction:** Make the connection use the exact validated address while preserving the original Host header and TLS SNI, or use a custom resolver/transport that validates the peer address at connect time. Cover mixed public/private DNS answers, IPv4-mapped IPv6, rebinding, redirects, and connection reuse.

### P2. CRM delivery does not enforce HTTPS for the shared bearer/HMAC secret

[`crm.py`](https://github.com/mubashiribstec/xplogent/blob/9f1fbb7bbc578261582390e658ff04e2eed640fe/src/xplogent/voip/crm.py#L155-L178) builds the CRM URL from `crm.base_url` and sends the API token as a bearer credential and as the HMAC key. The default configuration documents an HTTPS example, but the client does not reject `http://` URLs.

**Impact:** A plain HTTP configuration sends both the shared credential and call data, including transcripts and candidate information, in cleartext.

**Fix direction:** Reject HTTP for non-loopback CRM endpoints; allow an explicit development-only exception for loopback. Make the settings UI show the validation error before saving/testing. Do not silently follow a redirect from HTTPS to HTTP.

### P2. Test mode bypasses the daily spend gate and calling-hours check

[`service.py`](https://github.com/mubashiribstec/xplogent/blob/9f1fbb7bbc578261582390e658ff04e2eed640fe/src/xplogent/voip/service.py#L587-L616) skips calling-hours checks when `test=True` and skips the daily-cost gate for test calls. [`voip.py`](https://github.com/mubashiribstec/xplogent/blob/9f1fbb7bbc578261582390e658ff04e2eed640fe/src/xplogent/interfaces/api/routes/voip.py#L718-L747) accepts `test` directly from the API request and passes it through. Test mode is still a real outbound dial.

**Impact:** An authenticated API client can place real calls outside configured hours and repeatedly bypass the daily cap. Cost estimates may also be ineffective where provider pricing is left at zero.

**Fix direction:** Restrict test calls to explicitly verified test destinations, apply a separate per-day test budget and rate limit, retain the daily hard stop unless an operator performs a distinct audited override, and make that override unavailable to ordinary agent/tool actions.

### P2. Role path scopes cover writes only and do not constrain shell access

[`profile.py`](https://github.com/mubashiribstec/xplogent/blob/9f1fbb7bbc578261582390e658ff04e2eed640fe/src/xplogent/safety/profile.py#L14-L30) identifies only `write_file` and `edit_file` as path-scoped tools. [`approval.py`](https://github.com/mubashiribstec/xplogent/blob/9f1fbb7bbc578261582390e658ff04e2eed640fe/src/xplogent/safety/approval.py#L119-L147) applies its root check to that set. `read_file` and `list_dir` are not checked; shell is not constrained by `allowed_paths`. The default role comments describe path scope broadly, while the coder example describes write sandboxing.

**Impact:** A role configured with `allowed_paths` is not a filesystem sandbox. It can read outside the listed roots, and an allowed shell can access paths beyond them.

**Fix direction:** Decide and document whether this is a write restriction or a true filesystem boundary. For a true boundary, apply canonical-path checks to reads, listings, and writes; prevent symlink escapes and time-of-check/time-of-use races; and run shell in an OS/container sandbox with explicit mounts. Do not claim shell isolation from command classification alone.

### P2. Training uploads have a per-file limit but no aggregate request or storage quota

[`voip.py`](https://github.com/mubashiribstec/xplogent/blob/9f1fbb7bbc578261582390e658ff04e2eed640fe/src/xplogent/interfaces/api/routes/voip.py#L423-L468) allows up to 50 uploaded files and checks each file against 200 MB while copying it. There is no total-bytes cap for the multipart request, per-user quota, or global concurrency/storage quota in this handler.

**Impact:** A valid dashboard credential can trigger very large temporary and permanent disk use. Multipart parsing may spool files before the application’s per-file copy check runs.

**Fix direction:** Enforce request-body and aggregate upload limits at the proxy and application layers, lower configurable per-file limits to the product need, enforce free-space and quota checks, limit concurrent processing, and clean up partially parsed or failed uploads.

### P2. SIP in-dialog requests are not bound to the established peer/dialog

[`sip.py`](https://github.com/mubashiribstec/xplogent/blob/9f1fbb7bbc578261582390e658ff04e2eed640fe/src/xplogent/voip/telephony/sip.py#L572-L612) applies `_allowed` to a new INVITE in `_inbound_invite`, but the top-level request handler processes BYE, CANCEL, ACK, re-INVITE, and NOTIFY using a Call-ID lookup without first validating the source address or dialog tags against the established call.

**Impact:** If an attacker can learn or guess an active Call-ID and dialog identifiers, they may disrupt or alter an active call. The exploitability depends on deployment network exposure and identifier predictability; this should be fixed even where the PBX firewall is expected to limit reachability.

**Fix direction:** Validate source, Call-ID, local/remote tags, CSeq, and transaction state for every in-dialog request. Apply strict source policy by default for public deployments and test spoofed BYE/CANCEL/re-INVITE/NOTIFY requests.

### P2. CRM call delivery is retried in memory, with no automatic outbox replay

[`service.py`](https://github.com/mubashiribstec/xplogent/blob/9f1fbb7bbc578261582390e658ff04e2eed640fe/src/xplogent/voip/service.py#L482-L492) stores a payload and then schedules `enqueue_delivery`, which starts an asyncio task. The CRM module retries inside that task. The repository provides a manual resend path, but the audited startup/recovery path does not sweep persisted unsent CRM payloads and enqueue them after a restart.

**Impact:** A process exit during delivery interrupts retries. The record remains available for a human resend, but delivery is not automatically durable.

**Fix direction:** Use a durable outbox in the database with state, attempt count, next retry time, and idempotency key. A startup worker should reclaim pending/expired work, retry with bounded backoff, and expose dead-letter status without duplicating CRM side effects.

### P2. CRM dial-lock helpers are not used on the outbound dial path

[`crm.py`](https://github.com/mubashiribstec/xplogent/blob/9f1fbb7bbc578261582390e658ff04e2eed640fe/src/xplogent/voip/crm.py#L468-L481) implements `dial_lock` and `dial_unlock`. The audited [`VoipService.dial`](https://github.com/mubashiribstec/xplogent/blob/9f1fbb7bbc578261582390e658ff04e2eed640fe/src/xplogent/voip/service.py#L587-L645) performs local DNC checks and a CRM lookup, then dials; it does not acquire/release these CRM locks.

**Impact:** The CRM contract’s cross-system coordination is not actually protecting this dial path. A recruiter and Xplogent, or two Xplogent instances, may dial the same candidate concurrently. The local DNC check and the later dial are also not an atomic operation.

**Fix direction:** Acquire a CRM lock before a campaign dial, fail closed when the lock is unavailable or held by another party, release on all terminal paths, and use an idempotent lease/owner token so one worker cannot release another worker’s lock. Define a separate policy for manual calls.

## Production readiness gaps

These are not all defects in the current personal/self-hosted design. They become important when operating at business volume or with multiple teams.

1. **Campaign orchestration is incomplete.** The CRM queue endpoint previews candidates, and the UI can manually initiate a call. The code has no durable campaign scheduler, pacing controller, automatic retry policy, per-campaign stop/pause controls, or operator approval queue. Existing callback scheduling is a useful foundation but is not a campaign dialer.
2. **Single-process assumptions limit scale.** Active SIP/Telnyx calls, callback runners, SIP transports, and delivery tasks are in process memory. SQLite and local recordings fit a single host, but horizontal replicas would need distributed call ownership, leases, a shared durable queue, and storage coordination.
3. **Cost governance needs an effective default.** The code supports configurable cost rates and caps, but zero/default rates can make estimates materially undercount. Add provider/model/carrier rate sources, unknown-cost alerts, hard call-duration ceilings, campaign budgets, and a separate test budget.
4. **Recording and transcript protection needs an explicit operator policy.** Recording defaults to enabled in `default.yaml` and is stored as local WAV files. The reviewed recorder writes files without application-level encryption. Add deployment guidance and controls for encryption, access auditing, retention verification, consent configuration, redaction, deletion/export requests, and pausing recording during sensitive segments.
5. **No general tenant/user authorization model is visible.** The web API uses a shared dashboard token. Agent role profiles govern tool execution, but they do not give separate dashboard users scoped access to calls, recordings, contacts, or settings. This is suitable for an owner-operated appliance; shared/team hosting needs user identity, role-based API authorization, and audit attribution.
6. **Operational quality loops are early.** There is call debug, alerts, and a substantial test suite. The repo does not show a first-class automated conversation evaluation suite with scenario datasets, versioned scorecards, latency/error SLOs, and production monitor thresholds. Add call-quality sampling, regression replay, and alerts on p50/p95 response latency, ASR/TTS failures, transfer failures, CRM delivery, and dropped media.
7. **License metadata is inconsistent.** `pyproject.toml` and the README advertise MIT, but the repository API reports no recognized license and the root tree has no `LICENSE` file. Add the actual license text and verify package metadata.

## Structure and logic review

### What is structured well

- Runtime assembly is centralized in `runtime.py`; provider, tools, memory, and safety are wired in one place.
- API routes are divided by feature, with shared token/origin checks in `deps.py` and `server.py`.
- SIP protocol/media, call state, voice brain, CRM client, and recording modules are separated into cohesive components.
- The tests cover many specialized behaviors, including API security, SIP, VoIP hardening, CRM v2, callback hours, secrets, backups, and deployment scripts.
- The latest GitHub CI run for the audited commit passed; the Docker image workflow also completed successfully on that SHA.

### Design improvements

- Make security invariants explicit at module boundaries: accepted network peer, allowed filesystem roots, permitted CRM scheme, and call-state transitions should be enforced once in shared primitives.
- Validate configuration into typed models at load/save time. Today many `voip` settings are plain nested dictionaries and are converted ad hoc; malformed ranges or contradictory settings can fail only when a call starts.
- Add state-machine tests for SIP and call lifecycle transitions, including duplicate/reordered webhooks, cancellation during CRM lookup, carrier disconnects, and process restart recovery.
- Centralize outbound call policy so UI, callback runner, tools, and future campaign workers cannot bypass DNC, calling hours, rate limits, or cost controls through alternate call paths.
- Use structured, redacted logging. Avoid putting full phone numbers and user-entered transfer targets in audit text unless necessary; define PII retention for debug traces.

## Comparison with professional voice-agent platforms

This is a comparison of the repository’s visible capabilities with vendor-documented platform features, not a claim that every vendor feature is suitable or superior for every deployment.

| Area | Xplogent at this revision | Professional platform baseline / gap |
|---|---|---|
| Telephony control | Strong self-hosted SIP, FreePBX/Asterisk, Telnyx options; direct ownership of call path | Hosted platforms typically provide managed calling, numbers, and batch APIs. Vapi documents server-side bulk call operations; Retell describes batch calls and call-ended/analyzed webhooks. [Vapi](https://docs.vapi.ai/quickstart/web), [Retell](https://www.retellai.com/learn/how-ai-voice-agents-work-on-a-call) |
| CRM and callbacks | CRM lookup, HMAC-signed summaries, call fields, DNC checks, exact-time callbacks | Strong domain fit, but durable event delivery and CRM dial locking need completion. Retell documents post-call analysis results via dashboard, webhook, or API. [Retell post-call analysis](https://www.retellai.com/features/post-call-analysis) |
| Analytics and debugging | Local call list, transcripts, debug bundle, SIP/RTP diagnostics, spend estimate | Good foundation, but no visible automated quality monitor/issue lifecycle. Vapi documents call logs including transcripts, recordings, analysis, latency, and cost, plus webhook delivery logs and monitoring. [Vapi logs](https://docs.vapi.ai/observability/logs/overview), [Vapi monitoring](https://docs.vapi.ai/observability/monitoring-quickstart) |
| Safety controls | Mandatory fields, DNC, calling hours, role facts guard, safety approvals, per-call/daily caps | Several thoughtful guardrails; higher assurance requires the media/source validation and cost bypass fixes above, and stronger campaign locks and regression evaluations.
| Privacy / enterprise ops | Self-hosted storage and configurable recording retention; secret encryption at rest | No evidence in the repo of vendor-like enterprise retention controls, team audit boundaries, regional data processing guarantees, or certification claims. Twilio’s voice docs expose recording controls, callbacks, channel composition, and deletion APIs as managed features. [Twilio recording](https://www.twilio.com/docs/voice/api/recording), [Twilio status callbacks](https://www.twilio.com/docs/usage/webhooks/voice-webhooks) |
| Deployment model | Open-source, self-hosted, SIP/PBX-friendly, local models possible | This is Xplogent’s main differentiator. Managed platforms trade some control for provider operations, scaling, and integrated monitoring.

**Overall comparison:** Xplogent is best positioned today as a self-hosted, PBX-integrated voice-agent framework for technically capable operators. To compete as a turnkey professional platform, prioritize secure RTP, durable call/webhook workflows, campaign pacing, multi-user authorization, enterprise data controls, quality monitoring, and a validated release/install path.

## Recommended order of work

1. Fix RTP source validation and DNS rebinding; add adversarial tests.
2. Enforce CRM HTTPS and secure SIP in-dialog handling.
3. Close the test-mode spend/hour bypass; enforce test destinations and separate caps.
4. Make role path scope truthful and close the upload aggregate resource-exhaustion path.
5. Implement a durable CRM outbox and CRM dial-lock lifecycle.
6. Add encrypted recording storage, lifecycle controls, and per-user authorization before team hosting.
7. Build campaign orchestration and automated call-quality evaluation/monitoring.
8. Add root LICENSE and clarify single-instance deployment limits in the docs.

## Verification notes

- Audited commit: `9f1fbb7bbc578261582390e658ff04e2eed640fe`.
- GitHub Actions showed CI passing and Docker image publishing successful for this commit on 28 October 2026.
- The review inspected source and configuration remotely; it did not exercise a live PBX, carrier account, browser session, or deployed container.
- Findings are based on code paths and stated preconditions. The SIP dialog spoofing issue in particular depends on network exposure and identifier disclosure; validate it against the intended deployment topology.
