# Xplogent Voice: phone calling agent

Answers inbound phone calls with an AI that only speaks from your scripts and knowledge base,
logs every call, and hands off to a person when it can't help.

## How it works

```
caller -> Telnyx number -> wss://<your server>/ws/telephony/<one-time token>
  VAD -> speech-to-text -> CallBrain (fast LLM + your knowledge + the customer record)
      -> text-to-speech -> audio back to the caller      (interrupt it any time: barge-in)
```

## Setup (inbound)

1. **Server**: needs a public HTTPS address (a reverse proxy such as Caddy/nginx in front of
   `xplogent up`, forwarding WebSocket upgrades). `pip install 'xplogent[voip]'`.
2. **Speech on your GPU** (cheapest): run an OpenAI-compatible speech-to-text server (for
   example faster-whisper-server) and text-to-speech server (for example Kokoro-FastAPI),
   then set `voip.stt.base_url` / `voip.tts.base_url` in the dashboard config. Or leave
   `stt.provider: whisper-local` to run faster-whisper inside Xplogent. Or point both at
   OpenAI for a no-GPU start.
3. **Telnyx**: create a Call Control application and buy a number. Set the application's
   webhook URL to `https://<server>/telephony/webhook/<webhook secret>` (Dashboard -> Calls
   shows it after you generate a secret). Put `TELNYX_API_KEY` in Settings -> secrets.
4. **Dashboard -> Calls**: set the public URL, company name and hand-off number, turn the
   phone agent **on**.
5. **Knowledge**: add your scripts and FAQs (Knowledge view). **Contacts**: import your
   customer CSV so the agent knows who is calling.
6. Call your number.

## Cost control

Per-minute cost is roughly carrier (about half a cent) + the LLM you choose. Pick a small,
fast model in `voip.model`. Local speech on a GPU removes the speech-to-text and
text-to-speech charges. `voip.cost_per_minute` is your own estimate, shown per call.

## Safety defaults

- The agent states it is an AI and that the call may be recorded (edit `voip.greeting`).
- It answers only from the knowledge base, script and customer record; otherwise it offers a
  callback or a human.
- "Stop calling me" -> the number is added to the do-not-call list.
- Calls are capped by `voip.max_call_seconds` and end after prolonged silence.

Check recording, AI-disclosure, consent and calling-hours rules for your country before going
live, especially before outbound calling. This is engineering documentation, not legal advice.

## Status

Inbound MVP. Telnyx-specific request fields (`stream_bidirectional_*`) should be confirmed
with a real test call. Coming next: training from your call recordings, CRM write-back,
outbound campaigns.
