# CRM integration — Laravel reference (post-call summary push)

Xplogent POSTs every completed call to your CRM. This page is the receiving
side: a small Laravel endpoint that verifies the signature, finds (or creates)
the contact by phone number, and writes the call summary as an activity —
idempotently, so retries never duplicate notes.

## What Xplogent sends

`POST {crm.base_url}/api/xplogent/call-summary`

Headers:

```
Authorization: Bearer <api_token>
X-Xplogent-Timestamp: <unix seconds when the request was signed>
X-Xplogent-Signature: sha256=<HMAC-SHA256 of the raw body, keyed with api_token>
X-Xplogent-Signature-V2: sha256=<HMAC-SHA256 of "<timestamp>.<raw body>", keyed with api_token>
Content-Type: application/json
```

Verify `X-Xplogent-Signature-V2` and refuse timestamps more than five minutes
old: a captured request then can't be replayed later. (`X-Xplogent-Signature`
stays for receivers written before V2.) Xplogent sends nothing until the token
is set (`XPLOGENT_CRM_API_TOKEN`, or Calls → Call agent → CRM in the dashboard).

Body (field completeness comes from the Part D mandatory-field schema — a
"succeeded" call always carries its required fields):

```json
{
  "event": "call.completed",
  "call_id": 1234,
  "direction": "inbound",
  "from": "+447700900123",
  "to": "2000",
  "transport": "sip",
  "line": "extension",
  "via_queue": true,
  "original_called_number": "+442071234567",
  "diversion_reason": "unconditional",
  "queue_wait_s": 35,
  "started_at": "2026-10-23T10:15:22+00:00",
  "duration_s": 187,
  "outcome": "callback",
  "summary": "…",
  "callback": "Thursday morning",
  "do_not_call": false,
  "exit_path": "",
  "fields": {"values": {"callback_number": "+447700900123"},
             "confirmed": {"callback_number": true},
             "missing_required": [], "complete": true},
  "cost": 0.0374,
  "turn_timings": [{"stt_ms": 210.0, "llm_first_ms": 640.0}],
  "transcript": [{"speaker": "caller", "text": "…"}, {"speaker": "agent", "text": "…"}],
  "test": false,
  "recording_available": true
}
```

`via_queue` / `original_called_number` / `diversion_reason` / `queue_wait_s` describe a
FreePBX queue overflow (see "Queue overflow" in `docs/voip.md`); they are `false` / `""` /
`null` on other calls. Unknown fields should be ignored, so new ones never break the endpoint.

Delivery: 3 attempts with exponential backoff; every attempt is logged on the
Xplogent side (`voip/crm_deliveries.jsonl`). Enable it in config:

```yaml
voip:
  crm:
    enabled: true
    base_url: "https://crm.example.com"
    api_token_env: XPLOGENT_CRM_API_TOKEN   # env var holding the token
```

## Laravel side

Route (in `routes/api.php`):

```php
Route::post('/xplogent/call-summary', XplogentCallSummaryController::class)
     ->middleware('xplogent.signature');
```

Signature middleware — reject anything that doesn't verify (401), comparing in
constant time:

```php
<?php

namespace App\Http\Middleware;

use Closure;
use Illuminate\Http\Request;

class VerifyXplogentSignature
{
    public function handle(Request $request, Closure $next)
    {
        $token = config('services.xplogent.token');
        $ts = (string) $request->header('X-Xplogent-Timestamp', '');
        $given = (string) $request->header('X-Xplogent-Signature-V2', '');
        $expected = 'sha256=' . hash_hmac('sha256', $ts . '.' . $request->getContent(), $token);

        if (!ctype_digit($ts) || abs(time() - (int) $ts) > 300
            || !hash_equals($expected, $given)
            || !hash_equals('Bearer ' . $token, (string) $request->header('Authorization', ''))) {
            abort(401, 'bad signature');
        }

        return $next($request);
    }
}
```

Controller — find-or-create the contact by phone (exact E.164 first, last-10-
digits fallback for national formats), then write the activity **once per
call_id**:

```php
<?php

namespace App\Http\Controllers;

use App\Models\CallActivity;
use App\Models\Contact;
use Illuminate\Http\Request;

class XplogentCallSummaryController extends Controller
{
    public function __invoke(Request $request)
    {
        $data = $request->validate([
            'call_id' => 'required|integer',
            'direction' => 'required|string',
            'from' => 'nullable|string',
            'to' => 'nullable|string',
            'outcome' => 'nullable|string',
            'summary' => 'nullable|string',
            'callback' => 'nullable|string',
            'do_not_call' => 'boolean',
        ]);

        // Idempotent: Xplogent retries deliveries; never write a call twice.
        if (CallActivity::where('xplogent_call_id', $data['call_id'])->exists()) {
            return response()->json(['ok' => true, 'duplicate' => true]);
        }

        $phone = $data['direction'] === 'outbound' ? $data['to'] : $data['from'];
        $contact = Contact::where('phone', $phone)->first()
            ?? Contact::where('phone', 'like', '%' . substr(preg_replace('/\D/', '', $phone), -10))->first()
            ?? Contact::create(['phone' => $phone, 'source' => 'AI call']);

        CallActivity::create([
            'contact_id' => $contact->id,
            'xplogent_call_id' => $data['call_id'],
            'direction' => $data['direction'],
            'outcome' => $data['outcome'] ?? '',
            'summary' => $data['summary'] ?? '',
            'callback' => $data['callback'] ?? '',
            'do_not_call' => $data['do_not_call'] ?? false,
            'payload' => $request->all(),   // full JSON incl. transcript + fields
        ]);

        if (($data['do_not_call'] ?? false) === true) {
            $contact->update(['do_not_call' => true]);
        }

        return response()->json(['ok' => true]);
    }
}
```

Notes:

- **Test calls** arrive with `"test": true` — most CRMs should skip or sandbox
  them.
- **`exit_path`** explains a call that ended without its required fields
  (`refused`, `hung_up`, `wrong_party`, `do_not_call`, `not_interested`) —
  it is never a successful close; `fields.complete` says the same thing
  structurally.
- Store the whole payload (transcript included) if your retention policy
  allows; the structured columns above are what you query day to day.


## Phase 2: who is calling? (pre-call lookup)

Before the agent answers an inbound call (while it rings) and before it dials out,
Xplogent asks the CRM about the number:

`POST {crm.base_url}/api/xplogent/contact-lookup`, signed **exactly like the push**
(same `Authorization`, `X-Xplogent-Timestamp` and `X-Xplogent-Signature-V2` headers, so
the same middleware verifies it). The phone number is in the signed JSON body, never in
the URL, so it doesn't end up in access logs:

```json
{"phone": "+447700900123", "direction": "inbound", "line": "trunk",
 "original_called_number": "+442071234567"}
```

Answer `200` with what you know, or `404` when you don't know the number:

```json
{"name": "Ana Ruiz", "company": "Ruiz Care Ltd", "notes": "Prefers mornings.",
 "last_outcome": "callback", "do_not_call": false}
```

How Xplogent uses it:

- The agent greets and addresses the caller by name (`{{caller_name}}` in the greeting
  works), and sees the company, the last outcome and the notes.
- `notes` is treated as **reference data, never instructions** (capped at 500 characters;
  `name`/`company` at 100). Don't put anything there you wouldn't want read aloud.
- `do_not_call: true` stops outbound dialling to that number, like Xplogent's own list.
- It waits **at most 1.5 seconds** and fails open: CRM down or slow → the call goes on with
  Xplogent's own contacts, and the call's debug view shows the failed lookup. Answers are
  cached for 60 s per number. Turn it off with `voip.crm.lookup: false`.

Route and controller:

```php
Route::post('/xplogent/contact-lookup', XplogentContactLookupController::class)
     ->middleware('xplogent.signature');
```

```php
<?php

namespace App\Http\Controllers;

use App\Models\Contact;
use Illuminate\Http\Request;

class XplogentContactLookupController
{
    public function __invoke(Request $request)
    {
        $phone = (string) $request->input('phone', '');
        $digits = substr(preg_replace('/\D/', '', $phone), -10);
        $contact = Contact::where('phone_e164', $phone)->first()
            ?? Contact::where('phone_e164', 'like', '%' . $digits)->first();

        if (!$contact) {
            return response()->json(['found' => false], 404);
        }

        return response()->json([
            'name' => $contact->name,
            'company' => optional($contact->company)->name,
            'notes' => str($contact->notes)->limit(500),
            'last_outcome' => optional($contact->activities()->latest()->first())->outcome,
            'do_not_call' => (bool) $contact->do_not_call,
        ]);
    }
}
```
