# Prompt for the CRM session (copy everything below the line)

---

You are working in the Laravel CRM repository **`mubashiribstec/crm_laravel_sass`** (Laravel, MariaDB/MySQL, PHPUnit, Spatie permissions, Sanctum). It is a UK recruitment CRM. Models live under the **`Horsefly\`** namespace (for example `Horsefly\Applicant`, `Horsefly\Sale`, `Horsefly\CVNote`, `Horsefly\CallLog`, `Horsefly\DialLock`, `Horsefly\ModuleNote`, `Horsefly\JobTitle`).

**Goal.** Add a small, signed API that lets **Xplogent**, our AI phone agent, run recruitment screening calls from this CRM, plus a CRM page for controlling those calls:
- get the candidates to call for a vacancy, with the vacancy details and candidate history;
- look up a caller;
- write each call's result back exactly the way a recruiter records it;
- send the vacancy email;
- take the same dial lock a recruiter takes;
- let an employee click a number on a CRM page and have Xplogent call it.

**Read the existing code before writing.** Reuse the CRM's own models, helpers and rules. Do not change existing screens or behaviour. Where logic must be shared, **extract it into a service class and call that service from both the old code and the new API**, so the two can never drift apart. No secrets in the repository. New settings go in `.env` / `config/xplogent.php` with **no real default values**. Every endpoint must be covered by PHPUnit feature tests (below). Work on a branch and open a pull request.

**Verified facts about this repo (checked 2026-11-02 — confirm them by reading the files, do not take them on trust):**
- `app/Http/Controllers/SaleController.php` **`getApplicantsBySaleRadius`** exists (lines 3095–3633 on `main`). It is the radius-matching screen's DataTables endpoint. Section 4.2 below tells you to clone it — read it first.
- `Horsefly\Applicant` has a **`phoneMatches`** scope (primary, secondary, landline; last-10-digit match) — see `app/Http/Controllers/CallLogController.php` (`lookup`, `store`, `desktopLookup`, `desktopStore`).
- `Horsefly\DialLock` has **`keyFor($number)`** (phone key) and rows carry `phone_key` + `expires_at` — see `CallLogController::desktopStore`, which releases the lock when a desktop call ends.
- `Horsefly\CallLog` is created with `user_id, applicant_id, caller_number, caller_name, direction, duration_seconds, sip_call_id, called_at, source` — see `CallLogController::store`.
- Notes use the suffix format `' --- By: <name> Date: d-m-Y'` — see `SaleController::storeSaleNotes`.
- Existing API routes (`routes/api.php`): the xplosip softphone widget (`GET /api/contacts/lookup`, `POST /api/calls/log`, Sanctum) and Desktop MicroSIP (`/api/sip/{token}/lookup`, `/api/sip/{token}/calls`, shared token in the URL). **None of these are Xplogent. Do not reuse their auth.** On `dev` there is also an external portal feed (`GET /api/sales/open`, `/sales/details/{id}`, `/regions/active`, Bearer `PORTAL_API_TOKEN`) — that is a portal feed, not Xplogent.
- Applicant flag columns (seen in `SaleController`): `is_blocked`, `is_temp_not_interested`, `is_no_job`, `is_callback_enable`, `is_no_response`, `is_circuit_busy`, `is_in_nurse_home`, `paid_status`, `have_nursing_home_experience`, `status`, `deleted_at`. Applicant contact columns: `applicant_name`, `applicant_email`, `applicant_email_secondary`, `applicant_phone`, `applicant_phone_secondary`, `applicant_landline`, `applicant_postcode`, `applicant_experience`, `applicant_notes`, `lat`, `lng`, `job_title_id`, `job_category_id`, `job_source_id`.
- Sale columns (seen in `SaleController`): `job_title_id`, `lat`, `lng`, `status`, `is_on_hold`, `cv_limit`, `position_type`, `job_type`, `timing`, `salary`, `experience`, `qualification`, `benefits`, `job_description`, `postcode`.
- `CVNote` rows carry `sale_id`, `applicant_id`, `status` (`1` = CV sent). `applicants_pivot_sales` links applicants to vacancies with `is_interested`. Latest note is `COALESCE(module_notes, applicant_notes)` — `module_notes` rows use `module_noteable_id` + `module_noteable_type = 'Horsefly\Applicant'` with `details` + `created_at`; `applicant_notes` rows use `applicant_id` with `details` + `created_at`.

**Correction — an earlier draft of this prompt was written for a different repo (`mubashiribstec/CRM`) and named helpers that do NOT exist here.** Do not assume these names: `storeShortNotes`, `sendEmailsToApplicants`, `App\Support\PhoneNumber`, and the `send_job_vacancy_details` template name were all checked and do not exist in this repo. (`getApplicantsBySaleRadius` is the one exception — it DOES exist, see above.) For the note/flag writing, the vacancy-email sending, and the CV file storage, **find the real mechanism in THIS repo first** (read the controllers, models and templates that do those jobs today), reuse it, and name what you found in the pull request. If a mechanism does not exist, build the minimal version the API needs and say so in the pull request.

## 1. Settings and the AI user

`config/xplogent.php`, reading:

| Env | Meaning |
|---|---|
| `XPLOGENT_API_TOKEN` | shared secret (≥ 32 random chars); no default. If empty, every `/api/xplogent/*` call returns 503 |
| `XPLOGENT_AI_USER_ID` | id of a `users` row named **"Xplogent AI"** (no login: random unusable password, no roles). Every note, call log, history row and dial lock the API writes uses this user, so the audit trail shows the AI |
| `XPLOGENT_SHARE_CLIENT_NAME` | `false` by default. If false, the vacancy brief does **not** include the office or unit names |
| `XPLOGENT_MAX_SKEW` | allowed timestamp skew in seconds, default 300 |
| `XPLOGENT_TIMEZONE` | `Europe/London`; times in notes and the `callback_at` display use it (the app itself is UTC) |

Add an artisan command `xplogent:make-ai-user` that creates that user idempotently and prints its id.

## 2. Authentication (all endpoints)

All endpoints are `POST`, with a JSON body, under `/api/xplogent/`, in `routes/api.php`, behind a new middleware `VerifyXplogentSignature` plus `throttle:120,1`.

Every request carries:
```
Authorization: <redacted>
X-Xplogent-Timestamp: <unix seconds>
X-Xplogent-Signature-V2: sha256=<hex HMAC-SHA256 of "<timestamp>.<raw body>", key = XPLOGENT_API_TOKEN>
Content-Type: application/json
```

The middleware rejects with **401** when:
- the token or signature doesn't match: compare with `hash_equals`;
- the timestamp isn't digits;
- the timestamp is more than `XPLOGENT_MAX_SKEW` seconds from now.

It rejects with **413** when the body is larger than 256 KB.

This is exactly the scheme in https://github.com/mubashiribstec/xplogent/blob/main/docs/crm-laravel.md (Xplogent already signs this way).

**Responses:**
- Always JSON, with `"api_version": 2`.
- Errors: `{"ok": false, "error": "<code>", "message": "<human text>"}`, with 4xx/5xx.
- Never return stack traces.
- **Log** each request without personal data: endpoint, applicant_id, sale_id, status and duration only.

## 3. Phone numbers
- The CRM stores UK national numbers (`07…`, 11 digits); Xplogent uses E.164 (`+447…`).
- **Responses** give `phone_e164`: `07xxxxxxxxx` → `+447xxxxxxxxx`, `01…`/`02…`/`03…` → `+44…`. Skip numbers that can't be converted.
- **Requests** may carry either form; match with `Applicant::phoneMatches` (primary, secondary, landline; last-10-digit match).
- Report which number matched: `phone_kind` = `mobile` | `secondary` | `landline`.

## 4. Endpoints

### 4.1 `POST /api/xplogent/sales/open`: vacancies for the campaign picker
Request: `{"search": "care leeds", "limit": 50}`. Both fields are optional.

Returns open vacancies (`sales.status = 1`, `is_on_hold = 0`, not deleted), newest first:
```json
{"api_version": 2, "sales": [{
  "sale_id": 45, "job_title": "Care Assistant", "job_category": "Care",
  "postcode": "LS6 2AB", "postcode_district": "LS6",
  "office_name": "Oak Care Group", "unit_name": "Oak House",
  "position_type": "Full time", "salary": "£12.60 per hour",
  "cvs_sent": 3, "cv_limit": 8, "created_at": "2026-10-20T09:12:00Z"}]}
```
These names are for staff in the Xplogent dashboard: they're never spoken unless `XPLOGENT_SHARE_CLIENT_NAME` is true. `cvs_sent` counts `cv_notes` rows with `status = 1` for the vacancy, the same count the CRM already shows. (There is a `GET /api/sales/open` portal feed on `dev` — do not reuse it; this endpoint is a separate signed POST with the shape above.)

### 4.2 `POST /api/xplogent/campaigns/queue`: who to call for a vacancy — CLONE `getApplicantsBySaleRadius`
Request:
```json
{"sale_id": 45, "radius_km": 15, "limit": 50, "exclude_called_within_days": 14}
```

Refuse with **409** `{"error": "sale_not_callable", "message": …}` if the vacancy is closed, on hold, deleted, or has reached `cv_limit`.

**Clone `getApplicantsBySaleRadius` (`app/Http/Controllers/SaleController.php`, lines 3095–3633 on `main`). Read it first. Extract its applicant query into a service class (for example `App\Services\Xplogent\CandidateQueueService`) and call that service from BOTH the existing controller and this new endpoint, so the two can never drift apart. The existing radius screen must keep working exactly as today.**

Reuse from it, unchanged:
- the **distance formula** (spherical law of cosines, kilometres):
  `ACOS(SIN(lat * PI()/180) * SIN(lat * PI()/180) + COS(lat * PI()/180) * COS(lat * PI()/180) * COS((lon - lng) * PI()/180)) * 180/PI() * 60 * 1.852` AS `distance`, with `having('distance', '<', $radius)` — the sale's `lat`/`lng` are the centre, `$radius` is `radius_km` from the request (default 15);
- the **job-title matching**: the vacancy's `job_title_id` title plus its `related_titles` (JSON), lowercased, mapped to `job_title` ids, then `whereIn('applicants.job_title_id', $jobTitleIds)`;
- the **base filters**: `applicants.status = 1`, not deleted, `is_in_nurse_home = 0`;
- the **latest note**: `COALESCE` of the latest `module_notes` row (`module_noteable_type = 'Horsefly\Applicant'`) falling back to the latest `applicant_notes` row, for both `details` and `created_at`.

Do NOT copy from it: the DataTables response, the DataTables sorting/search, the `status_filter` branches, the checkbox/job columns, or the `paid_status_order` CASE. This endpoint returns plain JSON, not a DataTables payload.

Then **exclude** (count each reason in `skipped`):
- `is_blocked = 1` → `blocked`;
- `is_temp_not_interested = 1` → `not_interested`;
- `is_no_job = 1` → `no_job`;
- `paid_status = 'close'` → `closed`;
- a `cv_notes` row for this vacancy (any status) → `already_sent`;
- `applicants_pivot_sales.is_interested = 1` for this vacancy → `already_interested`;
- an unexpired `dial_locks` row for any of the applicant's numbers → `locked`;
- a `call_logs` row by the AI user for this vacancy within `exclude_called_within_days` → `recently_called`;
- no convertible phone number → `no_phone`.

Order: distance ascending, then never-called before called. Return up to `limit`:
```json
{"api_version": 2,
 "sale": {"sale_id": 45, "job_title": "Care Assistant", "job_category": "Care",
          "position_type": "Full time", "job_type": "regular",
          "timing": "Days, 3 x 12h shifts incl. alternate weekends",
          "salary": "£12.60 per hour", "experience": "6 months in care",
          "qualification": "Care Certificate preferred", "benefits": "Paid training",
          "description": "<plain text, HTML stripped, max 800 chars>",
          "postcode_district": "LS6",
          "client_name": null, "site_name": null,
          "cvs_sent": 3, "cv_limit": 8},
 "candidates": [{
   "applicant_id": 123, "name": "Sarah Jones", "first_name": "Sarah",
   "phone_e164": "+447700900123", "phone_kind": "mobile",
   "postcode_district": "LS7", "distance_km": 4.2, "distance_miles": 2.6,
   "job_title": "Care Assistant",
   "flags": {"callback": false, "no_response": false, "circuit_busy": false},
   "last_note": "<latest note, max 300 chars>", "last_note_at": "2026-10-12T15:02:00Z",
   "last_called_at": null}],
 "skipped": {"blocked": 4, "not_interested": 2, "no_job": 1, "closed": 0, "already_sent": 3,
             "already_interested": 0, "locked": 1, "recently_called": 6, "no_phone": 0}}
```
- `client_name` / `site_name` are filled only when `XPLOGENT_SHARE_CLIENT_NAME=true`.
- **Never** return the CV file, date of birth, gender, emails or full postcode in this response: the AI doesn't need them.

### 4.3 `POST /api/xplogent/contact-lookup`: who is this caller / candidate
Request:
```json
{"phone": "+447700900123", "direction": "inbound", "line": "extension",
 "original_called_number": "", "sale_id": 45}
```
`sale_id` is optional; it's present on campaign calls.

- **No match:** 404 `{"ok": true, "found": false}`.
- **Match:** 200 with the candidate:
```json
{"api_version": 2, "found": true, "contact": {
  "applicant_id": 123, "name": "Sarah Jones", "first_name": "Sarah", "company": "",
  "phone_kind": "mobile", "postcode_district": "LS7", "job_title": "Care Assistant",
  "do_not_call": false,
  "flags": {"blocked": false, "temp_not_interested": false, "callback": true,
            "no_response": false, "no_job": false, "circuit_busy": false},
  "notes": "<latest note, max 500 chars>", "last_outcome": "callback",
  "callback_note": "<latest callback note, max 300 chars or empty>",
  "cv_excerpt": "<plain-text excerpt of the candidate's CV, max 2000 chars>",
  "history": [{"date": "2026-10-12", "sale_id": 45, "job": "Care Assistant — LS6",
               "stage": "crm", "sub_stage": "crm_interview_attended"}],
  "applications": [{"sale_id": 45, "job": "Care Assistant — LS6", "interested": true,
                    "latest_stage": "quality", "latest_sub_stage": "quality_cvs"}],
  "interviews": [{"sale_id": 45, "date": "2026-10-30", "time": "10:00", "status": 1}],
  "calls": [{"at": "2026-10-12T15:00:00Z", "direction": "outbound", "duration_s": 182,
             "by": "Xplogent AI", "sale_id": 45}]}}
```
- `do_not_call` = `is_blocked`.
- `last_outcome` is the latest note's outcome (e.g. `callback`, `not interested`, `no job`, `no response`), or empty.
- `history`: the latest 5 rows. `applications`: the latest 5. `interviews`: upcoming and the last 2. `calls`: the latest 5 `call_logs` rows.
- Job labels use the postcode district, never the client name (unless sharing is allowed).
- `cv_excerpt`: exception to the data-minimisation rule. The CV is stored in the CRM as a DOC, DOCX, or PDF file. **Find where CV files are stored in this repo, extract plain text from the file server-side, and return it here, capped at 2,000 characters** (experience, qualifications, current role). Never include date of birth, gender, emails, or the full postcode. The CV file itself is never sent to the agent. Name the parser library you use in the pull request.
- Same data-minimisation rule as 4.2 otherwise.
- **Must answer in under 300 ms** on production data: use the indexed phone columns and `LIMIT`ed sub-queries.

### 4.4 `POST /api/xplogent/call-summary`: write the call result back
Xplogent sends this once per finished call and retries on failure. **It must be idempotent on `call_id`.** Body (unknown fields must be ignored):
```json
{"event": "call.completed", "call_id": 1234, "test": false,
 "direction": "outbound", "from": "+441132000000", "to": "+447700900123",
 "applicant_id": 123, "sale_id": 45, "campaign_id": 7,
 "started_at": "2026-10-28T10:15:22+00:00", "duration_s": 187,
 "outcome": "callback", "final_attempt": false,
 "callback_at": "2026-10-29T17:30:00+00:00",
 "summary": "Interested; asked to be called after 5pm tomorrow.",
 "answers": {"right_to_work": "British citizen", "commute": "Bus, 25 min",
             "shifts": "yes", "start_date": "2 weeks notice"},
 "email_confirmed": "sarah.jones@example.com",
 "do_not_call": false, "transcript": [{"speaker": "agent", "text": "…"}],
 "provider_call_id": "abc@xplogent", "recording_available": true}
```
Older Xplogent builds send this without `applicant_id`/`sale_id`: then match the applicant by phone (`to` on outbound, `from` on inbound), as `docs/crm-laravel.md` describes.

**Behaviour:**
1. **`test: true`:** return 200 `{"ok": true, "test": true}` and write nothing.
2. **Idempotency:** a new table `xplogent_call_events` (`id`, `call_id` unique, `applicant_id`, `sale_id`, `outcome`, `payload` json, `created_at`). If `call_id` exists, return 200 `{"ok": true, "duplicate": true}` and write nothing else.
3. **Always**, in one DB transaction:
   - a `call_logs` row: `user_id` = AI user, `applicant_id`, `sale_id`, `caller_number` = the candidate's number in national form, `direction`, `duration_seconds`, `sip_call_id` = `provider_call_id`, `called_at` = `started_at`;
   - the `xplogent_call_events` row.
4. **Per outcome**, using the **same flag-and-note logic the CRM already uses when a recruiter records a call outcome** — find that logic in this repo (it is NOT a method called `storeShortNotes`; read the controllers that write applicant notes and set `is_blocked` / `is_temp_not_interested` / `is_no_job` / `is_callback_enable` / `is_no_response` / `is_circuit_busy`), extract it into a service if it is inline, and call that service from both the old code and this endpoint, so the two can never drift apart. The service writes the flags, the note, and the audit exactly like today. The mapping is:

| `outcome` | CRM write |
|---|---|
| `interested` | `applicants_pivot_sales` updateOrCreate(applicant, sale) with `is_interested = 1`; reason **`casual`** (clears the temporary flags) with the summary note. **Never** create `cv_notes` or move a stage: a recruiter does that. |
| `callback` | reason **`callback`**; the note includes `callback_at` shown in Europe/London ("Callback Wed 29 Oct 17:30") |
| `recruiter_callback` | as `callback`, with the note prefixed `RECRUITER CALLBACK:` (a human must call, not the AI) |
| `not_interested` | reason **`not_interested`** |
| `no_job` | reason **`no_job`** |
| `do_not_call` | reason **`blocked`** |
| `no_answer`, `voicemail`, `busy` | call log only. If `final_attempt: true`: reason **`no_response`**, with a note listing the attempts |
| `number_unobtainable` | reason **`circuit_busy`** |
| `wrong_person`, `transferred`, `screen_incomplete`, `interrupted`, `other` | note only, no flag change |

5. **Note text** follows the existing format (see `SaleController::storeSaleNotes` for the suffix pattern). Example:
   `AI call (callback): <summary>. Answers: Right to work: …; Commute: …; Shifts: …; Start: … --- By: Xplogent AI Date: 28-10-2026`
   - Cap it at 2,000 characters.
   - **Never store the transcript in notes.** It stays in `xplogent_call_events.payload`.
6. **`email_confirmed`:** if it differs from both stored emails, add it to the note ("Candidate gave email …"). **Don't overwrite** `applicant_email`.
7. Return `{"ok": true, "call_log_id": …}`.
8. **Recording is optional.** `recording_available` is informational only — the PBX already records the call, so the CRM does not need to fetch the agent's recording.
9. **Multiple dial attempts:** each attempt is a separate call with its own `call_id`, so each attempt gets its own `call_logs` row (idempotency is per `call_id`). The CRM page lists the attempts under the candidate — it does not overwrite a single recording. Each attempt's PBX recording is found via that attempt's call record (time + `sip_call_id`).

### 4.5 `POST /api/xplogent/send-job-details`: email the vacancy to one candidate
Request:
```json
{"applicant_id": 123, "sale_id": 45, "email": "sarah.jones@example.com", "call_id": 1234}
```
- Refuse with **409** `blocked` if the applicant `is_blocked`.
- Validate the email (RFC + DNS like the existing rules).
- Fill the **vacancy email template exactly as the CRM already sends vacancy emails today** — find the template and the sending code in this repo (it is NOT a template called `send_job_vacancy_details` or a method called `sendEmailsToApplicants`; read the mail/template code, reuse it, and name what you found in the pull request), using the "Xplogent AI" user name as `(agent_name)`.
- Send it through the existing mail and SMTP settings, and record it in `sent_emails` as other sends are.
- Return `{"ok": true, "sent_email_id": …}`.
- Rate-limit: at most 3 per applicant per day.

### 4.6 `POST /api/xplogent/dial-lock` and `POST /api/xplogent/dial-unlock`
- **`dial-lock`** request: `{"phone": "+447700900123", "applicant_id": 123, "ttl_seconds": 900}`.
  - Use `Horsefly\DialLock::keyFor` and the `DialLock` model, owned by the AI user.
  - Success: 200 `{"locked": true, "expires_at": …}`.
  - Already locked by someone else and unexpired: 409 `{"locked": false, "locked_by": "<user name>", "expires_at": …}`.
  - Recruiters' existing click-to-dial must show the AI's lock like any other ("being called by Xplogent AI").
- **`dial-unlock`** request: `{"phone": "+447700900123"}`. It removes only the AI user's own lock.

## 5. The CRM page: Agent Call (click a number, Xplogent calls it)
Build a page in the CRM where an employee controls calls:
- The employee picks a vacancy and a candidate (or opens a candidate) and clicks the number.
- The page POSTs to Xplogent: `POST {xplogent_base_url}/voip/crm/dial` with header `Authorization: Bearer <CRM token>` and body `{phone, applicant_id, sale_id, candidate: {first_name, job_title, postcode_district, phone_e164}, jobs: [{sale_id, job_title, job_category, distance_km, …job details…}], cv_excerpt, history, callback_note}` (or a `brief` in campaign-queue shape). The Xplogent base URL and the CRM token come from config (`.env`: `XPLOGENT_BASE_URL`, `XPLOGENT_CRM_TOKEN`) — the token is the same one set on the Xplogent server (`XPLOGENT_CRM_TOKEN` env), never hardcoded, never shown in the page source.
- The response includes `staging_id`, the chosen job (`analysis`), `warmup`, and `dial` — show the chosen job and the dial result on the page.
- After the call, Xplogent POSTs the summary back (§4.4). The page shows the result under the candidate: status (busy / no answer / voicemail / answered), transcript when answered, and the attempts list (one row per `call_id`, each with its own status, transcript and PBX recording link).
- If the page sends ids + phone but no jobs, Xplogent says "no jobs supplied" and the agent asks the CRM mid-call — for the full flow the page should send the jobs inline (or call `sales/open` / `campaigns/queue` first and pass the result).
- The page must respect the dial lock: if the number is locked (by a recruiter or by the AI), show "being called" and do not dial.

## 6. Tests (PHPUnit feature tests, `RefreshDatabase`; add factories where missing)

**Middleware:**
- a missing token, wrong signature or old timestamp gets 401;
- an empty `XPLOGENT_API_TOKEN` gets 503;
- a correct request passes.

**Queue (the clone):**
- the service returns the same candidates as `getApplicantsBySaleRadius` for the same sale and radius, minus the Xplogent exclusions;
- only matching titles within the radius are returned;
- each exclusion reason is counted;
- the distance is in kilometres and matches the existing formula;
- a closed / on-hold / deleted / `cv_limit`-reached vacancy gets 409.

**Lookup:**
- a known number returns the v2 record with `cv_excerpt` (capped, no DOB/gender/emails/full postcode);
- an unknown number gets 404;
- `do_not_call` follows `is_blocked`.

**Summary:**
- a first call writes one `call_logs` row, one `xplogent_call_events` row, the flags and the note;
- the same `call_id` twice writes nothing the second time (returns `duplicate: true`);
- `test: true` writes nothing;
- each outcome maps to the right flags/note per the table in §4.4;
- the transcript is in the payload, never in notes;
- two dial attempts (two `call_id`s) produce two `call_logs` rows.

**Send-job-details:**
- a blocked applicant gets 409;
- a valid request sends the email and records it;
- the 4th send in a day is refused.

**Dial-lock:**
- locking an unlocked number succeeds;
- locking a locked number gets 409 with `locked_by`;
- unlock removes only the AI user's own lock.

**CRM page:**
- clicking a number posts to Xplogent with the signed body and shows the dial result;
- a locked number cannot be dialled from the page.

## 7. Security
- Check this repo for committed secrets or default credentials (for example default database passwords, demo admin passwords, fallback credentials) and **report what you find in the pull request** — do not silently change unrelated files, and never commit a real secret. (An earlier draft listed security findings for a different repo; they do not apply here — verify against THIS repo.)
- The Xplogent token is a shared secret: never log it, never return it, never put it in a URL.

## 8. Xplogent side (context only — NOT this session's work)
Xplogent already has the client for these endpoints (`src/xplogent/voip/crm.py`), the call brief, the role-facts guard, and the click-to-call entry point (`POST /voip/crm/dial`, defaults to the PBX extension line; FreePBX routes via Telnyx/CommPeak/Primo). What Xplogent still needs, on its server, is config: `voip.crm.enabled: true`, `voip.crm.base_url` = this CRM's address, `XPLOGENT_CRM_API_TOKEN` set, `voip.public_url` set, and `XPLOGENT_CRM_TOKEN` set. That is the owner's job, not this session's. This session builds only the CRM side. The full Xplogent contract reference is `docs/crm-laravel.md` in the Xplogent repo.
