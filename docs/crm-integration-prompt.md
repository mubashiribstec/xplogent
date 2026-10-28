# Prompt for the CRM session (copy everything below the line)

---

You are working in the Laravel CRM repository **`mubashiribstec/CRM`** (Laravel 12, PHP 8.2,
MariaDB/MySQL, PHPUnit 11, Spatie permissions, Sanctum). It is a UK recruitment CRM:
- `applicants` are candidates;
- `sales` are vacancies, with `offices` (clients) and `units` (sites);
- `applicants_pivot_sales` links applicants to vacancies (`is_interested`);
- `history` holds stage and sub-stage per applicant and vacancy;
- `applicant_notes` and `module_notes` hold timestamped notes with `moved_tab_to`;
- also `interviews`, `call_logs`, `dial_locks`, `cv_notes`, `email_templates` and
  `sms_templates`.

**Goal.** Add a small, signed API that lets **Xplogent**, our AI phone agent, run recruitment
screening calls from this CRM:
- get the candidates to call for a vacancy, with the vacancy details and candidate history;
- look up a caller;
- write each call's result back exactly the way a recruiter records it;
- send the vacancy email;
- take the same dial lock a recruiter takes.

Also fix the security issues and the bug listed at the end.

**Ground rules.**
- Read the existing code before writing. Reuse the CRM's own models, helpers and rules:
  - `App\Support\PhoneNumber` (`tail`, `normalize`, `lockKey`);
  - `Applicant::scopePhoneMatches`;
  - the radius rule in `SaleController::getApplicantsBySaleRadius`;
  - the reasons in `ApplicantController::storeShortNotes`;
  - the template filling in `CommunicationController::sendEmailsToApplicants`;
  - the `DialLock` model.
- Don't change existing screens or behaviour. Where logic must be shared (the
  `storeShortNotes` reasons, the vacancy-email filling, the radius query), **extract it into
  a service class and call that service from both the old controller and the new API**, so
  the two can never drift apart.
- No secrets in the repository. New settings go in `.env` / `config/xplogent.php` with **no
  real default values**.
- Every endpoint must be covered by PHPUnit feature tests (below). Work on a branch and open a
  pull request.

## 1. Settings and the AI user

`config/xplogent.php`, reading:

| Env | Meaning |
|---|---|
| `XPLOGENT_API_TOKEN` | shared secret (≥ 32 random chars); no default. If empty, every `/api/xplogent/*` call returns 503 |
| `XPLOGENT_AI_USER_ID` | id of a `users` row named **"Xplogent AI"** (no login: random unusable password, no roles). Every note, call log, history row and dial lock the API writes uses this user, so the audit trail shows the AI |
| `XPLOGENT_SHARE_CLIENT_NAME` | `false` by default. If false, the vacancy brief does **not** include the office or unit names |
| `XPLOGENT_MAX_SKEW` | allowed timestamp skew in seconds, default 300 |
| `XPLOGENT_TIMEZONE` | `Europe/London`; times in notes and the `callback_at` display use it (the app itself is UTC) |

Add an artisan command `xplogent:make-ai-user` that creates that user idempotently and prints
its id.

## 2. Authentication (all endpoints)

All endpoints are `POST`, with a JSON body, under `/api/xplogent/`, in `routes/api.php`, behind
a new middleware `VerifyXplogentSignature` plus `throttle:120,1`.

Every request carries:
```
Authorization: Bearer <XPLOGENT_API_TOKEN>
X-Xplogent-Timestamp: <unix seconds>
X-Xplogent-Signature-V2: sha256=<hex HMAC-SHA256 of "<timestamp>.<raw body>", key = XPLOGENT_API_TOKEN>
Content-Type: application/json
```

The middleware rejects with **401** when:
- the token or signature doesn't match: compare with `hash_equals`;
- the timestamp isn't digits;
- the timestamp is more than `XPLOGENT_MAX_SKEW` seconds from now.

It rejects with **413** when the body is larger than 256 KB.

This is exactly the scheme in
https://github.com/mubashiribstec/xplogent/blob/main/docs/crm-laravel.md (Xplogent already
signs this way).

**Responses:**
- Always JSON, with `"api_version": 2`.
- Errors: `{"ok": false, "error": "<code>", "message": "<human text>"}`, with 4xx/5xx.
- Never return stack traces.
- **Log** each request without personal data: endpoint, applicant_id, sale_id, status and
  duration only.

## 3. Phone numbers
- The CRM stores UK national numbers (`07…`, 11 digits); Xplogent uses E.164 (`+447…`).
- **Responses** give `phone_e164`: `07xxxxxxxxx` → `+447xxxxxxxxx`, `01…`/`02…`/`03…` → `+44…`.
  Skip numbers that can't be converted.
- **Requests** may carry either form; match with `Applicant::phoneMatches` (primary, secondary,
  landline; last-10-digit match).
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
These names are for staff in the Xplogent dashboard: they're never spoken unless
`XPLOGENT_SHARE_CLIENT_NAME` is true. `cvs_sent` counts `cv_notes` rows with `status = 1` for
the vacancy, the same count the CRM already shows.

### 4.2 `POST /api/xplogent/campaigns/queue`: who to call for a vacancy
Request:
```json
{"sale_id": 45, "radius_km": 15, "limit": 50, "exclude_called_within_days": 14}
```

Refuse with **409** `{"error": "sale_not_callable", "message": …}` if the vacancy is closed, on
hold, deleted, or has reached `cv_limit`.

Candidates come from **the same rule as `SaleController::getApplicantsBySaleRadius`**: extract
it into a service and reuse it.
- `applicants.status = 1`, `is_in_nurse_home = 0`, not deleted;
- `job_title_id` is the vacancy's title or one of its `related_titles`;
- straight-line distance < `radius_km` (same formula as today).

Then **exclude** (count each reason in `skipped`):
- `is_blocked = 1` → `blocked`;
- `is_temp_not_interested = 1` → `not_interested`;
- `is_no_job = 1` → `no_job`;
- `paid_status = 'close'` → `closed`;
- a `cv_notes` row for this vacancy (any status) → `already_sent`;
- `applicants_pivot_sales.is_interested = 1` for this vacancy → `already_interested`;
- an unexpired `dial_locks` row for any of the applicant's numbers → `locked`;
- a `call_logs` row by the AI user for this vacancy within `exclude_called_within_days`
  → `recently_called`;
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
- **Never** return the CV, date of birth, gender, emails or full postcode in this response:
  the AI doesn't need them.

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
  "history": [{"date": "2026-10-12", "sale_id": 45, "job": "Care Assistant — LS6",
               "stage": "crm", "sub_stage": "crm_interview_attended"}],
  "applications": [{"sale_id": 45, "job": "Care Assistant — LS6", "interested": true,
                    "latest_stage": "quality", "latest_sub_stage": "quality_cvs"}],
  "interviews": [{"sale_id": 45, "date": "2026-10-30", "time": "10:00", "status": 1}],
  "calls": [{"at": "2026-10-12T15:00:00Z", "direction": "outbound", "duration_s": 182,
             "by": "Xplogent AI", "sale_id": 45}]}}
```
- `do_not_call` = `is_blocked`.
- `last_outcome` is the latest note's `moved_tab_to` (e.g. `callback`, `not interested`,
  `no job`, `no response`), or empty.
- `history`: the latest 5 rows. `applications`: the latest 5. `interviews`: upcoming and the
  last 2. `calls`: the latest 5 `call_logs` rows.
- Job labels use the postcode district, never the client name (unless sharing is allowed).
- Same data-minimisation rule as 4.2.
- **Must answer in under 300 ms** on production data: use the indexed `*_normalized` phone
  columns and `LIMIT`ed sub-queries.

### 4.4 `POST /api/xplogent/call-summary`: write the call result back
Xplogent sends this once per finished call and retries on failure. **It must be idempotent on
`call_id`.** Body (unknown fields must be ignored):
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
Older Xplogent builds send this without `applicant_id`/`sale_id`: then match the applicant by
phone (`to` on outbound, `from` on inbound), as `docs/crm-laravel.md` describes.

**Behaviour:**
1. **`test: true`:** return 200 `{"ok": true, "test": true}` and write nothing.
2. **Idempotency:** a new table `xplogent_call_events` (`id`, `call_id` unique,
   `applicant_id`, `sale_id`, `outcome`, `payload` json, `created_at`). If `call_id` exists,
   return 200 `{"ok": true, "duplicate": true}` and write nothing else.
3. **Always**, in one DB transaction:
   - a `call_logs` row: `user_id` = AI user, `applicant_id`, `sale_id`,
     `caller_number` = the candidate's number in national form, `direction`, `duration_seconds`,
     `sip_call_id` = `provider_call_id`, `called_at` = `started_at`;
   - the `xplogent_call_events` row.
4. **Per outcome**, using the **same flag logic as `storeShortNotes`** (extract it into
   `ApplicantStatusService::apply($applicant, $reason, $details, $user)`, which writes the flags,
   `applicant_notes`, the `ApplicantNote` with `moved_tab_to`, the `ModuleNote` and the audit
   exactly like today):

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
| `wrong_person`, `transferred`, `screen_incomplete`, `interrupted`, `other` | note only (an `ApplicantNote` with an empty `moved_tab_to`), no flag change |

5. **Note text** follows the existing format. Example:
   `AI call (callback): <summary>. Answers: Right to work: …; Commute: …; Shifts: …; Start: … --- By: Xplogent AI Date: 28-10-2026`
   - Cap it at 2,000 characters.
   - **Never store the transcript in notes.** It stays in `xplogent_call_events.payload`.
6. **`email_confirmed`:** if it differs from both stored emails, add it to the note ("Candidate
   gave email …"). **Don't overwrite** `applicant_email`.
7. Return `{"ok": true, "call_log_id": …}`.

### 4.5 `POST /api/xplogent/send-job-details`: email the vacancy to one candidate
Request:
```json
{"applicant_id": 123, "sale_id": 45, "email": "sarah.jones@example.com", "call_id": 1234}
```
- Refuse with **409** `blocked` if the applicant `is_blocked`.
- Validate the email (RFC + DNS like the existing rules).
- Fill the **`send_job_vacancy_details`** template exactly as `sendEmailsToApplicants` does
  (extract that filling into a service), using the "Xplogent AI" user name as `(agent_name)`.
- Send it through the existing mail and SMTP settings, and record it in `sent_emails` as
  other sends are.
- Return `{"ok": true, "sent_email_id": …}`.
- Rate-limit: at most 3 per applicant per day.

### 4.6 `POST /api/xplogent/dial-lock` and `POST /api/xplogent/dial-unlock`
- **`dial-lock`** request: `{"phone": "+447700900123", "applicant_id": 123, "ttl_seconds": 900}`.
  - Use `PhoneNumber::lockKey` and the `DialLock` model, owned by the AI user.
  - Success: 200 `{"locked": true, "expires_at": …}`.
  - Already locked by someone else and unexpired: 409
    `{"locked": false, "locked_by": "<user name>", "expires_at": …}`.
  - Recruiters' existing click-to-dial must show the AI's lock like any other ("being called
    by Xplogent AI").
- **`dial-unlock`** request: `{"phone": "+447700900123"}`. It removes only the AI user's own
  lock.

## 5. Tests (PHPUnit feature tests, `RefreshDatabase`; add factories where missing)

**Middleware:**
- a missing token, wrong signature or old timestamp gets 401;
- an empty `XPLOGENT_API_TOKEN` gets 503;
- a correct request passes.

**Queue:**
- only matching titles within the radius are returned;
- each exclusion reason is counted;
- closed / on-hold / full vacancies get 409;
- no CV, DOB, gender or email appears in the response.

**Lookup:** a match by primary, secondary and landline number, in national and E.164 form; 404
when unknown; the history and calls limits; `do_not_call` follows `is_blocked`.

**Summary:**
- every outcome row of the table writes exactly the listed flags and notes;
- a duplicate `call_id` writes nothing;
- `test: true` writes nothing;
- `interested` never creates `cv_notes`;
- the transcript never reaches the notes.

**Send-job-details:** blocked gets 409; the template placeholders are filled; a `sent_emails`
row is written; the rate limit holds.

**Dial lock:** the AI lock blocks a recruiter lock and the other way round; expiry works;
unlock only removes the AI's own lock.

**Regression:** the existing `storeShortNotes` and `sendEmailsToApplicants` behaviour is
unchanged after the extraction.

## 6. Security fixes in this repository (do these in the same PR, separate commits)
1. **Default passwords.** Remove real-looking default passwords from tracked files:
   - `docker-compose.yml`, `.env.docker`, `docker/entrypoint.sh`: use required variables
     (`${DB_PASSWORD:?set DB_PASSWORD}`) or obvious placeholders (`change-me`);
   - the demo admin password in `DemoSeeder.php` / `.env.docker`: generate it at seed time and
     print it once.
2. **FreePBX fallbacks.** Remove the fallback FreePBX credentials in `config/database.php`:
   `env('DB_FREEPBX_PASSWORD')` with no default; the same for host and user.
3. **SIP passwords.** Check `users.sip_password`. If it's stored in plain text, add the
   `encrypted` cast with a migration that re-encrypts existing values, and never return it in
   any API.
4. **Secret scanning.** Add a `gitleaks` (or `trufflehog`) CI job that fails on committed
   secrets.
5. **Blocked applicants emailed (bug).** `CommunicationController::distance()` chains
   `->where('job_category_id', …)->orWhere('job_title_id', …)` without grouping. That OR
   cancels the `is_blocked` / `is_no_job` / `is_callback_enable` / `status` filters, so
   blocked applicants can be emailed. Group it:
   `->where(fn ($q) => $q->where('job_category_id', $cat)->orWhere('job_title_id', $title))`.
   Add a test proving a blocked applicant with a matching title is excluded.
6. **In the PR description, tell the owner to:**
   - rotate the real MariaDB, FreePBX-database and admin passwords (anything that may ever
     have been committed is in the git history);
   - make the repository private.

   Don't rewrite git history yourself.

## 7. Deliverables
- **Code:**
  - `config/xplogent.php`;
  - the middleware, the controller(s) under `App\Http\Controllers\Api\Xplogent\`;
  - the services `ApplicantStatusService`, `VacancyMatchService` and `VacancyEmailService`;
  - the migration for `xplogent_call_events`;
  - the artisan command;
  - the routes and tests.
- **`docs/xplogent-api.md`:** setup (the env vars, `php artisan xplogent:make-ai-user`) and
  each endpoint with an example `curl`, including how to compute the signature.
- **A PR** with a summary, the outcome-mapping table, the security notes from §6, and the test
  results. Before the PR, all tests must pass (`php artisan test`).
- **Don't merge:** the owner reviews.
