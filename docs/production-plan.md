# Xplogent in production: recruitment calling — answers and plan

Status checked against the code at build `7575bc9` (27 Oct 2026). Every answer says what
the product does **today**, what is **missing**, and the **plan item** that closes the gap.
Prices are live list prices where a source is given; time and capacity figures marked
*estimate* still have to be measured (plan item **P2-I**).

## At a glance

| # | Question | Today | Plan |
|---|---|---|---|
| 1 | Which candidate, role, questions, history | Partly: name, notes, last outcome from CRM; role only from a CSV contact | **P1-A** |
| 2 | Calls at the same time | 4 per SIP line (setting); CPU-bound with local speech | **P2-I** |
| 3 | Automatic reschedule | No: callback time is stored as text, nothing redials | **P1-C** |
| 4 | Not interested | Outcome recorded, contact marked; no campaign rule | **P1-B** |
| 5 | Email for job details | Email captured and validated, sent to CRM; nothing emails | **P2-K** |
| 6 | Self-improvement, control | Calls never learn on their own; training + playbook are human-curated | **P2-N** |
| 7 | Best model | Measured shortlist below; benchmark to decide | **P2-H** |
| 8 | Best STT / TTS | Comparison below | **P2-H** |
| 9 | Pipeline vs realtime | Both exist; recommend the pipeline | — |
| 10 | Automatic / bulk calling with on/off | No: manual only | **P1-B** |
| 11 | Incoming: what to say, CRM check | CRM lookup on every call; greeting not candidate-aware | **P3-Q** |
| 12 | Training vs knowledge base | Both exist; different jobs (below) | **P2-N** |
| 13 | Interruptions | Yes: the agent stops and listens (not during the disclosure) | — |
| 14 | Voicemail | Not detected: the agent talks to the machine | **P1-D** |
| 15 | Voicemail vs human, leave a message? | Not detected; recommend hang up, no message | **P1-D** |
| 16 | Caller ID, call-backs | Configurable per line; call-back routing is PBX/Telnyx config | **P1-B**, **P3-Q** |
| 17 | TPS and DNC screening | DNC (local + CRM flag) checked before dialling; no TPS | **P1-E** |
| 18 | Job distance | Not implemented | **P2-J** |
| 19 | Salary/role never guessed | Prompt rules only: no hard guarantee | **P1-F** |
| 20 | Hours, retries, mid-call failure | Hours (time of day only); no retries; crash leaves calls stuck | **P1-B**, **P1-G** |
| 21 | Live monitoring, cost caps | Live transcript + controls; no listen-in; no caps | **P1-H**, **P2-M** |
| 22 | Test without real candidates | Browser calls, test extensions, Call me, Run checks | **P2-O** |
| 23 | Transfer when interested / no human free | Transfer exists; if it fails the agent offers a callback, but no task is created | **P2-L** |
| 24 | Off-topic, not-interested callers wasting time | Silence handling only; one overall cap | **P1-H** |
| 25 | Maximum call length | 15 min overall (`max_call_seconds: 900`) | **P1-H** |

## Answers

### 1. How does the agent know who to call, what role, what to ask, and the history?
**Today.**
- Before every call, Xplogent asks the CRM `POST /api/xplogent/contact-lookup` (signed,
  1.5 s budget, fails open). It gets back **name, company, notes, last outcome and a
  do-not-call flag**.
- The questions come from the selected template (`kingsbury_outbound_screener`): right to
  work, commute, shifts, experience, must-haves, pay, start date, next step.
- Its `{{candidate_name}}`, `{{role}}` and `{{location}}` are filled from the local contact's
  fields (CSV import), **not from the CRM**.
- Nothing tells Xplogent *which* candidates to call, and history is only "last outcome".

**Plan P1-A: CRM as the source of truth.** Xplogent reads through a small, signed Laravel API.
It never gets direct MariaDB access: the API keeps the schema private and lets Laravel apply
its own rules.
- `GET /api/xplogent/call-queue?campaign=…` returns the candidates due to be called, each with
  a **call brief**:
  - candidate: id, name, mobile, postcode, consent basis;
  - the application: role id, title, client (if shareable), location/postcode, pay, hours,
    must-haves, start date;
  - **history**: previous calls with date, outcome and summary, notes, last contact, and
    whether there's an open callback.
- `contact-lookup` is extended with the same `history[]` and open `applications[]`, so
  inbound calls get the same picture.
- The brief is stored with the call (a snapshot), rendered into the prompt as **ROLE FACTS**
  and **HISTORY**, and pushed back with the summary. That gives the CRM one history, and the
  agent knows "we spoke on 12 Oct, you asked us to call after 5 pm".
- The question set comes from the campaign's template. Per-role must-haves come from the
  brief, so recruiters edit them in the CRM, not in Xplogent.

### 2. How many calls at the same time?
**Today.**
- Each SIP line takes **4 calls** (`max_calls`, adjustable), and the PBX trunk's channel
  count is a second limit.
- On your **4-core CPU server with local Whisper and Kokoro**, speech is the bottleneck:
  expect about **1–2 calls** before replies slow down (*estimate*).
- With hosted speech-to-text, voice and model, the server mostly relays audio and the limit
  moves to the line and trunk channels.

**Plan P2-I.** A load test with N simulated callers (Asterisk rig) to measure latency per
concurrent call, then a published capacity. The **target is 10 concurrent** on the current
server with hosted speech. The dialler (P1-B) never exceeds the measured number.

### 3. Can the agent reschedule automatically?
**Today: no.**
- `book_callback` stores free text ("tomorrow at 10am") on the contact and in the CRM summary.
- Nothing dials it.

**Plan P1-C.**
- `book_callback` takes an exact time: the model must turn "tomorrow at 10" into
  `2026-10-29T10:00 Europe/London` and read it back to the candidate.
- A scheduler redials at that time, inside calling hours. If the time falls outside them, the
  agent offers the nearest allowed slot during the call.
- The callback is also written to the CRM as a task, so a recruiter can see or take it.
- A missed callback goes through the retry rules (Q20).

### 4. Candidate not interested
**Today.** The outcome `not_interested` is recorded, the contact is marked, and the CRM gets
it. "Stop calling me" is different: outcome `do_not_call` puts the number on the DNC list,
permanently.

**Recommended rule (P1-B).**
- **Not interested in this role**: thank them, ask *one* optional question ("Would you like
  to hear about other roles in future?"), then end.
- No more calls for that role. Other roles are allowed only if they said yes, and not before a
  cool-off (default **90 days**).
- **Stop calling**: DNC, every campaign, permanently, and pushed to the CRM.
- Either way the call should end within about **45 seconds** of the "no" (P1-H).

### 5. Candidate gives an email for job details
**Today.** `email` is a validated field; it is sent to the CRM in the call summary. Nothing
sends an email.

**Plan P2-K.**
- The agent spells the address back ("j-o-h-n dot smith at gmail dot com — is that right?")
  and records it as **confirmed**.
- A `send_job_details` tool asks the **CRM** to send its own job-details email template.
  That keeps one sender, unsubscribe handling and an audit trail in the CRM, and lets the
  candidate's record update.
- Xplogent never writes the email itself.

### 6. Self-improvement: does it learn from calls, and how do we control it?
**Today.**
- **Phone calls never change the agent by themselves.**
- Learning is human-driven: upload recordings, scripts and Q&A (or press "Use for
  training" on a call), then **Build playbook**. Review and edit it in Call training; it
  applies to the next call.
- The dashboard chat's self-learning is separate and is now gated: no learning from small
  talk or failed tools, and you can review and forget facts.
- **Gap:** a selected recruitment template *replaces* the trained playbook instead of adding
  to it.

**Plan P2-N.**
- **Suggestions queue:** after each day, Xplogent proposes:
  - knowledge-gap questions with a draft answer;
  - objections it handled badly;
  - playbook edits (as a diff).

  A recruiter approves, edits or rejects each one. Nothing goes live unapproved.
- **Versioned playbook:** who approved what, with one-click rollback.
- **Regression check:** before a new playbook version goes live, the scripted test
  conversations (P2-O) must still pass.
- **Template plus playbook:** the template keeps the rules and questions; the trained
  playbook adds tone, objections and FAQs.

### 7. Best model: fast, accurate, low cost
Estimated model cost per **4-minute screening call** (~15 turns, ~60k input + 1k output
tokens), from the Vercel AI Gateway's live price list, all with tool support:

| Model | Input / output per 1M | Model cost per call | Notes |
|---|---|---|---|
| anthropic/claude-haiku-5.5 | $0.10 / $0.50 | ~$0.007 | strong instruction-following; to benchmark |
| openai/gpt-6-luna | $0.10 / $0.50 | ~$0.007 | to benchmark |
| google/gemini-3.5-flash-lite | $0.30 / $2.50 | ~$0.021 | |
| google/gemini-3.6-flash | $0.75 / $3.75 | ~$0.049 | measured live: tool call correct, 2.2 s |
| nvidia/nemotron-3-ultra (free, OpenRouter) | free | $0 | measured 1.8 s; rate-limited (50/day free) |
| local qwen/llama on your 4-core CPU | $0 | $0 | **too slow for calls** (5–20 s first word measured) |

**Recommendation.**
- The model is the *cheapest* part of a call: under 5 cents even for the dearer ones.
- Choose by **tool-calling accuracy and first-word delay**, not price: run the P2-H benchmark
  on Claude Haiku 5.5, GPT-6 Luna and Gemini 3.6 Flash through your Vercel key.
- Keep Gemini 3.5 Flash-Lite or a free model as the **backup** model.

### 8. Speech-to-text and voice: quality, speed, cost
| Service | Kind | Price | Notes |
|---|---|---|---|
| Deepgram Nova-3 (streaming) | STT | $0.0048/min | [deepgram.com/pricing](https://deepgram.com/pricing); already supported, streams while the caller talks |
| Deepgram Flux | STT | $0.0065/min | built for voice agents (turn detection) |
| ElevenLabs Scribe v2 Realtime | STT | $0.39/h (~$0.0065/min), ~150 ms | [elevenlabs.io/pricing/api](https://elevenlabs.io/pricing/api) |
| Local Whisper (small.en on CPU) | STT | $0 | ~1–2 s per utterance on 4 cores; fine for tests |
| Deepgram Aura-2 | voice | $0.030 / 1k chars | |
| ElevenLabs Flash | voice | $0.04 / 1k chars, ~75 ms | best natural British voices; already supported |
| Cartesia Sonic | voice | ~$0.045 / 1k chars (Startup overage) | [cartesia.ai/pricing](https://cartesia.ai/pricing); already supported |
| Local Kokoro | voice | $0 | good quality; CPU limits concurrency |

The agent speaks about 1,400 characters in a 4-minute call. Typical hosted speech cost is then
about **$0.02 (speech-to-text) + $0.04–0.06 (voice)** per call.

**Recommendation:** **Deepgram Nova-3 streaming + ElevenLabs Flash** (British voice), with
local Kokoro as the fallback voice. Confirm in the P2-H benchmark with recorded test
candidates (accents, noisy lines).

### 9. Pipeline or realtime speech-to-speech?
**Use the pipeline** (speech-to-text → model → voice).
- **Exact control:** the disclosure is word for word, the pay guard (Q19) can check each
  sentence before it's spoken, and the field guard and transcript keep working.
- **Free choice of parts:** model, voice and accent can each be swapped.
- **Lower cost per minute.**

Realtime (OpenAI gpt-realtime, already built in as an option) is more natural in turn-taking,
but:
- every sentence is generated and spoken in one step, so it can't be checked first;
- it's cloud-only;
- third-party estimates range widely, from about $0.04 to $0.45 a minute. OpenAI bills
  per audio token and doesn't publish a per-minute price.

Revisit after the pipeline is in production.

### 10. Automatic and bulk calling with an on/off switch
**Today: manual only.**

**Plan P1-B: campaigns and a dialler.**
- **A campaign** = a role (or a CRM saved search) + a template + a caller ID line + rules.
- **Controls:** **On/Off** per campaign, plus a global **Stop all** kill switch.
- **Pacing:** maximum concurrent calls and calls per minute.
- **Rules:** calling window, retry rules, and a daily and per-campaign **cost cap** that
  pauses the campaign.
- **Before each dial:** DNC, TPS (P1-E), the CRM's latest status and consent are re-checked,
  never a stale list.
- **Dashboard:** queued / called / connected / interested / not interested / voicemail /
  failed, with **Pause** and **Resume**.
- **Bulk "cold" calling** of candidates who never applied needs the legal check in P1-E.

### 11. Incoming calls: what to say, and CRM check?
**Today.** Every inbound call is checked against the CRM (and the local contacts) while it
rings. The greeting is the template's or the configured one, with the AI disclosure always
added.

**Plan P3-Q.**
- **Known candidate with an open application:** "Hello Sarah, I'm an AI assistant at
  Kingsbury Personnel — this call is recorded. Are you calling about the care assistant role
  in Leeds?" History is available to the agent.
- **Known, but nothing open:** register interest, then a recruiter callback.
- **Unknown:** the inbound template ("a job for yourself, or staff for your company?").
- **Calling back after a missed AI call:** "You missed our call about …". The CRM history
  shows the missed call.

### 12. Call training vs knowledge base
- **Knowledge base: *what* is true.** Facts the agent looks up per question: company,
  policies, FAQs, standard pay bands, process. It answers only from it.
- **Call training: *how* to talk.** Past recordings and scripts are distilled into the
  **playbook**: tone, the order of questions, how good recruiters handle objections. The
  transcripts are also searchable.
- **Why both:** knowledge without training gives right facts in a robotic style; training
  without knowledge gives a convincing style with no facts to stand on.
- **Role-specific facts** (this job's pay and hours) belong in neither: they come from the
  CRM brief (P1-A).

### 13. Interruptions
**Yes.** When the caller starts speaking, the agent stops mid-sentence, clears queued audio
and listens. The only exception is the greeting with the AI and recording disclosure, which
always plays to the end. The sensitivity is tunable (`voip.vad`).

### 14 and 15. Voicemail: detect it, and leave a message?
**Today: not detected.** The agent greets the answering machine as if it were a person.

**Plan P1-D.**
- **Telnyx calls:** use Telnyx's answering-machine detection on dial.
- **SIP/FreePBX calls:** detection in Xplogent:
  - a long uninterrupted greeting in the first 3–6 s;
  - phrases like "leave a message" or "not available" (from the speech-to-text);
  - the beep tone.
- **On voicemail:** **hang up without leaving a message** (recommended default). Record
  outcome `voicemail` and retry per the rules; the CRM can follow up by SMS or email.
- **Why no message:** a pre-recorded or synthetic message left on voicemail is the kind of
  "automated call" UK rules restrict, and it could disclose that the person applied for a
  job to whoever hears it.
- **Optional:** a short, neutral human-recorded message ("This is Kingsbury Personnel,
  please call us on …"), only if your legal review says it's fine.

### 16. Which number shows, and what happens on call-back?
**Today.**
- The caller ID is set per line: Telnyx `from_number`, the SIP trunk's `caller_id`, or the
  PBX outbound route.
- UK rules (ICO): your number, or another contact number, must be shown to the person you
  call. On request you must give a contact address or freephone number.

**Plan.**
- Use **one dedicated DID** for AI calls, set on the campaign (P1-B).
- **Inbound on that DID** goes to the AI with CRM context (P3-Q): "You missed our call about
  the care role". The candidate can ask for a person anytime, which transfers to the
  recruiters' ring group. Outside office hours the agent books a recruiter callback.
- Configure it in FreePBX as an inbound route on that DID → the AI extension, with the
  recruiters' ring group as fail-over.

### 17. TPS and do-not-call screening
**Today.** Before every dial the **local DNC list** and the **CRM's do-not-call flag** are
checked; any "stop calling me" adds the number automatically. There is **no TPS/CTPS
check**.

**Plan P1-E.**
- **Screening:** a TPS/CTPS check through a licensed screening provider's API before every
  campaign dial, cached for 28 days (the TPS file's refresh window).
- **Consent:** each candidate carries a **consent basis** from the CRM (applied via a job
  board for this role, registered with us, or cold). The dialler applies rules per basis.
  ICO guidance: you must not make marketing calls to TPS numbers without specific consent.
- **Legal review (gate before go-live):** whether your outbound AI calls are "direct
  marketing" (cold candidates likely, applicants about their own application likely not).
  Also whether an interactive AI voice counts as an "automated call" under PECR, which needs
  prior specific consent. ICO's page only discusses recorded-message systems. I can build the
  controls, but this needs a lawyer's answer, not mine.

### 18. Job distance: straight line or travel time?
**Today: not implemented.**

**Plan P2-J.**
- **Travel time, not straight line**: 10 miles across a city can be 15 minutes or an hour.
- Postcodes come from the CRM brief. Postcode → coordinates comes from **postcodes.io**
  (free, UK).
- Travel time by car and by public transport comes from a routing API (e.g. TravelTime or
  Google Distance Matrix), computed once per candidate and job and cached.
- The agent says it as a fact from the brief: "it's about 35 minutes by bus from your
  postcode". It never estimates distance itself.
- Ideally the CRM already stores the result and passes it in the brief.

### 19. Guarantee salary and role details come from the CRM
**Today.** The prompt and templates tell the model to answer only from the knowledge and
script and never guess pay. That is strong, but **not a guarantee**.

**Plan P1-F: a hard guard.**
1. Role facts (pay, hours, location, start, client) come **only** from the CRM brief, as a
   structured ROLE FACTS block, marked as the only allowed source.
2. **Number guard before speaking:** each sentence is checked before it reaches the voice.
   Any money amount, hourly rate, salary, date or hour figure that isn't in ROLE FACTS (or
   the knowledge base) is **not spoken**. The agent says "the recruiter will confirm the
   exact figure", and the event is logged for review.
3. A daily audit of all pay statements against the briefs, shown on the dashboard.

### 20. Calling hours, retries, mid-call failure
**Today.**
- Calling hours are 09:00–20:00 in the contact's time zone, by **time of day only**: no
  weekdays and no bank holidays.
- There are no retries.
- If the model, voice or speech-to-text fails, a backup takes over **when one is
  configured** (Settings → Backup model, `voip.tts_fallback`, the speech-to-text fallback);
  if a transfer fails, the agent resumes the call.
- **If the server dies mid-call**, the call stays marked "active" forever and no summary is
  sent.

**Plan.**
- **Hours (P1-B):**
  - Mon–Fri 09:00–20:00, Sat 10:00–16:00, no Sundays or UK bank holidays (per-campaign
    override);
  - candidate's preferred time if the CRM has one.
- **Retries (P1-B):**
  - no answer / busy → up to **3 attempts** over **3 days** at different times
    (+2 h, next day afternoon, day 3 morning);
  - voicemail counts as no answer;
  - then mark "unreachable" and hand to the CRM.
- **Failures (P1-G):**
  - on restart, calls left "active" are closed as `interrupted`, their summary (with
    transcript so far) is sent to the CRM, and a callback is queued with an apology;
  - if the line drops mid-call: one retry within 10 minutes if the screen wasn't finished;
  - health alerts (email or webhook) when calls fail repeatedly.

### 21. Supervisor monitoring and cost caps
**Today.**
- Supervisors see live calls with a **live transcript**.
- Controls: **Pause AI** (take over by typing), **Say**, **Instruct**, **Transfer** and
  **Hang up**.
- There is **no live audio listen-in** and **no cost caps** (only a per-minute estimate).

**Plan.**
- **Cost caps (P1-H):** per campaign, a daily cap and a maximum spend per call. At 80 % the
  dashboard warns; at 100 % the campaign pauses (calls in progress finish).
- **Live audio listen-in (P2-M):** with Whisper (agent only) and Barge (join the call), plus
  alerts when a call goes long or the candidate sounds upset.
- **Suggested starting caps:** £0.25 per call, £20 per campaign per day while piloting.

### 22. Test without calling real candidates
**Today.**
- **Browser test calls** (text or microphone, with the real voice).
- **Run checks**, which tests model, backup, voice, hearing, microphone and connection.
- **Call me** to your own phone or extension.
- **Test extensions** answered while the agent is off.
- **Demo mode.**

Test calls are never counted or sent to the CRM.

**Plan P2-O.**
- **Candidate simulator:** text-only conversations with scripted AI "candidates":
  - interested, not interested, rude, off-topic;
  - asks about pay, gives a wrong number, wants a person, says "stop calling".

  Each run is scored automatically on whether it asked every required question, made no pay
  guesses, used the right tools, and how long it took.
- **Test campaign:** the full dialler against internal extensions, with fake briefs from a
  CRM **sandbox**, so nothing touches real records.

### 23. Interested candidate → recruiter, or callback if nobody's free
**Today.**
- `transfer_to_human` transfers to the hand-off number.
- If the transfer fails, the agent says so and offers a callback, recorded as text.

**Plan P2-L.**
- **Warm transfer:** the recruiter first hears a 5-second whisper ("Screened candidate,
  Sarah, care assistant Leeds, right to work yes, starts in 2 weeks"), then is connected.
- **Availability:** within recruiter hours, ring the recruiters' ring group for 20 s.
- **If nobody answers**, the agent books a **recruiter callback** in a real slot. Slots come
  from the CRM (or a fixed rota). The callback is a **CRM task** assigned to the
  role's recruiter, and the candidate hears the exact time.

### 24. Not interested, or off-topic: when to end
**Plan P1-H.**
- **Not interested:** close within ~45 s of the clear "no" (thank, one optional question,
  goodbye).
- **Off-topic:** after **2 polite redirects**, the agent ends the call ("I'll let you go —
  if you'd like to talk about the role later, call us on …"). It never argues.
- **Silence** (exists): 2 prompts over ~30 s, then goodbye.
- Each of these closes is logged with its own outcome, so you can see how often it happens.

### 25. Maximum call length
- **Today:** 15 minutes, for every call.
- **Recommended:** **8 minutes** for screening calls (a good screen takes 3–5).
  - At **7 minutes** the agent starts wrapping up and offers a recruiter callback for anything
    left.
  - At 8 minutes it ends politely.
- **Inbound:** 10 minutes.
- **Plan P1-H:** caps set per campaign or template, with the warning built in.

## The plan

**Phase 1 — must have before production outbound calling**

| Item | What | Size |
|---|---|---|
| P1-A | CRM call brief and history: Laravel `call-queue` and the extended `contact-lookup`; brief snapshot per call; ROLE FACTS and HISTORY in the prompt; Laravel reference code in `docs/crm-laravel.md` | L |
| P1-B | Campaigns and dialler: on/off per campaign plus a global stop; pacing; weekday and bank-holiday hours; retry rules; not-interested cool-off; per-campaign caller ID; dashboard | L |
| P1-C | Exact-time callbacks: structured time, read-back, scheduler redial within hours, CRM task | M |
| P1-D | Voicemail detection (Telnyx AMD; heuristics on SIP); hang up, outcome `voicemail`, retry | M |
| P1-E | Compliance gate: TPS/CTPS screening via provider API (cached 28 days); consent basis per candidate; legal sign-off checklist | M |
| P1-F | Pay and role guard: ROLE FACTS only; sentence check before speaking; audit report | M |
| P1-G | Crash recovery: stale calls closed as `interrupted`, CRM summary, apology callback; failure alerts | S |
| P1-H | Time and cost limits: per-template max length with a wrap-up warning; off-topic limit; quick close on "no"; per-call and per-campaign cost caps that pause | M |

**Phase 2 — quality and scale (during the pilot)**

| Item | What | Size |
|---|---|---|
| P2-H | Benchmark harness: models (Haiku 5.5, GPT-6 Luna, Gemini 3.6 Flash), STT and voice; first-word delay, tool accuracy, pay-guard hits, cost per call | M |
| P2-I | Load test and published capacity; dialler concurrency set from it | S |
| P2-J | Travel time: postcodes.io plus a routing API, cached; spoken only from the brief | M |
| P2-K | Email job details through the CRM (`send_job_details`), with spelled-back confirmation | S |
| P2-L | Warm transfer with whisper; recruiter availability; CRM callback task in a real slot | M |
| P2-M | Supervisor live listen, whisper and barge; long or upset-call alerts | M |
| P2-N | Learning with control: suggestions queue, versioned playbook with rollback, template plus playbook | M |
| P2-O | Candidate simulator and regression suite; test campaign on a CRM sandbox | M |

**Phase 3 — inbound polish**

| Item | What | Size |
|---|---|---|
| P3-Q | CRM-aware inbound greeting (known candidate, open application, missed AI call); dedicated DID routing guide | S |

**Pilot proposal:**
- P1 complete, then 2 weeks on one campaign (one role).
- Up to 50 calls a day, a recruiter reviewing every call summary.
- £20 a day cap.
- Then widen.

## Decisions needed from you
1. **CRM access:** a small signed Laravel API (recommended) or read-only database access?
   This also covers who builds the Laravel side: your team, or I write the code to drop in.
2. **Speech and model:** OK to use paid hosted speech (Deepgram + ElevenLabs) and the Vercel
   gateway for the model in production?
3. **Voicemail:** hang up without a message (recommended), or a short neutral message after
   legal review?
4. **TPS provider:** which screening service (you'll need an account), and the legal review
   of PECR and consent for AI calls.
5. **Rules:** confirm the defaults:
   - hours Mon–Fri 09–20, Sat 10–16;
   - 3 attempts over 3 days;
   - 8-minute screening cap;
   - 90-day cool-off;
   - £0.25 per call and £20 per day pilot caps.
6. **Recruiter hand-off:** which ring group or extensions, the hours, and where callback slots
   come from (CRM calendar or a fixed rota).
