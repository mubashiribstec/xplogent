# Response to the deep audit of 28 October 2026

**Audit:** `reports/xplogent-deep-audit-2026-10-28.md` (revision `9f1fbb7`)
**Response date:** 1 November 2026
**Status:** every P1 and P2 finding is fixed and covered by tests. P3 items are fixed where
they are small (licence, single-instance docs, campaign orchestration, test budget); the
larger ones are deferred below, with reasons.

I re-checked each finding against the code at `9f1fbb7` before fixing it. All nine P1/P2
findings were real. "CRM dial locks unused" and "campaign orchestration" were already
being handled by the campaign work (WP2) in progress at the time of the audit.

## P1

| Finding | Fix | Commit | Tests |
|---|---|---|---|
| **RTP accepts packets from any source; the first packet redirects media** | `RtpStream` has a source policy (`voip.sip.rtp_source`, default `dialog`). Audio and DTMF are accepted only from the SDP address or the SIP peer the call came from or went to; that covers FreePBX behind NAT. The port is learnt once, from an allowed host only. After that, packets from any other source are dropped and counted (`rejected_wrong_source` in the call's debug view). A re-INVITE with new SDP is the only way to move the media, and it must pass the SIP dialog check below. `sdp` pins to the SDP address alone; `any` is the old behaviour, for odd NAT setups. | `c352e8d` | `tests/test_rtp_source.py`: a wrong-source packet, first-packet hijack, a mid-call source change, a NAT peer accepted, re-INVITE renegotiation, each policy |
| **SSRF guard checks DNS, then httpx resolves again (rebinding)** | `safe_get` connects through `GuardedTransport`. Its network backend resolves the name once and refuses when **any** answer is non-public (IPv4-mapped IPv6 included). It then connects to exactly the address it checked; TLS SNI and Host stay the original name. Every redirect hop goes through the same check. `allow_private_network` still works. | `c352e8d` | `tests/test_netguard_pinning.py`: public→private rebinding with a fake resolver, mixed answers, a redirect to a private address, a valid public address, the private-network override |

## P2

| Finding | Fix | Commit | Tests |
|---|---|---|---|
| **CRM delivery doesn't enforce HTTPS** | `crm.post` and `crm.deliver` refuse plain `http://` to an **internet** address before any request is made, so no token is sent. Redirects are never followed. The settings card, Test connection and the readiness list say why and what to enter. On the owner's instruction (CRM and Xplogent run on the same office network), plain `http://` stays allowed to this machine and the local network: private and link-local IPs, bare machine names, `.local`/`.lan`-style names. An internal DNS name that looks public needs an explicit `voip.crm.allow_http: true`. | `2e65947` | `tests/test_crm_https.py`: the address rule (public, private, IPv6 ULA, mapped, names), no request and no token over public http, redirects not followed, LAN http delivered, readiness wording |
| **Test mode skips the daily spend gate and the calling hours** | A test call may only ring a saved **test number** (`voip.test_numbers`, edited under Call now; every change is audited as high risk), a PBX test extension, or a short internal extension on a SIP line. It is limited to `voip.test_calls_per_day` (30 by default). The do-not-call list applies, and the daily cost cap now counts test calls. The AI never dials 999/112/911/000. "Call anyway" outside the hours is audited as an operator override. The agent's own tools have no dial path, which I checked. | `2e65947` | `tests/test_test_calls.py`: someone else's number refused, the daily budget, DNC, the cap, emergency numbers, the test-number route audited, the hours override audited; `tests/test_limits.py` updated |
| **Role path scopes cover writes only; shell is unconfined** | A role's `allowed_paths` is now a real boundary for `read_file`, `list_dir`, `write_file` and `edit_file`. Paths are resolved first, so a symlink can't escape. Shell is refused for a role with `allowed_paths`, because a shell can't be confined without an OS sandbox. The global `allowed_write_roots` keeps its write-only meaning. The docs say exactly this. | `2e65947` | `tests/test_role_paths.py`: reads and listings outside refused, a symlink escape refused, shell denied, an unscoped role unchanged |
| **Uploads have no aggregate request or storage limit** | Before the body is parsed: no `Content-Length` → 411; over the per-upload limit (1 GB) → 413; training storage full (20 GB) → 413; less than 2 GB of disk would stay free → 507. While copying, each file (200 MB) and the running total are capped. At most two uploads are processed at once (429 otherwise). Partial files are removed on every error path, including a client that disconnects. All limits are configurable under `voip.training`. | `2e65947` | `tests/test_training.py`: missing length, oversize refused before parsing, one oversized file removed while others are kept, the aggregate cap with a lying header, storage quota, free disk, two at once, settings |
| **SIP in-dialog requests matched on Call-ID only** | For a known Call-ID, an in-dialog request (BYE, CANCEL, ACK, re-INVITE, NOTIFY, INFO, UPDATE) must come from the call's peer address or a trusted trunk address. It must also match the dialog tags: From-tag = the remote tag and To-tag = ours, with the direction taken into account; a CANCEL must match the INVITE's branch. Anything else gets 481, is logged, and leaves the call untouched. Retransmissions still work. | `c352e8d` | `tests/test_sip_dialog.py`: spoofed BYE, CANCEL, re-INVITE and NOTIFY refused; real ones and retransmissions accepted |
| **CRM delivery is retried in memory only** | Durable outbox. A summary is written to the call row as `pending`, with an in-flight lease, **before** it is sent. A loop started with the API (every 30 s) resends pending and failed rows, and rows left mid-send by a restart, after 1, 5 and 15 minutes, then hourly. After 10 tries the row becomes `dead`, an alert goes out, and it stays available for **Resend**, which restarts the schedule. Rows older than 7 days are manual only. `call_id` is the idempotency key, which the CRM contract already dedupes on. Interrupted calls recovered at start-up use the same path. | `2e65947` | `tests/test_crm_outbox.py`: stored before sending, delivered without a running loop, backoff, dead letter and alert, a send cut off by a restart, old and test rows not resent, Resend restarts the retries |
| **CRM dial-lock helpers not used on the dial path** | Campaigns take the lock before every dial. It is released after every outcome, and when a dial fails or is held back. Every lease carries a random `lock_token`, and only that token can release or renew it; the mock enforces this and the CRM prompt documents it. **Manual calls** to a candidate the CRM knows now take the lock too: if someone else holds it, the call is refused with the holder's name; if the CRM can't answer, the call goes ahead with a warning (campaigns stop instead). The lock is released when the call ends, doesn't connect, or fails to dial. A lease is never carried into a later callback. | `a3b58a4`, `2e65947` | `tests/test_dialler.py`, `tests/test_crm_v2.py::test_dial_locks`, `tests/test_crm_outbox.py` (manual lock taken and released, someone else's lock, CRM unavailable, a stale lease not reused) |

## P3 and readiness gaps

| Item | State |
|---|---|
| Campaign orchestration | **Done** in `a3b58a4` (WP2): a durable campaign scheduler (`campaigns`, `campaign_items`); pacing (concurrency and calls a minute); a retry policy within calling hours; per-campaign on/off and a global Stop all; per-campaign daily budgets; TPS screening that fails closed; and a consent gate with admin sign-off for cold lists. Checked on the Asterisk rig against the mock CRM. |
| Cost governance | **Partly done.** A hard call-duration ceiling (`max_call_seconds`), per-call and daily caps (the daily one now includes tests), per-campaign daily budgets, and a separate test-call budget. **Deferred:** pulling provider rates automatically and alerting on unknown costs. Rates are entered by the operator; the default voice and model rates are 0 and say so in the settings. |
| Licence metadata | **Done:** root `LICENSE` (MIT, matching `pyproject.toml` and the image label); the README links it. |
| Single-process limits | **Documented** in `docs/deploy-linux.md` (Security): one instance per data directory. The dialler, callbacks, outbox and SIP registration run in-process, so replicas would double-dial. Multi-replica support (distributed call ownership, a shared queue) is **deferred**: the product is a single-host appliance today. |
| Recording and transcript protection | **Deferred:** application-level encryption at rest, redaction, and pausing recording mid-call. For now `docs/deploy-linux.md` says recordings are unencrypted in the data directory and to use an encrypted disk where policy requires it; retention pruning already exists. |
| Multi-user dashboard authorisation | **Deferred:** the dashboard is owner-operated with one token; every risky change is audited. Per-user identity and scoped roles are needed before team hosting and are not started. |
| Quality evaluation and monitoring | **Deferred:** call debug, per-turn timings and alerts exist (5 failures in a row, CRM down, cost, TPS, dead letters). A scenario dataset, scorecards and p95 latency SLO alerts are planned for the simulator milestone (M3). |
| Typed config validation | **Partly done:** `voip/config_check.py` validates every `voip:` key and type against `default.yaml` at start-up and in the readiness list. Full typed models are not done. |

## Verification

- `ruff check src tests`, the full pytest suite (737 tests) and `npm run build`, all green.
- **Asterisk rig, live call (P1 and SIP):** Asterisk rang the agent's extension and, during
  the call:
  - 20 RTP packets from another host before any real audio: all dropped, media address
    unchanged;
  - 20 RTP packets from the PBX's own host but another port, after the real audio started:
    all dropped, media address unchanged;
  - a BYE with the right Call-ID and tags from another host, and a BYE from the PBX host with
    a wrong From-tag: both answered `481`, each logged, and the call stayed up;
  - the call then ended normally after 59 s, with 250 real packets in and 40 rejected
    (`rejected_wrong_source` in the debug view).
- **Rig campaign against the mock CRM (P2), on `2e65947`:** the candidate was dialled under
  the CRM dial lock and answered as the wrong person. The item moved to done, the summary
  reached the mock through the outbox, and the lock was released. CRM **Test connection** is
  green over `http://127.0.0.1`, and the readiness item reads "plain http on your local
  network".
- **Found on the rig and fixed** (`6b2821d`): after a restart the dialler dialled
  before the extension had registered. Our own line answered 401, and that counted as one of
  the candidate's attempts. Now a campaign waits while its SIP line isn't registered yet, and
  a dial our own line refuses (401/407, line not set up) puts the item back untouched, stops
  that campaign's round and raises a `campaign_line` alert (`tests/test_dialler.py`).
- CI on Linux and Windows and the Docker image build after merging to `main`.
