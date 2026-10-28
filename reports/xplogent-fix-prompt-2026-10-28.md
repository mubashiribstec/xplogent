# Prompt: fix and harden Xplogent

Use this prompt with a coding agent that has access to the Xplogent repository.

---

You are working in `mubashiribstec/xplogent`. Implement the security and production fixes below in small, reviewable changes. Preserve the project’s self-hosted, SIP/FreePBX, and Telnyx use cases. Do not weaken existing protections or silently change call behavior. Read the repository instructions first, inspect the current implementation, and verify each fix with focused regression tests plus the project’s existing CI commands.

## Required fixes

### 1. Secure RTP source handling

- Inspect `src/xplogent/voip/telephony/rtp.py` and the SIP SDP negotiation in `sip.py`.
- Never let an arbitrary first UDP packet overwrite the already negotiated RTP destination.
- Accept inbound audio/DTMF only from a source allowed by the active dialog and documented NAT/symmetric-RTP policy.
- Reject source changes after learning unless an authenticated/validated renegotiation authorizes the change.
- Keep legitimate NAT behavior working; make the trust policy explicit in code and configuration.
- Add tests for wrong-source audio, wrong-source DTMF, first-packet hijacking, mid-call source changes, and legitimate negotiated media.

### 2. Close the DNS-rebinding SSRF gap

- Inspect `src/xplogent/core/netguard.py`.
- Ensure the IP address actually used for the TCP connection is the address that passed the public-IP check. Preserve correct Host header and TLS SNI behavior.
- Revalidate every redirect target and every new connection; reject mixed DNS answers containing any private/reserved address.
- Keep the explicit private-network opt-in behavior, but make its effect clear and testable.
- Add deterministic tests using a fake resolver/transport for public-to-private rebinding, redirects to private IPs, IPv4-mapped IPv6, mixed answers, and valid public destinations.

### 3. Enforce secure CRM transport

- Inspect `src/xplogent/voip/crm.py` and the CRM settings/API validation.
- Require HTTPS for remote CRM base URLs. Permit plain HTTP only for loopback development addresses under an explicit and documented rule.
- Do not follow HTTPS-to-HTTP redirects while sending bearer/HMAC credentials or call data.
- Surface a clear settings/test-connection error for invalid URLs.
- Add tests proving no request is made and no credentials are sent for rejected schemes/redirects.

### 4. Close test-call safety bypasses

- Inspect the API `/voip/dial` route, `VoipService.dial`, cost caps, and the UI caller.
- A `test` flag must not create an unbounded route to real outbound calls. Restrict test calls to configured/verified test destinations, apply a separate finite test-call and spend budget, and rate-limit repeated tests.
- Keep do-not-call checks in force. Treat any hours override as a separately authorized, audited operator action and do not let agent tools invoke it.
- Ensure daily hard caps cannot be bypassed through test mode. If there is an emergency override, make it explicit, narrow, time limited, and audited.
- Add tests for direct API attempts, repeated test calls, disabled calling, DNC numbers, hours boundaries, and test-budget exhaustion.

### 5. Make filesystem scope accurate

- Inspect `safety/profile.py`, `safety/approval.py`, filesystem tools, shell execution, and role documentation.
- Enforce allowed roots consistently for reads, directory listing, writes, and edits, using resolved paths and tests for symlink escape/race cases.
- A path check on `cwd` alone does not sandbox shell commands. For roles with restricted roots, either disallow shell or run it inside a genuine OS/container sandbox with only allowed mounts. Keep unrestricted operator behavior explicit.
- Update documentation so it does not describe write-only checks as a complete filesystem sandbox.
- Add tests that attempt access outside the configured roots through every enabled tool and through symlinks.

### 6. Bound upload resources

- Inspect `/voip/training/upload` and the deployment proxy configuration.
- Enforce request-wide byte limits before or during multipart parsing, aggregate file-size limits, per-file limits, concurrency limits, and a storage quota.
- Ensure rejected/failed uploads and parser temporary files are cleaned up. Avoid writing the same large upload to multiple temporary copies where possible.
- Return a stable client error when a quota/limit is exceeded.
- Add tests for one oversized file, many individually valid files exceeding the aggregate cap, concurrent uploads, malformed multipart input, and cleanup after failure.

### 7. Validate SIP in-dialog requests

- Inspect `SipTrunk._on_request` and call/dialog state.
- Bind BYE, CANCEL, ACK, re-INVITE, NOTIFY, and other in-dialog methods to the established source policy, Call-ID, local/remote tags, CSeq, and transaction state.
- Keep valid retransmissions and routed PBX deployments working. Do not accept a request merely because it reuses a Call-ID.
- Add spoofing tests for each method and positive tests for legitimate dialogs/retransmissions.

### 8. Make CRM delivery durable and use dial locks

- Implement a database-backed transactional outbox for call summaries: persist the payload and pending state atomically with the call completion; retry after process restart; use a stable idempotency key; track attempts/next retry/dead-letter state; expose status and safe manual resend.
- Wire CRM `dial_lock`/`dial_unlock` into campaign outbound dialing. Acquire before dialing, fail closed on held/unavailable lock, and release safely on every terminal path. Use an owner token/lease so a worker cannot release another worker’s lock.
- Keep manual calls’ policy explicit. Do not silently treat an unavailable CRM as permission to bypass campaign coordination.
- Add tests for process-restart recovery, duplicate delivery, expired leases, held locks, lock release after failure/cancel, and CRM outage behavior.

## Production hardening follow-up

After the required fixes, implement or document the following in separate changes if they are too broad for one PR:

- Encrypt recordings at rest, audit access/deletion, and provide clear retention, consent, redaction, export, and deletion controls. Verify actual deletion rather than only removing a database pointer.
- Add per-user authentication and API authorization before supporting shared/team dashboard access; the agent’s tool roles are not dashboard user roles.
- Document the single-instance deployment boundary. A multi-replica design needs distributed call ownership, durable task execution, shared recording storage, and coordinated callback/campaign workers.
- Build a durable campaign scheduler with pacing, concurrency, pause/stop, budget, retries, consent/DNC policy, and per-campaign audit history.
- Add versioned call-evaluation datasets and automated quality monitors for latency, ASR/TTS errors, transfer success, CRM delivery, call completion, guardrail violations, and cost.
- Add the missing root `LICENSE` text to match the MIT metadata/badge if MIT is intended.

## Implementation rules

- Work from the current branch and preserve unrelated user changes.
- Do not add a dependency unless the fix needs it; document why if one is required.
- Avoid broad rewrites. Keep security decisions in shared primitives so alternate routes/workers cannot bypass them.
- Never log API keys, bearer tokens, full transcripts, or unredacted secrets.
- Add focused tests for every security/logic fix; then run `ruff check src tests`, `pytest -q`, the web build, and Docker/Compose validation that the repository CI already uses.
- Review the final diff for backwards compatibility and update setup/config documentation and `.env.example` where behavior or settings changed.
- In your final response, list changed files, explain each fixed risk, report exact verification results, call out any deferred item, and do not claim production readiness unless all required checks pass.

## Acceptance criteria

1. An RTP packet from an unrelated source cannot inject call audio/DTMF or redirect agent audio.
2. No hostname can pass URL validation on one IP and connect to a different private IP.
3. CRM credentials and call data are never sent over cleartext HTTP to a remote address or after an insecure redirect.
4. Test mode cannot bypass DNC, configured safety hours without an explicit audited override, or all spend/rate limits.
5. A constrained role cannot read, list, write, edit, or shell-access paths outside its configured boundary.
6. Multipart uploads are bounded by a request-wide and storage-wide cap, not only a per-file limit.
7. SIP dialog methods from an untrusted peer or mismatched dialog are rejected without affecting the live call.
8. Pending CRM summaries resume automatically after restart, duplicate retries are safe, and campaign calls honor a valid CRM dial lock.
9. New regression tests fail on the old behavior and pass on the fix; the existing CI suite remains green.
