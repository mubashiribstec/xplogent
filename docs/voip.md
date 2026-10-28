# Xplogent Voice: phone calling agent

Answers and places phone calls with an AI that speaks from your scripts, training material and
knowledge base, logs every call, and hands off to a person when it can't help. You can watch
and steer live calls from the dashboard.

## How it works

```
phone line ──► Telnyx Voice API (webhook + wss media stream)    ─┐
           ├─► SIP trunk: Telnyx SIP, FreePBX, … (RTP)          ─┤
           └─► SIP extension: a FreePBX extension login (RTP)   ─┤
                                                                 ▼
   VAD -> speech-to-text -> CallBrain (fast LLM + playbook + knowledge + customer record + tools)
       -> text-to-speech -> audio back to the caller            (interrupt it any time: barge-in)
```

All phone lines feed the same call engine, so greeting, playbook, knowledge, tools, live
control and call records behave the same whichever line a call uses. Every call record
says which line it used (`trunk` or `extension`).

## Choose the phone line (Calls → Setup → Phone line)

| | Telnyx Voice API | SIP trunk (built-in client) |
|---|---|---|
| Needs | public **HTTPS** address (domain) | UDP ports reachable by the PBX/provider |
| Works with | Telnyx numbers | FreePBX/Asterisk extension, Telnyx SIP connection, most SIP providers |
| Outbound caller ID | your Telnyx number | the PBX/trunk's rules (or "Caller ID" if allowed) |
| Transfer | Telnyx transfer | SIP REFER (to a number or a PBX extension) |

You can also use **both**.

### Telnyx Voice API

1. HTTPS first: point a domain at the server and run `xplogent-docker https agent.example.com`.
2. Calls → Setup: company name, public HTTPS address, hand-off number, **Generate webhook secret**.
3. Telnyx portal: create a Voice API (Call Control) application with the webhook URL shown,
   assign your number, create an API key. Paste key, application ID and number in the
   Telnyx card and press **Test Telnyx**.

### SIP trunk (built-in client)

Xplogent registers like a desk phone and answers/places calls itself: no domain needed.

1. Docker: `sudo xplogent-docker sip on` (publishes UDP 5060 and 10000-10100 and records the
   server's address; use `--ip <public IP>` when the provider reaches you over the internet
   through a router). Open the same UDP ports in the firewall.
2. Calls → Setup → Phone line → **SIP trunk**, then fill in the SIP trunk card: server,
   username/extension, password (stored encrypted as `SIP_PASSWORD`), optional caller ID.
   **Save & connect** registers and shows the result; the checklist shows it too.
3. **FreePBX**: Applications → Extensions → add a **PJSIP** extension (e.g. 2000) for the AI,
   keep NAT on (rtp_symmetric, force_rport, rewrite_contact) and allow ulaw/alaw.

#### Testing with only a FreePBX extension (no Telnyx)

For a first test you don't need Telnyx, a public address or a webhook:

1. Calls → Setup → **Phone line: PBX extension only (internal calls, testing)**. The
   checklist then asks only for the extension. Telnyx, the public address and the webhook
   disappear.
2. Fill in the **PBX extension** card (PBX address and port, the extension, its secret), tick
   *Extension line switched on*, **Save & connect**, then **Test registration**.
3. **Agent calls an extension**: Call now → type the extension (e.g. `201`) → **Call me** (a
   test call: not counted, no CRM). Short extension numbers are accepted on this line.
4. **You call the agent** from a desk phone: switch the phone agent on (**Switch on** in Go
   live), or keep it off and list your extension under **Test extensions**. While it's off,
   every other caller gets "unavailable" (SIP 480), and calls from the test extensions are
   answered and marked test. Point an
   inbound route or ring group at it. The AI's outbound calls use the PBX's outbound routes,
   and the hand-off number can be an extension (e.g. `101`).
4. **Telnyx SIP connection**: Voice → SIP Trunking → connection with credentials auth; server
   `sip.telnyx.com` with its username/password. Assign your number to the connection and set
   an outbound voice profile for dialing out.

Who can ring the AI: the SIP server (any of its addresses), the "Also accept calls from"
list, and calls addressed to this extension (Docker and multi-server carriers need that).
Tick "only accept calls from these addresses" for the strict rule.

### PBX extension (second SIP login)

The trunk above is one SIP account. If it is spoken for — the usual setup is a Telnyx
SIP connection for external calls — the agent can hold a **second, independent login**:
an extension on your FreePBX. Switch it on in the **PBX extension (FreePBX)** card
(Calls → Setup), with the PBX address, the extension number and its secret (stored
encrypted as `SIP_EXTENSION_PASSWORD`). It registers exactly like the trunk but on its
own ports — SIP UDP **5062**, RTP **10200–10300** — so both lines run at once; in
Docker, publish those ports too (`XPLOGENT_SIP_EXT_PORT` / `XPLOGENT_RTP_EXT_START` /
`XPLOGENT_RTP_EXT_END` override them).

With it, anyone in the office can **dial the agent's extension** from another extension
to test it, the agent can **call extensions back** (choose "PBX extension" as the line
in *Call now*), and a FreePBX **queue can overflow to it**: point the queue's fail-over
destination at the agent's extension. Calls on this line are tagged `extension`, and
the agent knows it is on an internal/PBX call. **FreePBX side**: Applications →
Extensions → add a **PJSIP** extension for the AI; keep NAT on (rtp_symmetric,
force_rport, rewrite_contact) and allow ulaw/alaw.

#### Queue overflow: who called, what they dialled, how long they waited

When a FreePBX queue overflows to the agent, the agent can know it. It reads, **only from the
PBX itself** (headers from anyone else are ignored, so they can't be faked):

- the caller's number from `P-Asserted-Identity` (then `Remote-Party-ID`, then `From`);
- the number they originally dialled from the oldest `Diversion` entry (or `History-Info`);
- "came through a queue" when the diverting number is in **Queue numbers that overflow here**
  (PBX extension card, `voip.sip_extension.queue_numbers`) or FreePBX sends
  `X-Xplogent-Via: queue`, plus the wait from `X-Xplogent-Queue-Wait` (seconds).

The call is then marked **via queue**, the agent thanks the caller once for waiting, and the call
record and CRM payload carry `via_queue`, `original_called_number`, `diversion_reason` and
`queue_wait_s`. `voip.queue_greeting` can replace the greeting for these callers (the AI and
recording disclosure is still added).

FreePBX side, on the AI's PJSIP extension (Advanced tab): **Send RPID/PAI** → *Send
P-Asserted-Identity header* and **Trust RPID/PAI** → *Yes* (needed for the caller's number), and
keep **Send Diversion** on (the default). Then add this to `/etc/asterisk/extensions_custom.conf`
(2000 = the AI's extension; change the `VoiceMail` line to your voicemail box or ring group),
reload, create a **Custom Destination** with target `xplogent-overflow,s,1`, and pick it as the
queue's **Fail Over Destination**:

```
[xplogent-overflow]
exten => s,1,Set(__XPL_WAIT=${CDR(duration)})
 same => n,Set(REDIRECTING(from-num)=${FROM_DID})   ; the number the caller dialled
 same => n,Set(REDIRECTING(from-pres)=allowed)
 same => n,Set(REDIRECTING(reason)=no_answer)
 same => n,Set(REDIRECTING(count)=1)
 same => n,Dial(PJSIP/2000,60,b(xplogent-headers^s^1))
 same => n,VoiceMail(100@default,u)        ; AI busy or down: never strand the caller
 same => n,Hangup()

[xplogent-headers]
exten => s,1,Set(PJSIP_HEADER(add,X-Xplogent-Via)=queue)
 same => n,Set(PJSIP_HEADER(add,X-Xplogent-Queue-Wait)=${XPL_WAIT})
 same => n,Return()
```

This exact dialplan was tested on Asterisk 23 (caller ID, dialled number, queue flag and wait
time arrive; with the AI unregistered the caller lands on the fallback line). The wait is the
call's age when it reaches the AI, so it includes any IVR time before the queue.

#### When the AI line is full or down

Each SIP line takes `max_calls` calls at once (4 by default); the next caller gets "busy". The
Calls page warns when a line is nearly full and counts callers who got busy, but the caller
must still go somewhere. In FreePBX, give the AI's extension a fallback **behind** it:

- Extension → Advanced → **Optional Destinations**: set *Busy* and *Not Reachable* to
  voicemail or a ring group (Not Reachable also covers "the AI server is down / unregistered").
- If you use the custom destination above, its line after `Dial` is that fallback.
- Keep the queue's own timeout/fail-over chain ending at a person or voicemail.

Limits: UDP only (no TCP/TLS/SRTP), G.711 μ-law/A-law, two SIP accounts (trunk +
extension). DTMF uses RFC 4733 telephone-events and works both ways when the far end
offers it: callers can use the keypad (`voip.dtmf` configures what keys do), and the
agent can send digits (e.g. through a menu) on lines that negotiated it.

## Speech

Calls go: caller audio → speech-to-text → the call model → text-to-speech, sentence by
sentence, with barge-in (the greeting is never cut off). Pick both halves in **Calls → Speech**;
**Test voice** speaks a sentence and hears it back, showing both times in ms.

| Listening (speech-to-text) | Notes |
|---|---|
| Built-in Whisper (default, free) | Runs inside Xplogent. `voip.stt.model: auto` = `large-v3-turbo` on an NVIDIA GPU (≈1.6 GB download on first use, cached), `small.en` on CPU. Transcribes after the caller stops. |
| OpenAI Whisper | `whisper-1` over the API (OpenAI key). |
| **Deepgram, streaming** | Transcribes *while* the caller speaks, so the text is ready almost as soon as they stop (`DEEPGRAM_API_KEY`, model e.g. `nova-3`). If the stream fails mid-sentence, the recorded utterance is transcribed by Deepgram's batch API, then the built-in Whisper: no words are lost. |

| Voice (text-to-speech) | Notes |
|---|---|
| Local Kokoro (free) | `xplogent-docker speech on` (`--gpu` on NVIDIA). |
| OpenAI | `tts-1` voices (OpenAI key). |
| ElevenLabs | `ELEVENLABS_API_KEY`, model from config (default `eleven_flash_v2_5`), voice picked from your ElevenLabs voices. |
| Cartesia | `CARTESIA_API_KEY`, model from config (default `sonic-2`), voice picked from your Cartesia voices. |

Keys are stored encrypted (Speech card or Settings). If the main voice fails before speaking,
it is retried once, then `voip.tts_fallback` (any of the above) takes over for that sentence; the
call's debug view shows which provider served each turn and why a fallback happened.

**Realtime speech-to-speech (optional).** Set **Conversation mode → Realtime** (`voip.mode:
realtime`, OpenAI key; `voip.realtime` holds the model, voice and transcription model). The OpenAI
model then hears the caller directly (G.711 μ-law, no resampling) and answers with its own voice,
which removes the speech-to-text and text-to-speech steps from every turn. What stays the same:
the greeting with the AI and recording notice is still spoken word for word by the normal voice
and can't be interrupted; tools run through the same rules (a required field still blocks
`end_call`; do-not-call, callbacks and transfers work as usual); knowledge is looked up with a
`search_knowledge` tool; the model only speaks when Xplogent asks it to, so **Pause AI** works;
recording, transcripts, live control, the debug view and the CRM work unchanged. If OpenAI can't
be reached when the call starts, the call runs in the standard mode (the debug timeline says so).
Trade-offs: lower delay and more natural turn-taking, but cloud-only, a higher per-minute price
than the standard mode with local speech, and less exact control over wording; OpenAI receives
the call audio.

**Cost:** set `voip.cost_per_minute` to carrier + model + the speech providers' per-minute rates
from their price pages (local Whisper and Kokoro add nothing). **Privacy:** cloud speech-to-text
receives the caller's audio; cloud voices receive the agent's words. The local choices keep both
on your server; say which you use in your privacy notice.

### A small server without a GPU (e.g. 4 CPU cores)

Speech runs fine on a small CPU; the model is what gets slow. Keep speech local and put the
model elsewhere:

- **Listening**: built-in Whisper with `auto` (picks `small.en` on CPU).
- **Voice**: local Kokoro (`xplogent-docker speech on`, the CPU image).
- **Model for calls** (Calls → Conversation): a hosted model, e.g. one of the free OpenRouter
  models (*Gemma 4 31B (free)*, *Nemotron 3 Ultra (free)*; both support the agent's tools, and
  you need an OpenRouter key), or a model on your stronger Ollama server (Settings → Ollama servers
  → Add server; with its bearer token, it shows up in the list as `model@server`).
- **Backup model**: keep a small model as backup (Settings → Backup model, on this server or on
  a named Ollama server). Free OpenRouter models are rate-limited and sometimes refuse with
  "temporarily rate-limited upstream" (HTTP 429); the backup then answers that turn, so the
  caller isn't left waiting.

The Go-live checklist shows a "Call model speed" warning when the call model is an Ollama model
on this server and no GPU is found.

## Call recording

Every call is recorded as a stereo WAV (caller on the left, agent on the right) on all lines:
Telnyx, the SIP trunk, the PBX extension and browser test calls. Open a call in **Calls** to
play, download or delete its recording, or press **Use for training** to add it to Call
training. Files are kept in `data/voip/recordings/` for `voip.recording.retention_days`
(90 by default, 0 keeps them).

While recording is on, callers are told so: the greeting is adjusted automatically (see
Safety defaults). To keep transcripts only, set **Call recording → off** in the Conversation
card (`voip.recording.enabled: false`).

## Training (Call training page)

- **Call recordings** (wav, mp3, m4a, ogg…, up to 200 MB each) are transcribed on the server.
  Stereo recordings are split per channel into Agent/Customer turns (pick which channel is
  the agent; re-transcribe with the other channel if it's wrong).
- **Scripts, FAQs, price lists** (txt, md, pdf).
- **Q&A sheet** (csv with `question` and `answer` columns): each row is one answer.

Everything goes into the knowledge base the agent searches during a call (deleting an item
removes it). **Build from calls** asks the call model to write a playbook draft (call flow,
FAQ, objections, tone) from the transcripts and Q&A; edit it and **Save**: every call reads it.

## Calling and testing

- **Live calls**: transcript as it happens; **Pause AI** (a person takes over by typing what
  the agent says), **Say** (the agent says your sentence now), **Instruct** (a private note the
  agent follows from its next reply), **Transfer**, **Hang up**.
- **Call now**: the AI calls a number, or a customer from Contacts (📞). The do-not-call list
  is enforced; outside the calling hours you are asked first.
- **Calling hours** (Setup → Calling hours, `voip.calling_hours`):
  - a window for each weekday. The default is Mon–Fri 09:00–20:00, Sat 10:00–16:00 and
    Sunday closed, in Europe/London (the contact's own time zone wins when it's known);
  - UK bank holidays are closed (England and Wales, Scotland or Northern Ireland).
    The dates come from gov.uk, refreshed weekly, with a built-in list as the fallback, so a
    dial never waits on the internet;
  - the card shows whether calls are allowed now and the next allowed time. Test calls ignore
    the hours.
- **Outbound calls open safely** (Setup → Conversation; `voip.outbound`, `voip.voicemail`):
  - **Listen first:** after the callee answers, the agent waits for their "Hello?" (at most
    2.5 s) instead of talking over them.
  - **Identity check:** when the callee's first name is known (a campaign brief or a contact),
    the opening line is only "Hi, this is {company}'s AI assistant, on a recorded line. Am I
    speaking with Sarah?". The job and the application are mentioned only after a clear yes.
    If someone else answers, the agent apologises, gives no details and ends the call
    (outcome `wrong_person`).
  - **Answering machines:** in the first seconds the agent checks for speech that runs on
    without a pause (> 4 s), voicemail phrases ("leave a message", "you've reached…") and the
    beep (an 800–1,200 Hz tone). On a machine it hangs up silently: outcome `voicemail`, which
    the CRM counts as an attempt. On Telnyx calls the carrier's detection is used as well
    (`voicemail.telnyx_amd: premium`).
- **Alerts** (Setup → Alerts): paste a Slack or Teams incoming-webhook address (it's stored
  as the secret `XPLOGENT_ALERT_WEBHOOK_URL`). You're told when:
  - 5 calls in a row fail (a busy line or no answer doesn't count);
  - the CRM hasn't answered for 5 minutes;
  - calls were cut off because the server stopped.

  At most one alert of each kind every 15 minutes (`voip.alerts.min_gap_minutes`). After a
  restart, calls that were cut off are marked `interrupted`, and their transcript so far goes to
  the CRM.
- **Test call in the browser**: talk to the agent exactly as a caller would, typing or with
  the microphone, hearing its real voice and seeing the tools it uses. **Start test call** asks
  for the microphone straight away. Browsers only allow the microphone on HTTPS or `localhost`:
  over `http://<server-ip>` it is off (typing still works). Either set up HTTPS
  (`sudo xplogent-docker https your.domain`), or open the dashboard through an SSH tunnel
  (`ssh -L 8765:localhost:8765 you@server`, then `http://localhost:8765`). If the voice fails
  (for example OpenAI voice with no OpenAI key), the panel says so once and the call carries on
  as text; "the agent is thinking" shows while a reply is on its way.
- **Run checks** (in the test-call card): tests every link of a call one by one, with the time
  and, when something fails, the reason and the fix:
  - the call model, sent the way a call sends it (with a tool); for OpenRouter it also shows
    your free requests used today;
  - the backup model;
  - the voice;
  - hearing: the voice's audio transcribed back, with the share of words recognised;
  - your microphone (HTTPS and permission);
  - the live connection (WebSockets through any reverse proxy).
- **Test this model** (Calls → Model for calls) and **Test key** (Settings → API keys) run the
  same model test. Typical answers:
  - "busy at Google AI Studio (shared free pool), not a problem with your key";
  - "the API key was refused";
  - "all 50 free-model requests for today are used". OpenRouter allows 20 requests a minute
    and 50 a day on free models; buying $10 of credits raises that to 1,000 a day.
- **Call me**: rings your phone. Test calls are marked "test", not counted, and never change
  a contact or the do-not-call list.

## Debugging a call

Open any call in **Calls** (live or finished) and press **Debug this call**:

- **Turns**: per turn, speech-to-text ms, model ms (to the first sentence), voice ms (to the
  first audio) and the total from "caller stopped talking" to "caller hears the agent", which
  provider served each stage, any fallback (e.g. `tts:fallback`, `stt:stream->batch`,
  `llm:backup`) and the exact error when a stage failed.
- **Timeline**: answered, greeting, each line said, interruptions, keys, transfer attempts and
  results, why the call ended.
- **SIP messages** (SIP calls): the INVITE/100/180/200/ACK/BYE ladder with key headers on hover.
  Login headers (`Authorization`, `Proxy-Authorization`) are never stored.
- **Audio (RTP)**: codec, far end, packets in/out, loss, late packets, jitter, silence filled
  in, keypad events. **Telnyx events** for Telnyx calls.

**Download debug bundle** saves one JSON file for a bug report: the call, transcript, all of
the above and a config snapshot with secrets removed (no audio). Each download is in the audit
log. From a terminal: `xplogent voip debug <call-id>` (or `--json`). Diagnostics are kept as
long as recordings (`voip.recording.retention_days`).

## Backup model

If the call model's server is down or rate-limited, a small model answers instead
(`xplogent-docker backup-model on`, Settings → Backup model), so calls aren't dropped. The
backup runs on this server's Docker Ollama or on any named Ollama server (**Runs on**).

## Cost control

Per-minute cost is roughly carrier + the LLM you choose. Pick a small, fast model in
`voip.model`. Local speech removes the speech charges. `voip.cost_per_minute` is your own
estimate, shown per call.

## Safety defaults

- The agent states it is an AI and that the call is recorded (or, with recording off,
  transcribed). The system keeps this true whatever the greeting says: while recording is on,
  "transcribed" becomes "recorded" and a greeting that mentions neither gets "This call is
  recorded." in front.
- It answers only from the knowledge base, playbook and customer record; otherwise it offers a
  callback or a human.
- "Stop calling me" → the number is added to the do-not-call list; dialing such a number is refused.
- Calls are capped by `voip.max_call_seconds` and end after prolonged silence.

Check recording, AI-disclosure, consent and calling-hours rules for your country before going
live, especially before outbound calling. This is engineering documentation, not legal advice.

## The agent layer (disclosure, silence, fields, templates, CRM)

- **AI disclosure (`voip.disclose_ai`, default on).** The disclosure sentence is
  injected by the system in front of the greeting whenever the greeting text
  doesn't already contain it — editing the greeting can never silently remove
  it. The greeting itself is spoken once by the engine and is not part of the
  model's prompt; the model is instructed not to greet again.
- **Silence (`voip.silence`).** A quiet caller gets at most `max_prompts` short
  prompts ("Are you still there?" …), then a polite goodbye and hangup.
- **Mandatory fields (`voip.fields`).** Declare fields the call must capture —
  e.g. a callback number with `validator: phone, require_confirmed: true`.
  Values are validated and normalised (phone → E.164), the model gets a
  per-turn checklist, and **`end_call` is refused** while a required field is
  missing or a number hasn't been read back and confirmed. The only closes
  without them are explicit exit paths (`refused`, `hung_up`, `wrong_party`)
  or a do-not-call / not-interested outcome, recorded on the call.
- **Dynamic variables.** Greeting, persona and playbook may use
  `{{caller_name}}`, `{{role}}`, `{company}`, contact fields, etc.
- **Templates (`voip.template`).** `kingsbury_inbound` and
  `kingsbury_outbound_screener` ship a greeting, playbook and field schema for
  UK recruitment, plus a scoring rubric (right-to-work gate; must-haves 40 /
  commute 20 / shifts 20 / experience 20; a pay mismatch flags, never scores).
  A template only applies to calls in its own direction (the inbound one to
  inbound calls, the screener to outbound calls); other calls use your own
  greeting and playbook. Pick one in Calls → Call agent.
- **CRM (`voip.crm`).** On call end, a signed (HMAC-SHA256) `call.completed`
  webhook is POSTed with outcome, summary, field completeness and the
  transcript — 3 attempts, backoff, delivery log at
  `voip/crm_deliveries.jsonl`. Nothing is sent until the API token is set
  (Calls → Call agent, stored encrypted as `XPLOGENT_CRM_API_TOKEN`); **Send
  test** posts a sample. Each call row shows its delivery (`CRM sent` / `retrying` /
  `failed` / `skipped`); a failed or skipped one has a **Resend** button that sends the
  exact same summary again (the receiver dedupes on `call_id`). Before answering (while it rings) and before dialling, the
  agent also asks the CRM who the number is (signed `contact-lookup`, 1.5 s max, fails
  open to local contacts; the CRM's `do_not_call` blocks dialling). See
  `docs/crm-laravel.md`.
- **Knowledge gaps.** Questions the knowledge base couldn't answer are logged
  to `voip/knowledge_gaps.jsonl` (size-capped) and ranked in Calls → Call
  agent — that list is what to teach the agent next.
