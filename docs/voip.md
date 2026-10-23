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
   keep NAT on (rtp_symmetric, force_rport, rewrite_contact) and allow ulaw/alaw. Point an
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

Limits: UDP only (no TCP/TLS/SRTP), G.711 μ-law/A-law, two SIP accounts (trunk +
extension). DTMF uses RFC 4733 telephone-events and works both ways when the far end
offers it: callers can use the keypad (`voip.dtmf` configures what keys do), and the
agent can send digits (e.g. through a menu) on lines that negotiated it.

## Speech

`xplogent-docker speech on` runs Kokoro (text-to-speech) on CPU; listening uses the built-in
Whisper. Choose **Local on this server** in the Speech card and press **Test voice**.

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
  is enforced; outside `voip.calling_hours` (09:00–20:00 in the contact's time zone by
  default) you are asked first.
- **Test call in the browser**: talk to the agent exactly as a caller would, typing (or with
  the microphone on HTTPS), hearing its real voice and seeing the tools it uses.
- **Call me**: rings your phone. Test calls are marked "test", not counted, and never change
  a contact or the do-not-call list.

## Backup model

If the call model's server is down, a small local model can answer instead
(`xplogent-docker backup-model on`, Settings → Backup model), so calls aren't dropped.

## Cost control

Per-minute cost is roughly carrier + the LLM you choose. Pick a small, fast model in
`voip.model`. Local speech removes the speech charges. `voip.cost_per_minute` is your own
estimate, shown per call.

## Safety defaults

- The agent states it is an AI and that the call is transcribed (edit `voip.greeting`). Xplogent stores transcripts, not audio recordings — the greeting must not claim recording.
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
- **CRM (`voip.crm`).** On call end, a signed (HMAC-SHA256) `call.completed`
  webhook is POSTed with outcome, summary, field completeness and the
  transcript — 3 attempts, backoff, delivery log at
  `voip/crm_deliveries.jsonl`. See `docs/crm-laravel.md`.
- **Knowledge gaps.** Questions the knowledge base couldn't answer are logged
  to `voip/knowledge_gaps.jsonl`; `top_knowledge_gaps()` ranks them — that list
  is what to teach the agent next.
