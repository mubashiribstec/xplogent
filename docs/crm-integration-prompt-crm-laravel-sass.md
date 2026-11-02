# Prompt for the CRM session (copy everything below the line)

Revised 2026-11-02. Every repo fact below was checked against `crm_laravel_sass` `dev`
(`aeef0ed`) and `main` (`6658014`). Still confirm them by reading the files before you
change anything.

---

You are working in the Laravel CRM repository **`mubashiribstec/crm_laravel_sass`**. It is
built on Laravel 12, PHP 8.2+, MariaDB/MySQL, Spatie permissions, Sanctum and Yajra DataTables,
and it is a UK recruitment CRM. Models live in the **`Horsefly\`** namespace under `app/Models`
(`Horsefly\Applicant`, `Sale`, `CVNote`, `CallLog`, `DialLock`, `DialCallLog`, `ApplicantNote`,
`ModuleNote`, `JobTitle`, `EmailTemplate`, `SentEmail`, `User`, `Setting`).

**Work on a new branch `xplogent` created from `dev`**, then open a pull request into `dev`.
`dev` is the newest integration branch; `main` is older and has no `CallLog` model. Don't
merge it yourself: the owner reviews it.

**Goal.** Add a small, signed API that lets **Xplogent**, our AI phone agent, run recruitment
screening calls from this CRM, plus one CRM page from which staff ask Xplogent to call a
candidate. The API must let Xplogent:
- get the candidates to call for a vacancy, with the vacancy details;
- look up a caller or candidate;
- write each call's result back **exactly the way a recruiter records it**;
- queue the vacancy email;
- take the same dial lock a recruiter takes.

## 0. Ground rules
- **Read the existing code first, and reuse it.** Where the API needs logic that a screen
  already has, **extract it into a service under `app/Services/Xplogent/` and switch the old
  code to call that service**, so the two can never drift apart. The existing screens must
  behave exactly as today; prove it with regression tests (§7).
- No secrets in the repository. New settings go in `config/xplogent.php`, read from `.env`,
  with **no real default values**.
- Don't touch the existing xplosip / MicroSIP endpoints (`/api/contacts/lookup`,
  `/api/calls/log`, `/api/sip/{token}/…`) or the `dev` portal feed (`GET /api/sales/open`,
  `PORTAL_API_TOKEN`). They are separate integrations with their own auth: don't reuse their
  auth and don't change them.
- Migrations follow the repo's own defensive pattern (`if (!Schema::hasTable(...))` /
  `hasColumn`), because production databases were partly created by hand.

## 1. Facts about this repo you will build on (verified)
| What | Where |
|---|---|
| **Recruiter call outcomes**: reasons `blocked`, `casual`, `no_response`, `no_job`, `circuit_busy`, `not_interested`, `callback`; each sets the flag set below, writes `applicants.applicant_notes`, an `ApplicantNote` (`moved_tab_to`, `note_uid = md5(id)`), switches the old `ModuleNote`s to `status = 0` and writes a new one (`module_note_uid = md5(id)`), and audits through `ActionObserver::customApplicantAudit` | `ApplicantController::storeShortNotes` |
| Note text format: `"<details> --- By: <user name> Date: d-m-Y"` | `storeShortNotes`, `SaleController::storeSaleNotes` |
| **Radius matching**: the sale's `job_title_id` plus its `job_titles.related_titles` (JSON or CSV names → ids); `applicants.status = 1`, not deleted, `is_in_nurse_home = 0`, `lat`/`lng` not null; **haversine** `6371 * acos(...) <= radius` km in a `whereRaw`; latest note = `COALESCE` of the latest `module_notes` (type `Horsefly\Applicant`) and the latest `applicant_notes` | `SaleController::getApplicantsBySaleRadius` (on `dev`; `main` still has the older `ACOS…*60*1.852` form) |
| Radius list filters, including "same `job_category_id` as the sale" when no title filter is given | trait `App\Traits\FiltersRadiusApplicants::applyRadiusApplicantFilters` |
| **Vacancy email**: template slug **`send_job_vacancy_details`** with placeholders `(agent_name) (job_category) (unit_name) (salary) (qualification) (job_type) (timing) (experience) (location)`, then `nl2br` | `CommunicationController::sendEmailsToApplicants`, `saveEmailsForApplicants` |
| Emails are **queued**, not sent inline: a `sent_emails` row with `status = '0'`, sent every minute by `emails:send-bulk` through the `SmtpSetting` of `sent_from` | `App\Traits\SendEmails::saveEmailDB`, `app/Console/Commands/SendBulkEmails.php` |
| Phone helpers: `digits`, `tail` (last 10), `normalize`, `lockKey`, `mask` | `App\Support\PhoneNumber` |
| Phone match on primary, secondary and landline, using the indexed `*_normalized` columns when present | `Applicant::scopePhoneMatches` |
| **Dial lock**: one `dial_locks` row per `phone_key` (`full_number`, `user_id`, `user_name`, `applicant_id`, `call_count`, `locked_at`, `expires_at`), plus settings `dialing_lock_enabled`, `dialing_lock_other_user_minutes`, `dialing_lock_same_user_minutes` and a daily cap through `DialCallLog`. Other agents see "This number is being called by {user_name}" | `DialLockController::acquire`, `Horsefly\DialLock::keyFor`, `DIAL_LOCK.md` |
| `call_logs`: `user_id, caller_number, caller_name, direction, duration_seconds, sip_call_id, applicant_id, sale_id, source, called_at`. **The repo has no migration for it**: the table only exists in production | `Horsefly\CallLog` (`dev` only) |
| Stage history `history` (`applicant_id, sale_id, user_id, stage, sub_stage, status`), `interviews` (`schedule_date`, `schedule_time`, `status`), `cv_notes` (`status 1` = CV sent) | the `database/migrations/2025_*` files |
| **"Not interested in this vacancy"**: an `applicants_pivot_sales` row (applicant, sale) plus a `NotesForRangeApplicant` reason; reverting deletes the row and clears `is_temp_not_interested` when no rows remain. A row means *not* interested, whatever its name suggests | `ResourceController::markApplicantNotInterestedOnSale`, `revertNotInterestedApplicant` |
| Sales, offices and units can be blacklisted (`is_blacklist`) | `Sale`, `Office`, `Unit` |
| Model observers audit every change as `Auth::id() ?? 1`: with nobody signed in, writes are attributed to user 1 (the admin) | `app/Observers/*Observer.php` |
| Applicant flags: `is_blocked`, `is_temp_not_interested`, `is_no_job`, `is_callback_enable`, `is_no_response`, `is_circuit_busy`, `is_in_nurse_home`, `paid_status` | `Horsefly\Applicant` |
| CV files: `applicants.applicant_cv` / `updated_cv`: newer uploads under `public/uploads/resume/…`, older ones under `storage/`. `phpoffice/phpword` and `spatie/pdf-to-text` are already in `composer.json` | `Applicant`, `composer.json` |

## 2. Settings, migrations and the AI user
`config/xplogent.php`:

| Env | Meaning |
|---|---|
| `XPLOGENT_API_TOKEN` | shared secret, ≥ 32 random characters, no default. Empty → every `/api/xplogent/*` call returns **503**. The same value is `XPLOGENT_CRM_API_TOKEN` on the Xplogent server: one token, used for signing in both directions |
| `XPLOGENT_AI_USER_ID` | id of the `users` row **"Xplogent AI"**. Every call log, note, history row, email and dial lock the API writes uses this user |
| `XPLOGENT_BASE_URL` | Xplogent's address (for the page in §5), e.g. `http://192.168.1.30:8765` (plain http is fine on the LAN) |
| `XPLOGENT_SHARE_CLIENT_NAME` | default `false`: no office or unit names in what is sent to Xplogent |
| `XPLOGENT_SHARE_CV_EXCERPT` | default `false`: see §4.3 (the text would go to the AI model's provider) |
| `XPLOGENT_MAX_SKEW` | allowed timestamp skew, default 300 s |
| `XPLOGENT_TIMEZONE` | `Europe/London`, for times written into notes (the app runs in UTC) |

Migrations, all guarded:
1. `call_logs` create-if-missing, matching `CallLog::$fillable`, with indexes on `applicant_id`,
   `sale_id`, `user_id`, `called_at`.
2. `dial_locks.lock_token` (nullable `string(64)`).
3. `xplogent_call_events` (`id`, `call_id` unique, `applicant_id`, `sale_id`, `outcome`,
   `payload` json, timestamps).

Artisan command **`xplogent:make-ai-user`**: creates the user idempotently (random unusable
password, no roles, can't log in) and prints its id.

## 3. Authentication, responses, logging
All endpoints are `POST` with a JSON body under **`/api/xplogent/`** in `routes/api.php`, behind
a new middleware **`VerifyXplogentSignature`** plus `throttle:120,1`. Every request carries:
```
Authorization: Bearer <XPLOGENT_API_TOKEN>
X-Xplogent-Timestamp: <unix seconds>
X-Xplogent-Signature-V2: sha256=<hex HMAC-SHA256 of "<timestamp>.<raw body>", key = the token>
Content-Type: application/json
```
- **401** for a wrong token or signature (compare with `hash_equals`), a non-numeric timestamp,
  or skew beyond `XPLOGENT_MAX_SKEW`;
- **413** over 256 KB;
- **503** when no token is configured.

This is exactly how Xplogent already signs (`docs/crm-laravel.md` in the Xplogent repo).

After a valid signature, make the AI user the request's authenticated user (`Auth::setUser`,
in memory only: no session or login). Otherwise the observers audit every API write as
user 1.

**Responses** are always JSON with `"api_version": 2`. Errors look like
`{"ok": false, "error": "<code>", "message": "<text>"}`. Never return stack traces.

**Log** one line per request with endpoint, applicant_id, sale_id, status and duration only:
no names, numbers, notes or token.

**Phones:** the CRM stores UK national numbers (`07…`). Responses give `phone_e164` (`07…` →
`+447…`, `01/02/03…` → `+44…`; skip numbers that can't be converted) and `phone_kind`
(`mobile` | `secondary` | `landline`). Requests may use either form: match with
`Applicant::phoneMatches`.

## 4. Endpoints (controllers in `App\Http\Controllers\Api\Xplogent\`)
### 4.1 `POST /api/xplogent/sales/open`
Request: `{"search": "care leeds", "limit": 50}` (both optional).

Returns open vacancies (`status = 1`, `is_on_hold = 0`, not deleted), newest first:
`{"api_version": 2, "sales": [{"sale_id", "job_title", "job_category", "postcode",
"postcode_district", "office_name", "unit_name", "position_type", "salary", "cvs_sent",
"cv_limit", "created_at"}]}`.
- `cvs_sent` = the `cv_notes` with `status = 1` for the sale.
- Office and unit names are for staff screens only; Xplogent never speaks them unless sharing
  is on.

### 4.2 `POST /api/xplogent/campaigns/queue`
Request: `{"sale_id": 45, "radius_km": 15, "limit": 50, "exclude_called_within_days": 14}`.

**409 `sale_not_callable`** when the sale is closed, on hold, deleted, blacklisted (the sale,
its office or its unit), has no coordinates, or has reached `cv_limit`. `sales/open` leaves
the same sales out.

**Extract** the base query of `getApplicantsBySaleRadius` into
`App\Services\Xplogent\CandidateQueueService::radiusQuery(Sale $sale, float $radiusKm)` and
make the controller use it. That means:
- title ids including related titles;
- the base filters;
- the haversine `<=` rule;
- the default `applyRadiusApplicantFilters` (same category);
- the latest-note join.

The screen keeps its DataTables layer (sorting, search, status / cv filters,
`paid_status_order`) on top of the shared query. The API adds `distance_km` (the same
haversine expression as a selected column) and then **excludes and counts** in `skipped`:

| reason | rule |
|---|---|
| `blocked` | `is_blocked = 1` |
| `not_interested` | `is_temp_not_interested = 1`, or an `applicants_pivot_sales` row for this sale |
| `no_job` | `is_no_job = 1` |
| `closed` | `paid_status = 'close'` |
| `already_sent` | any `cv_notes` row for this sale |
| `locked` | an unexpired `dial_locks` row for any of the applicant's numbers (by `phone_key`) |
| `recently_called` | a `call_logs` row by the AI user for this sale within `exclude_called_within_days` |
| `no_phone` | no number that can be converted |

Order by distance, then never-called before called, and return up to `limit`:
```json
{"api_version": 2,
 "sale": {"sale_id": 45, "job_title": "Care Assistant", "job_category": "Care",
          "position_type": "Full time", "job_type": "regular", "timing": "…", "salary": "£12.60 per hour",
          "experience": "…", "qualification": "…", "benefits": "…",
          "description": "<job_description, HTML stripped, ≤ 800 chars>", "postcode_district": "LS6",
          "client_name": null, "site_name": null, "cvs_sent": 3, "cv_limit": 8},
 "candidates": [{"applicant_id": 123, "name": "Sarah Jones", "first_name": "Sarah",
   "phone_e164": "+447700900123", "phone_kind": "mobile", "postcode_district": "LS7",
   "distance_km": 4.2, "distance_miles": 2.6, "job_title": "Care Assistant",
   "flags": {"callback": false, "no_response": false, "circuit_busy": false},
   "last_note": "<≤ 300 chars>", "last_note_at": "…", "last_called_at": null}],
 "skipped": {"blocked": 4, "not_interested": 2, "no_job": 1, "closed": 0, "already_sent": 3,
             "locked": 1, "recently_called": 6, "no_phone": 0}}
```
Never include the CV, date of birth, gender, emails or the full postcode.

### 4.3 `POST /api/xplogent/contact-lookup`
Request: `{"phone": "+447700900123", "direction": "inbound", "line": "extension",
"original_called_number": "", "sale_id": 45}`. `sale_id` is optional.

- **No match:** **404** `{"ok": true, "found": false}`.
- **Match:** 200 with:
```json
{"api_version": 2, "found": true, "contact": {
  "applicant_id": 123, "name": "Sarah Jones", "first_name": "Sarah", "company": "",
  "phone_kind": "mobile", "postcode_district": "LS7", "job_title": "Care Assistant",
  "do_not_call": false,
  "flags": {"blocked": false, "temp_not_interested": false, "callback": true,
            "no_response": false, "no_job": false, "circuit_busy": false},
  "notes": "<latest note ≤ 500>", "last_outcome": "callback", "callback_note": "<≤ 300 or empty>",
  "cv_excerpt": "",
  "history": [{"date": "2026-10-12", "sale_id": 45, "job": "Care Assistant — LS6", "stage": "crm", "sub_stage": "…"}],
  "applications": [{"sale_id": 45, "job": "Care Assistant — LS6", "interested": true, "latest_stage": "…", "latest_sub_stage": "…"}],
  "interviews": [{"sale_id": 45, "date": "2026-10-30", "time": "10:00", "status": 1}],
  "calls": [{"at": "…", "direction": "outbound", "duration_s": 182, "by": "Xplogent AI", "sale_id": 45}]}}
```
- `do_not_call` is `is_blocked`.
- `last_outcome` is the latest `ApplicantNote.moved_tab_to`.
- Limits: `history` latest 5; `applications` latest 5; `interviews` upcoming plus the last 2;
  `calls` latest 5 `call_logs`.
- Job labels use the postcode district, never the client name (unless sharing is on).
- **Must answer in under 300 ms** on production data: use `phoneMatches` (indexed columns) and
  `LIMIT`ed queries.
- **`cv_excerpt`** stays `""` unless `XPLOGENT_SHARE_CV_EXCERPT=true`. When it's on:
  - extract text server-side from `updated_cv` or else `applicant_cv`: DOCX/DOC with phpword,
    PDF with spatie/pdf-to-text (needs `pdftotext`: add `poppler-utils` to the `Dockerfile`);
  - **redact** emails, phone numbers, UK postcodes and anything that looks like a date of
    birth;
  - keep it to 2,000 characters;
  - cache it per file (path + mtime);
  - a broken file gives `""`, never an error.

### 4.4 `POST /api/xplogent/call-summary`
Xplogent sends this once per finished call (and per unanswered dial attempt), and retries from
a durable outbox. **Make it idempotent on `call_id`.** Unknown fields are ignored. The body is
the `call.completed` payload: `call_id`, `test`, `direction`, `from`, `to`, `applicant_id`,
`sale_id`, `campaign_id`, `started_at`, `duration_s`, `outcome`, `final_attempt`, `callback_at`,
`summary`, `answers`, `email_confirmed`, `do_not_call`, `transcript`, `provider_call_id`,
`recording_available`. Older Xplogent builds omit `applicant_id` / `sale_id`: then match the
applicant by phone (`to` on outbound, `from` on inbound).

1. `test: true` → 200 `{"ok": true, "test": true}`, and nothing is written.
2. A known `call_id` → 200 `{"ok": true, "duplicate": true}`, and nothing is written.
3. In **one transaction**:
   - a `call_logs` row: AI user, `applicant_id`, `sale_id`, `caller_number` in national form,
     `direction`, `duration_seconds`, `sip_call_id = provider_call_id`, `called_at = started_at`,
     `source = 'xplogent'`;
   - the `xplogent_call_events` row (the full payload, so the transcript lives here);
   - the outcome write below.
4. **Outcome write:** extract the reason switch, the notes, the module notes and the audit of
   `storeShortNotes` into **`App\Services\Xplogent\ApplicantStatusService::apply(Applicant
   $applicant, string $reason, string $details, User $by)`**, make `storeShortNotes` call it
   unchanged in behaviour, and use it here. Likewise extract the two "not interested in this
   vacancy" actions of `ResourceController` into **`App\Services\Xplogent\SaleInterestService`**
   (`markNotInterested`, `revertNotInterested`) and make the controller call them:

| `outcome` | write |
|---|---|
| `interested` | reason **`casual`** with the summary note; with a `sale_id`, also revert a "not interested in this vacancy" (`SaleInterestService::revertNotInterested`). **Never** create `cv_notes` or move a stage. |
| `callback` | reason **`callback`**; the note includes `callback_at` in Europe/London ("Callback Wed 29 Oct 17:30") |
| `recruiter_callback` | as `callback`, with the note starting `RECRUITER CALLBACK:` (a person must call back) |
| `not_interested` with a `sale_id` | not interested in **that vacancy** only: `SaleInterestService::markNotInterested` (the pivot row and `NotesForRangeApplicant`, as Resources → mark not interested), plus the note. The global flag stays as it was |
| `not_interested` without a `sale_id` | reason **`not_interested`** (the global flag) |
| `no_job` | reason **`no_job`** |
| `do_not_call` | reason **`blocked`** |
| `no_answer`, `voicemail`, `busy` | call log only; if `final_attempt: true`, reason **`no_response`** with a note listing the attempt |
| `number_unobtainable` | reason **`circuit_busy`** |
| `wrong_person`, `transferred`, `screen_incomplete`, `interrupted`, `other` | an `ApplicantNote` with an empty `moved_tab_to` (plus the module note); no flag change |

5. Note text: `AI call (<outcome>): <summary>. Answers: Right to work: …; Commute: …; … --- By:
   Xplogent AI Date: d-m-Y`, at most 2,000 characters. **The transcript never goes into notes.**
6. `email_confirmed` that differs from both stored emails is added to the note ("Candidate gave
   email …"). Never overwrite the stored emails.
7. Each dial attempt has its own `call_id`, so each attempt gets its own `call_logs` row.
8. Return `{"ok": true, "call_log_id": …}`.

### 4.5 `POST /api/xplogent/send-job-details`
Request: `{"applicant_id": 123, "sale_id": 45, "email": "…", "call_id": 1234}`.
- **409 `blocked`** for an applicant with `is_blocked`;
- validate the email (`email:rfc,dns`);
- at most 3 per applicant per day (**429** after that).

Extract the template filling of `sendEmailsToApplicants` into
**`App\Services\Xplogent\VacancyEmailService::fill(Sale $sale, string $agentName): array`**
(subject, body, from, title) and make both CRM methods use it. The API then **queues** a
`sent_emails` row exactly like `saveEmailDB`: `status '0'`, `sent_from` = the template's
`from_email`, `user_id` = AI user, `applicant_id`, `sale_id`. `emails:send-bulk` delivers it.
`(agent_name)` is "Xplogent AI". Return `{"ok": true, "sent_email_id": …}`.

### 4.6 `POST /api/xplogent/dial-lock` and `/dial-unlock`
- **`dial-lock`** request: `{"phone", "applicant_id", "ttl_seconds": 900, "lock_token": "<random per call>"}`.
  - Key by `DialLock::keyFor` (= `PhoneNumber::lockKey`), in a transaction with
    `lockForUpdate`, as `acquire` does.
  - An unexpired lock held by **another user** → **409**
    `{"locked": false, "locked_by": "<user_name>", "expires_at"}`.
  - An unexpired lock held by the AI user with a **different** `lock_token` (another Xplogent
    worker) → **409**.
  - Otherwise write the row as the AI user: `user_name = "Xplogent AI"`, `lock_token`,
    `expires_at = now + ttl_seconds` (60–3600), `call_count + 1`, and a `DialCallLog`
    increment. Return 200 `{"locked": true, "expires_at"}`.
  - Recruiters' click-to-dial then shows "being called by Xplogent AI" without any change.
  - When `dialing_lock_enabled` is off, return `{"locked": true}` without writing.
- **`dial-unlock`** request: `{"phone", "lock_token"}`. Expire the lock only if it belongs to
  the AI user **and** the token matches; otherwise it's a no-op, so a worker can never release
  a recruiter's lock or another worker's.

## 5. CRM page "Agent call" (staff ask Xplogent to call)
New permission **`xplogent-agent-call`**, a menu item, and a page, using the CRM's own layout:
1. Pick an open vacancy (§4.1 data), then a candidate from its queue (§4.2 data), or open the
   page from an applicant.
2. Before calling, check the CRM dial lock as `acquire` would. If the number is locked, show
   "being called by …" and don't dial.
3. **Call with AI** posts server-side (never from the browser; the token never reaches the
   page) to **Xplogent `POST {XPLOGENT_BASE_URL}/voip/crm/dial`**, signed with the same headers
   as §3, body:
   `{"phone": "+447…", "applicant_id": 123, "sale_id": 45, "requested_by": "<staff name>",
   "brief": {"sale": {…§4.2 sale…}, "candidate": {…§4.2 candidate…, "cv_excerpt": ""}}}`.
   One vacancy per call: the employee picks it.
4. Xplogent answers `{"ok": true, "call": {…}}`, or `{"ok": false, "error": "…"}` for DNC,
   outside calling hours, a cost cap or a CRM lock (`locked_by`). Show it.
5. Results come back through §4.4. The page lists the attempts under the candidate, one row per
   `call_id`, from `call_logs` + `xplogent_call_events`:
   - the time and the outcome (answered / no answer / busy / voicemail / …);
   - the summary;
   - the transcript (only with the permission).

   Add a PBX recording link only if the CRM can already find recordings by `sip_call_id`;
   otherwise say so in the PR.

## 6. Security fixes (same PR, separate commits)
1. **Blocked applicants emailed (bug).** `CommunicationController::distance()` chains
   `->where("job_category_id", …)->orWhere("job_title_id", …)` without grouping, which cancels
   the `status` / `is_blocked` / `is_no_job` / `is_callback_enable` filters. Group it, and add
   a test proving a blocked applicant with a matching title is excluded.
2. **`users.sip_password` is plain text:** add the `encrypted` cast plus a migration that
   encrypts existing values; never return it from an API.
3. **`docker-compose.yml`:** no fixed MySQL root password (`${DB_ROOT_PASSWORD:?…}`), and don't
   publish 3306 by default.
4. **FreePBX DB settings in `config/database.php`:** no real-looking defaults (the username
   default is `root` today).
5. **A `gitleaks` CI job** that fails on committed secrets.
6. **In the PR description, tell the owner to:**
   - rotate the FreePBX-DB, MySQL and admin passwords (the older `mubashiribstec/crm` repo
     still has FreePBX credentials in `config/database.php`, and this repo's history had
     them);
   - make both repositories private.

   Don't rewrite git history.

## 7. Tests (PHPUnit feature tests, `RefreshDatabase`, on MariaDB)
`phpunit.xml` targets MySQL, so add a GitHub Actions workflow with a MariaDB service; locally,
use the repo's `docker-compose`. Add factories where they're missing.
- **Middleware:**
  - a missing token, wrong signature or stale timestamp gets 401;
  - no token configured gets 503;
  - an oversized body gets 413;
  - a valid request passes.
- **Queue:**
  - the same base set as the radius screen for the same sale and radius (regression);
  - related titles are included;
  - candidates outside the radius or category are excluded;
  - each skip reason is counted;
  - 409 cases;
  - no CV, DOB, gender, email or full postcode.
- **Lookup:**
  - matches by primary, secondary and landline number, in national and E.164 form;
  - 404 when unknown;
  - limits;
  - `do_not_call`;
  - `cv_excerpt` empty when off, and redacted and capped when on.
- **Summary:**
  - every row of the table writes exactly its flags and notes;
  - a duplicate `call_id` and `test` write nothing;
  - `interested` never creates `cv_notes`;
  - no transcript in notes;
  - two attempts give two `call_logs` rows.
- **Send-job-details:**
  - blocked gets 409;
  - placeholders are filled;
  - a queued `sent_emails` row is written;
  - the 4th send in a day gets 429.
- **Dial lock:**
  - the AI lock blocks a recruiter's `acquire` and the other way round;
  - the same token renews and a different token gets 409;
  - unlock needs the AI user's own token;
  - expiry works.
- **Regression:**
  - `storeShortNotes` writes the same rows as before for every reason;
  - `sendEmailsToApplicants` / `saveEmailsForApplicants` fill the same body;
  - the radius screen returns the same candidates.
- **Page:**
  - posts the signed request (`Http::fake`);
  - a locked number is not dialled;
  - only staff with the permission can see the transcript.

## 8. Deliverables
- **Code:** config, migrations, the command, the middleware, the services, the controllers,
  routes, the page, tests.
- **`docs/xplogent-api.md`:** setup (env, `php artisan xplogent:make-ai-user`, the token on
  both sides) and a `curl` example per endpoint, including how to compute the signature.
- **The PR into `dev`:** a summary, the outcome table, what you reused (by file/method), the
  security actions for the owner, and `php artisan test` results.

## 9. Xplogent side (context: built in the Xplogent repo, not here)
- Xplogent already calls every endpoint above (`src/xplogent/voip/crm.py`) and sends
  `lock_token`.
- It accepts `cv_excerpt` in the lookup and the brief.
- It serves `POST /voip/crm/dial` for §5: signed with the same token, it dials through all its
  normal checks (do-not-call, calling hours, cost caps, the CRM dial lock).
- **On the Xplogent server:** Settings → CRM: the CRM address and the same token.
