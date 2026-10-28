# Xplogent: implementation plan to close the production gaps

Companion to `docs/production-plan.md` (the answers to the 25 questions) and
`docs/crm-integration-prompt.md` (what the CRM must provide). The CRM endpoints and payloads
named here are the ones defined in that prompt (`/api/xplogent/*`, API version 2).

Milestones:
1. **M0: CRM link.**
2. **M1: safe automatic calling.**
3. **M2: pilot.** One vacancy, ≤ 50 calls a day, every call reviewed.
4. **M3: quality and scale.**

Each work package (WP) lists what changes in this repo and the tests that prove it.
Sizes: S ≈ 1–2 days, M ≈ 3–5, L ≈ 1–2 weeks.

## M0: CRM link (needs the CRM endpoints)

### WP1. CRM client v2 (M) · Q1, Q11
- `voip/crm.py`: one signed `post(path, body)` helper (V2 signature, timeout, fail-open
  result) used by:
  - `sales_open()`;
  - `queue(sale_id, radius_km, limit)`;
  - `lookup()`, now parsing `applicant_id`, `first_name`, `flags`, `history`, `applications`,
    `interviews` and `calls`;
  - `send_job_details()`;
  - `dial_lock()` / `dial_unlock()`.

  Every CRM string stays capped and control-stripped (it's untrusted).
- **Call brief:** a dict carried in the call's `info`: `applicant_id`, `sale_id`,
  `campaign_id`, plus the `sale` and `candidate` objects from the queue. It's stored with the
  call: a new `calls.brief` JSON column (migration in `memory/store.py`).
- **Prompt** (`service.build_session` → `CallBrain`): three labelled, read-only sections:
  - **ROLE FACTS** from `sale`;
  - **CANDIDATE** (first name, postcode district, distance, flags);
  - **HISTORY** (last calls, stage, interviews, callback note).

  The templates' `{{candidate_name}}`, `{{role}}` and `{{location}}` are filled from the brief,
  no longer only from CSV contacts.
- **`call-summary` payload v2:**
  - adds `applicant_id`, `sale_id`, `campaign_id`, `final_attempt`, `callback_at` (ISO),
    `answers` (the collected fields), `email_confirmed` and `provider_call_id`;
  - adds the new outcome values `no_answer`, `voicemail`, `busy`, `number_unobtainable`,
    `wrong_person`, `no_job`, `recruiter_callback`, `screen_incomplete`, `interrupted`;
  - older CRM receivers ignore the unknown fields.
- **Settings → CRM:** a **Test connection** button that calls `sales/open` with `limit 1` and
  reports a signature, clock or URL problem in plain words.
- **Tests:** an httpx mock CRM. Signatures verified; lookup parsing and capping; brief → prompt
  sections; payload v2 fields; fail-open on timeout.

## M1: safe automatic calling

### WP2. Campaigns and dialler (L) · Q10, Q20, Q4, Q16
- **Tables:**
  - `campaigns`: `id`, `name`, `sale_id`, `template`, `line` (telnyx/sip/extension),
    `caller_id`, `enabled`, `max_concurrent`, `calls_per_min`, `radius_km`, `hours_profile`,
    `retry_policy` (JSON), `daily_cost_cap`, `per_call_cost_cap`, `max_call_seconds`,
    `created_at`;
  - `campaign_items`: `id`, `campaign_id`, `applicant_id`, `phone`, `brief` (JSON), `state`
    (queued/locked/dialling/in_call/retry_wait/done/skipped/failed), `attempts`, `next_at`,
    `last_outcome`, `last_call_id`, `skip_reason`, `updated_at`.
- **Dialler:** an asyncio task in `VoipService`, ticking every 5 s, that dials only when **all**
  of these hold:
  - the global **Stop all** switch is off, and the phone agent and the campaign are on;
  - it's within hours (WP3);
  - fewer than `max_concurrent` calls are live, and the `calls_per_min` limit isn't reached;
  - the cost caps aren't reached (WP8).

  It then takes the due items. For each one, right before dialling:
  1. re-runs `contact-lookup` (fresh do-not-call and flags);
  2. checks the local DNC list and TPS (WP5);
  3. takes the CRM `dial-lock` (409 → skipped "locked by <user>");
  4. dials with `meta = {brief, campaign_id, item_id}`.

  On `finalize_call`, the item moves on by outcome:
  - **retry**: `no_answer` / `busy` / `voicemail` → `retry_wait` with `next_at` from the
    retry policy;
  - **final**: everything else → `done`;
  - the summary goes to the CRM with `final_attempt` on the last try;
  - the dial lock is released.
- **Refill:** `campaign/queue` is fetched on start and whenever fewer than 10 items are queued.
  Duplicates (same applicant) are ignored.
- **Retry policy default** `{"max_attempts": 3, "gaps_hours": [2, 22, 24]}`:
  - three tries over about three days, at different times of day;
  - after the last, the CRM gets `no_response`;
  - never more than 3 AI attempts per candidate per 7 days, across all campaigns.
- **Not interested** ends the item; the CRM sets `is_temp_not_interested`, and its queue
  excludes the candidate afterwards.
- **Dashboard → Campaigns** page:
  - create from a vacancy picker (`sales/open`), with a preview of the first queue and its
    `skipped` counts;
  - per-campaign **On/Off**, a global **Stop all** (also `xplogent voip campaigns stop-all`);
  - live counters (queued / calling / connected / interested / callback / not interested /
    voicemail / no answer / failed), spend today against the cap;
  - an item list with each attempt's call link.
- **Tests:**
  - the dialler never exceeds `max_concurrent` or the pacing;
  - it stops within one tick when Stop all is pressed;
  - DNC, TPS and lock skips are recorded;
  - the retry schedule follows the policy;
  - the item states after each outcome;
  - a refill doesn't duplicate.

### WP3. Calling hours that know the calendar (S) · Q20
- `calling_hours` profiles: per weekday (`mon`–`sun`: start/end or closed), plus
  `bank_holidays: closed`. The default:
  - Mon–Fri 09:00–20:00;
  - Sat 10:00–16:00;
  - Sunday and bank holidays closed.

  Everything is in Europe/London.
- **UK bank holidays:** the gov.uk JSON (`https://www.gov.uk/bank-holidays.json`), cached for
  a week, with a bundled fallback list for the next 2 years.
- The same check applies to manual calls (an override for tests stays as today).
- **Tests:** weekday and Saturday windows; Sunday and a bank holiday refused; the summer-time
  change.

### WP4. Exact-time callbacks (M) · Q3, Q23
- **Prompt:** the brain gets **NOW** (Europe/London date, weekday and time) every turn.
- **`book_callback`:** the arguments become `{"at": "<ISO local time>", "spoken": "Wednesday at
  5:30 pm", "who": "ai|recruiter"}`. The server checks the time is:
  - in the future, and within 30 days;
  - inside calling hours (or recruiter hours when `who` = recruiter).

  If not, the tool returns the nearest allowed slots for the agent to offer. The agent must
  read the time back.
- **`who` = ai:** the campaign item goes to `retry_wait` with `next_at = at`. Manual and inbound
  calls get a new `scheduled_callbacks` table, which the dialler also serves.
- **`who` = recruiter:** the CRM gets `recruiter_callback` with `callback_at`. The AI never
  calls these.
- **Tests:** natural phrases → valid ISO via a scripted model; an out-of-hours time → slots
  offered; the dialler redials at the time; the CRM gets `callback_at`.

### WP5. TPS/CTPS screening and consent (M) · Q17
- **Provider interface** `voip/compliance.py`:
  - `screen(numbers) -> {number: {"tps": bool, "ctps": bool, "checked_at"}}`;
  - one HTTP adapter for the chosen provider (configured URL and key, a secret);
  - a `tps_cache` table, valid for 28 days.
- **Rules:**
  - campaigns screen every number before its first dial;
  - TPS or CTPS registered → item `skipped: tps` (logged, never dialled);
  - a provider error → **not dialled** (fail closed for campaigns; manual and test calls only
    warn).
- **Campaign setting `consent_basis`:**
  - `applied` (candidates who applied via a job board for this kind of role);
  - `registered`;
  - `cold`.

  `cold` campaigns can't be switched on until the legal sign-off box is ticked by an admin;
  the sign-off is audited.
- **Tests:** a TPS hit is skipped; the cache is used; a provider error blocks campaign dials;
  the consent gate.

### WP6. Answer: listen first, detect voicemail, confirm identity (M) · Q14, Q15
- **Listen first (outbound):** wait up to 2.5 s after answer for the callee's "Hello?". Then
  say a short **identity check** without any job detail: "Hi, is that Sarah?". Only after a
  yes come the disclosure and the reason for the call.
  - Today the full greeting, role included, starts the instant the call is answered.
  - Prevents talking over "Hello?", and gives the voicemail check time.
  - Never tells a parent, partner or colleague that Sarah applied for a job.
  - Wrong person → apologise, end (`wrong_person`); no detail is given.
- **Voicemail detection:**
  - **Telnyx calls:** `answering_machine_detection: premium` on dial; hang up on a machine.
  - **SIP and extension calls**, in the engine during the first 6 s, any of:
    - continuous callee speech longer than 4 s;
    - speech-to-text hits ("leave a message", "not available", "after the tone", "voicemail",
      the carrier phrases);
    - a beep tone (a Goertzel detector at 800–1,200 Hz held ≥ 250 ms).
  - **On a machine:** hang up silently, outcome `voicemail`; it counts as an attempt.
- **Tests:** recorded voicemail greetings (UK carriers) → `voicemail` within 6 s; a human
  "Hello?" → identity check; wrong person → no job detail spoken.

### WP7. Role facts guard: no invented pay (M) · Q19
- **Before each sentence is spoken** (`CallBrain` sentence stream → engine), extract the
  "claims":
  - money (`£`, "pounds", "an hour", "per annum", `k`);
  - hours ("12-hour", "37.5 hours");
  - dates;
  - counts.
- **Allowed** if the same value (normalised) appears in ROLE FACTS, the knowledge chunks
  retrieved this turn, or the caller's own words.
- **Otherwise** the sentence is replaced with "I don't have that exact figure in front of me —
  the recruiter will confirm it", with an event `guard_blocked` and the original text kept in
  the debug view.
- **Settings:** a guard sensitivity setting and an audit page (blocked sentences per day,
  by campaign).
- **Tests:** a model says "£15 an hour" when the brief says £12.60 → blocked; "£12.60" →
  spoken; the caller's own numbers are allowed.

### WP8. Call limits and cost caps (M) · Q21, Q24, Q25
- **Time:** `max_call_seconds` per template and campaign (screening 480). At `max − 60 s` the
  brain gets "one minute left: wrap up, offer a recruiter callback for anything open".
- **Off-topic:** a new tool `redirect()` that the model calls when it steers back to the
  screen. On the 3rd consecutive redirect the engine closes politely (`exit: off_topic`, sent to the CRM as
  outcome `screen_incomplete`).
- **Quick close:** after `log_outcome(not_interested | no_job | do_not_call)` the engine
  allows 45 s, then ends.
- **Cost:**
  - a live estimate per call (minutes × the line, speech and voice rates + model tokens ×
    the gateway price);
  - at `per_call_cost_cap` → polite close;
  - campaign spend per day at 80 % → warning, at 100 % → campaign paused (live calls finish);
  - a global daily cap.
- **Tests:** a wrap-up at T−60 s; an off-topic close; a quick close after "not interested";
  the cap pauses the campaign; an over-cap call ends.

### WP9. Crash recovery and alerts (S) · Q20
- **On start-up:**
  - calls still `active` with no live session → `interrupted`;
  - their transcript so far is sent to the CRM;
  - campaign items go to `retry_wait` (in 30 min, with an apology line: "Sorry, we were cut
    off earlier…");
  - dial locks are released.
- **Line drop mid-screen:** one redial within 10 minutes if the screen wasn't finished.
- **Alerts** (a webhook URL and/or email, set in Settings): the dialler stops by itself
  (cap, errors), 5 failed calls in a row, the CRM unreachable for 5 min, or TPS unreachable.
- **Tests:** a simulated crash → recovery on restart; the alert fires on consecutive failures.

## M2: pilot hardening

### WP10. Email job details through the CRM (S) · Q5
- **Tool `send_job_details(email)`.** The email field must be confirmed first: the agent spells
  it back.
- The server calls the CRM `send-job-details` for the current applicant and vacancy and tells
  the agent "sent" or the reason ("blocked", "invalid email").
- **Tests:** an unconfirmed email is refused; the CRM is called once; a CRM error is said
  politely.

### WP11. Hand-off to a recruiter (M) · Q23
- **Interested + recruiter hours:** transfer to the recruiters' ring group (a transfer target
  per campaign).
- **Before transferring,** a provisional call summary goes to the CRM. The recruiter's
  softphone screen-pop (the CRM `sip/{token}/lookup`) then already shows "AI: interested,
  right to work yes, start in 2 weeks". That's cheaper than a spoken whisper and works with
  the existing desktop client.
- **Ring group doesn't answer in 20 s, or outside recruiter hours:**
  - the agent books a `recruiter_callback` (WP4), offering the next recruiter slots (the
    recruiter-hours profile);
  - the candidate hears the exact time;
  - the CRM shows it in the Callback tab.
- **Tests:** transfer success; no answer → callback booked with a slot; the provisional summary
  is sent before the transfer.

### WP12. Speech and model defaults + benchmark (M) · Q7, Q8
- `xplogent voip bench` runs N scripted candidate conversations (WP14) against chosen models
  and prints a table per model: first-word time, tool-call accuracy, guard hits, cost per call.
- **Recommended preset** "UK recruitment":
  - Deepgram Nova-3 streaming;
  - an ElevenLabs Flash British voice;
  - the model chosen by the benchmark (Claude Haiku 5.5 / GPT-6 Luna / Gemini 3.6 Flash via
    the Vercel gateway);
  - backup Gemini 3.5 Flash-Lite;
  - Kokoro as the fallback voice.

### WP13. Capacity and load test (S) · Q2
- **Rig script:** N concurrent Asterisk callers with recorded candidate audio. It measures
  first-word time per call as N grows.
- The result is shown in Settings ("tested: 12 calls at once ≤ 1.5 s"), and `max_concurrent`
  defaults to 80 % of the measured value.

## M3: quality and scale

### WP14. Candidate simulator and regression suite (M) · Q22, Q6
- **Text-mode runs** of the real `CallBrain` (no audio) against an LLM playing a candidate
  persona from a library (YAML in `voip/sim/`):
  - eager; not interested; busy (callback); rude; off-topic;
  - asks about pay; no right to work; wrong number; asks for a human; "stop calling";
  - speaks little English.
- **Scoring:** required fields collected, outcome correct, no guard hits, length, tools used.
- **Dashboard → Testing → Run suite:** a pass/fail table. Required before a playbook or template
  change goes live (WP15).
- **Test campaign mode:** dial only the test extensions, with fake briefs, or a CRM sandbox
  URL.

### WP15. Learning with control (M) · Q6, Q12
- **Template + playbook:** the template keeps the rules and questions; the trained playbook
  adds tone, objections and FAQ. Today a template replaces the playbook.
- **Daily suggestions** (from the transcripts): knowledge gaps with a draft answer, objections
  handled badly, proposed playbook edits as a diff. A **review queue** (approve / edit /
  reject) runs before anything changes.
- **Versioned playbook:** a version table (who, when, diff). Publishing a version runs the
  regression suite; rollback is one click.

### WP16. Supervisor listen-in (M) · Q21
- **Listen:** stream the mixed live call audio to the dashboard (the same player as browser
  test calls).
- **Whisper** to the AI already exists (Instruct).
- **Barge:** take over by voice through the browser microphone (HTTPS).
- **Alerts:** a call over 6 min, upset-caller keywords, three guard hits in one call.

### WP17. CRM-aware inbound (S) · Q11, Q16
- **Known candidate with an open application:** greet by first name, mention the vacancy by
  title and area: "Are you calling about the care assistant role near Leeds?"
- **Missed an AI call in the last 7 days:** "You missed our call about…".
- **Unknown:** the inbound template.
- **FreePBX guide:** the dedicated DID for AI calls → the AI extension, with the recruiters'
  ring group as fail-over.

### WP18. Travel time (S) · Q18
- The CRM's straight-line distance stays the filter.
- For queued candidates only: postcode → travel time by car and public transport via a
  routing API (configured key), cached per postcode-district pair, added to the brief.
- The agent speaks it as a fact ("about 30 minutes by bus").

## My additional suggestions
1. **Identity first, then purpose (WP6).** Never mention a job application until the person
   confirms who they are. It's a privacy requirement and it's polite.
2. **Data minimisation.** The model never sees the CV, date of birth, gender or full address.
   The CRM prompt already enforces this at the source.
3. **Recording and transcript retention.** Set `recording.retention_days` (90 by default) and
   say it in your privacy notice. If a candidate objects to recording, the agent switches
   recording off for that call (a tool) and notes it.
4. **SMS after an unanswered final attempt**, using the CRM's SMS templates ("We tried to call
   you about a care role near Leeds — reply or call 0113…"). That needs the candidate's
   consent basis to allow it.
5. **"Is this a scam?" handling.** The agent gives the office number and offers to call back
   from it. Recruiters should know the AI's caller ID.
6. **Vulnerability and language signals.** The candidate is confused after two rephrases,
   distressed, or has little English → a polite end and a recruiter callback (already in the
   template). The dashboard should count these.
7. **Pilot KPIs on the Campaigns page:**
   - connect rate;
   - screen completion rate;
   - interested rate;
   - cost per interested candidate;
   - recruiter "agree with AI outcome" rate (a thumbs up or down on each call);
   - complaints.

   Go wider only when recruiters agree with **≥ 90 %** of outcomes.
8. **Shadow week.** The first 3 days of the pilot, every call is also listened to live (WP16)
   or reviewed the same day before the next batch runs.
9. **Hosting.** Hosted speech, or a small GPU box, before going wider than the pilot: the 4-core
   server can't run local speech for more than 1–2 calls.
10. **Hygiene:**
    - rotate the pasted API keys and the dashboard token;
    - run `xplogent-docker backup` nightly;
    - set up HTTPS (needed for the microphone and Telnyx).

## Order of work and dependencies
```
CRM endpoints (other session) ─┐
WP1 CRM client v2 ─────────────┴─► WP2 Campaigns/dialler ─► WP4 Callbacks ─► WP8 Limits/caps
WP3 Hours ─────────────────────────┘        │                                   │
WP5 TPS/consent ───────────────────────────┤                                   ▼
WP6 Listen-first/voicemail ────────────────┤                          M1 done → M2 pilot
WP7 Pay guard ─────────────────────────────┘
WP9 Recovery/alerts (any time in M1)
M2: WP10 email · WP11 hand-off · WP12 bench · WP13 load test
M3: WP14 simulator · WP15 learning · WP16 listen-in · WP17 inbound · WP18 travel time
```
**Rough total:**
- M0–M1: about 6–8 weeks of focused work;
- M2: 2–3 weeks, plus the 2-week pilot;
- M3: 4–5 weeks.

WP1, WP3, WP6, WP7 and WP9 can start before the CRM endpoints exist, against a mock CRM that
implements the same contract.
