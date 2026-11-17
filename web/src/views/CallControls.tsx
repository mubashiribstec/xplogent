import { useEffect, useRef, useState } from "react";
import { CheckCircle2, Circle, Loader2, MinusCircle, Mic, MicOff, Pause, Phone, PhoneForwarded, PhoneOff, Play, Send, Stethoscope, X, XCircle } from "lucide-react";
import {
  CallResult, CallTurn, LiveCallInfo, dialCall, getCall, getFullConfig, hangupCall, instructCall, pauseAI, sayOnCall,
  transferCall, wsTokenParam, wsUrl, testModel, testSpeech, testVoiceAgent, getBackupModel, testBackupModel,
  getTestNumbers, saveTestNumber,
} from "../api";
import { Badge, Button, Card, Field } from "../ui";
import { useConfirm, usePrompt } from "../ui/confirm";
import { Transcript } from "./calls/Transcript";

type Toaster = (m: string, kind?: any) => void;
type Ask = (o: { title: string; body?: string; confirmLabel?: string; danger?: boolean }) => Promise<boolean>;
const since = (t: number) => { const s = Math.max(0, Math.round(Date.now() / 1000 - t)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };

/** Dial, asking before calling outside calling hours. */
export async function placeCall(body: Parameters<typeof dialCall>[0], toast: Toaster, ask: Ask): Promise<boolean> {
  let res: CallResult = await dialCall(body);
  if (!res.ok && res.hours && await ask({ title: "Outside calling hours", body: `${res.error}.`, confirmLabel: "Call anyway" }))
    res = await dialCall({ ...body, ignore_hours: true });
  if (res.ok) toast(body.test ? "calling your phone…" : "calling…", "success");
  else if (!res.hours || res.error) toast(res.error ?? "call failed", "error");
  return !!res.ok;
}

// ── live calls ───────────────────────────────────────────────────────────────
export function LiveCalls({ active, toast, onChange }: { active: LiveCallInfo[]; toast: Toaster; onChange: () => void }) {
  if (!active.length) return null;
  return (
    <Card title={<>Live calls <Badge tone="warn">{active.length}</Badge></>}>
      {active.map((c) => <LiveCall key={c.call_id} call={c} toast={toast} onChange={onChange} />)}
    </Card>
  );
}

function LiveCall({ call, toast, onChange }: { call: LiveCallInfo; toast: Toaster; onChange: () => void }) {
  const [turns, setTurns] = useState<CallTurn[]>([]);
  const [text, setText] = useState("");
  const [, tick] = useState(0);
  const box = useRef<HTMLDivElement>(null);
  const confirm = useConfirm();
  const prompt = usePrompt();
  useEffect(() => {
    const load = () => { getCall(call.call_id).then((r) => setTurns(r.call?.turns ?? [])).catch(() => {}); tick((n) => n + 1); };
    load();
    const id = setInterval(load, 1500);
    return () => clearInterval(id);
  }, [call.call_id]);
  useEffect(() => { const t = box.current?.querySelector(".transcript"); t?.scrollTo({ top: t.scrollHeight }); }, [turns.length]);

  const act = async (p: Promise<CallResult>, ok: string) => {
    const r = await p;
    if (r.ok) toast(ok, "success"); else toast(r.error ?? "failed", "error");
    onChange();
    return r.ok;
  };
  const say = async () => { if (text.trim() && await act(sayOnCall(call.call_id, text), "the agent is saying it")) setText(""); };
  const instruct = async () => { if (text.trim() && await act(instructCall(call.call_id, text), "instruction added")) setText(""); };
  const transfer = async () => {
    const to = await prompt({ title: "Transfer this call", label: "Number (blank = the hand-off number)", confirmLabel: "Transfer" });
    if (to !== null) act(transferCall(call.call_id, to.trim()), "transferring");
  };
  const hangup = async () => {
    if (await confirm({ title: "Hang up this call?", confirmLabel: "Hang up", danger: true })) act(hangupCall(call.call_id), "hung up");
  };

  return (
    <div className="live-call">
      <div className="row wrap" style={{ gap: 8, justifyContent: "space-between" }}>
        <div>
          <b>{call.contact_name || call.number || "unknown"}</b>{" "}
          <span className="dim">{call.direction} · {call.transport}{call.test && call.direction !== "test" ? " · test" : ""}{call.via_queue ? " · via queue" : ""} · {since(call.started)}</span>
          {call.paused && <> <Badge tone="warn">AI paused</Badge></>}
        </div>
        <div className="row wrap" style={{ gap: 6 }}>
          <Button size="sm" icon={call.paused ? Play : Pause} onClick={() => act(pauseAI(call.call_id, !call.paused), call.paused ? "AI resumed" : "AI paused")}
            title={call.paused ? "Let the AI answer again" : "Stop the AI from answering (type what to say instead)"}>
            {call.paused ? "Resume AI" : "Pause AI"}</Button>
          <Button size="sm" icon={PhoneForwarded} disabled={!call.can_transfer} onClick={transfer}>Transfer</Button>
          <Button size="sm" variant="danger" icon={PhoneOff} onClick={hangup}>Hang up</Button>
        </div>
      </div>
      <div ref={box}><Transcript turns={turns} live empty="Waiting for the first words…" /></div>
      {call.notes.length > 0 && <div className="dim xsmall">Instructions: {call.notes.join(" · ")}</div>}
      <div className="row wrap" style={{ gap: 6, marginTop: 6 }}>
        <input className="grow" style={{ flex: "1 1 220px" }} value={text} aria-label="Sentence or instruction for the agent"
          placeholder="Type a sentence for the agent to say, or an instruction"
          onChange={(e) => setText(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") say(); }} />
        <Button variant="primary" disabled={!text.trim()} onClick={say} title="The agent says this now">Say</Button>
        <Button disabled={!text.trim()} onClick={instruct} title="A private note the agent follows from its next reply on">Instruct</Button>
      </div>
    </div>
  );
}

// ── manual call + "call my phone" ────────────────────────────────────────────
export function CallNow({ toast, via, onCalled }: { toast: Toaster; via: string[]; onCalled: () => void }) {
  const [number, setNumber] = useState("");
  const [line, setLine] = useState(via[0] ?? "telnyx");
  const [me, setMe] = useState(() => { try { return localStorage.getItem("xpl_test_phone") || ""; } catch { return ""; } });
  const [busy, setBusy] = useState(false);
  const [tests, setTests] = useState<{ numbers: string[]; per_day?: number }>({ numbers: [] });
  const confirm = useConfirm();
  useEffect(() => { if (!via.includes(line) && via[0]) setLine(via[0]); /* eslint-disable-next-line */ }, [via.join(",")]);
  useEffect(() => { getTestNumbers().then(setTests).catch(() => {}); }, []);
  const editTests = async (b: { add?: string; remove?: string }) => {
    const r = await saveTestNumber(b);
    if (r.ok) setTests((t) => ({ ...t, numbers: r.numbers })); else toast(r.error ?? "not saved", "error");
    return !!r.ok;
  };
  // Mirrors the server rule (service.is_test_number), so "Call me" can offer to add the number first.
  const listed = (n: string) => {
    const key = n.replace(/[\s()-]/g, "");
    return (line !== "telnyx" && /^[0-9*#]{2,6}$/.test(key))
      || tests.numbers.some((t) => t.replace(/^\+/, "") === key.replace(/^\+/, ""));
  };
  const call = async (n: string, test: boolean) => {
    setBusy(true);
    try {
      if (test && !listed(n) && !(await confirm({ title: "Add a test number?", confirmLabel: "Add and call",
        body: `${n} isn't one of your test numbers yet. Only you should answer it.` }) && await editTests({ add: n }))) return;
      if (await placeCall({ number: n, via: line, test }, toast, confirm)) onCalled();
    } finally { setBusy(false); }
  };
  const lineSelect = via.length > 1 && (
    <Field label="Line">{(id) => <select id={id} value={line} onChange={(e) => setLine(e.target.value)}>
      {via.map((v) => <option key={v} value={v}>{v === "sip" ? "SIP trunk" : v === "extension" ? "PBX extension" : "Telnyx API"}</option>)}</select>}</Field>
  );
  return (
    <Card title={<><Phone size={16} aria-hidden /> Call now</>}
      description="The AI agent places the call. Numbers on the do-not-call list are refused; outside calling hours you're asked first.">
      <div className="form-grid" style={{ alignItems: "end" }}>
        <Field label="Number to call">{(id) => <input id={id} value={number} inputMode="tel"
          placeholder={line === "extension" ? "extension (e.g. 201) or number" : "+15551234567"}
          onChange={(e) => setNumber(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter" && number.trim()) call(number, false); }} />}</Field>
        {lineSelect}
        <div><Button variant="primary" icon={Phone} disabled={busy || !number.trim()} onClick={() => call(number, false)}>Call</Button></div>
      </div>
      <h4 className="mt-4" style={{ marginBottom: 8 }}>{line === "extension" ? "Test call to my extension" : "Test on my phone"}</h4>
      <div className="form-grid" style={{ alignItems: "end" }}>
        <Field label={line === "extension" ? "Your extension" : "Your mobile"}
          hint={`Not counted in stats and never changes a contact. Only your own test numbers${tests.per_day ? `, at most ${tests.per_day} a day` : ""}; counts toward the daily cost cap.`}>
          {(id) => <input id={id} value={me} inputMode="tel" placeholder={line === "extension" ? "e.g. 201" : "+447700900000"}
            onChange={(e) => { setMe(e.target.value); try { localStorage.setItem("xpl_test_phone", e.target.value); } catch { /* not saved */ } }} />}
        </Field>
        <div><Button disabled={busy || !me.trim()} onClick={() => call(me, true)}>Call me</Button></div>
      </div>
      <div className="row wrap mt-3" style={{ gap: 6 }}>
        <span className="dim small">Test numbers:</span>
        {tests.numbers.length ? tests.numbers.map((t) => (
          <span key={t} className="pill">{t}
            <button type="button" className="icon-btn" style={{ width: 22, height: 22 }} aria-label={`Remove ${t}`} title={`Remove ${t}`}
              onClick={() => editTests({ remove: t })}><X size={12} /></button></span>))
          : <span className="dim small">none yet</span>}
      </div>
    </Card>
  );
}

// ── test call in the browser ─────────────────────────────────────────────────
const MU_DECODE = (() => {
  const t = new Float32Array(256);
  for (let i = 0; i < 256; i++) {
    const u = ~i & 0xff, sign = u & 0x80, exp = (u >> 4) & 7, man = u & 0x0f;
    const mag = (((man << 3) + 0x84) << exp) - 0x84;
    t[i] = (sign ? -mag : mag) / 32768;
  }
  return t;
})();
function muEncode(x: number): number {
  let s = Math.max(-32635, Math.min(32635, Math.round(x * 32767)));
  const sign = s < 0 ? 0x80 : 0;
  if (sign) s = -s;
  s += 0x84;
  let exp = 7;
  for (let mask = 0x4000; (s & mask) === 0 && exp > 0; mask >>= 1) exp--;
  const man = (s >> (exp + 3)) & 0x0f;
  return ~(sign | (exp << 4) | man) & 0xff;
}

interface Line { who: "agent" | "caller" | "note"; text: string }

// ── "Run checks": every link of a call, tested one by one before you talk ────────
type CheckState = "wait" | "run" | "ok" | "bad" | "skip";
interface Check { id: string; label: string; state: CheckState; detail?: string; fix?: string }
const VA_CHECKS: Check[] = [
  { id: "voice_agent", label: "Deepgram Voice Agent (hears, answers and speaks)", state: "wait" },
  { id: "mic", label: "Your microphone", state: "wait" },
  { id: "live", label: "Live connection to the server", state: "wait" },
];
const CHECKS: Check[] = [
  { id: "model", label: "Call model (with a tool, as on a call)", state: "wait" },
  { id: "backup", label: "Backup model", state: "wait" },
  { id: "voice", label: "Agent's voice", state: "wait" },
  { id: "hearing", label: "Hearing (speech-to-text)", state: "wait" },
  { id: "mic", label: "Your microphone", state: "wait" },
  { id: "live", label: "Live connection to the server", state: "wait" },
];
const words = (t: string) => t.toLowerCase().replace(/[^a-z ]/g, " ").split(/\s+/).filter(Boolean);

function RunChecks() {
  const [checks, setChecks] = useState<Check[] | null>(null);
  const [busy, setBusy] = useState(false);
  const set = (id: string, u: Partial<Check>) => setChecks((cs) => (cs ?? []).map((c) => c.id === id ? { ...c, ...u } : c));
  const secs = (ms?: number) => `${((ms ?? 0) / 1000).toFixed(1)} s`;

  const run = async () => {
    setBusy(true);
    const mode = await getFullConfig().then((c) => String(c.voip?.mode ?? "")).catch(() => "");
    setChecks((mode === "voice_agent" ? VA_CHECKS : CHECKS).map((c) => ({ ...c })));
    if (mode === "voice_agent") {
      set("voice_agent", { state: "run" });
      try {
        const r = await testVoiceAgent();
        set("voice_agent", r.ok
          ? { state: "ok", detail: `${r.model} · ${r.voice} · connected in ${r.connect_ms} ms · first audio ${r.first_audio_ms} ms` }
          : { state: "bad", detail: r.error, fix: "Calls → Speech → Deepgram Voice Agent: check the key, the model and the voice." });
      } catch (e: any) { set("voice_agent", { state: "bad", detail: String(e?.message || e) }); }
    } else await standard();
    await browserChecks();
    setBusy(false);
  };

  const standard = async () => {
    // 1. the call model, exactly as a call uses it
    set("model", { state: "run" });
    try {
      const m = await testModel();
      const q = m.quota?.free_limit ? ` · free requests today ${m.quota.free_used ?? 0}/${m.quota.free_limit}` : "";
      set("model", m.ok
        ? { state: "ok", detail: `${m.spec} · ${secs(m.ms_total)}${m.tool_call ? ` · used ${m.tool_call.name}` : " · no tool call"}${q}` }
        : { state: "bad", detail: `${m.spec}: ${m.reason}${q}`, fix: m.fix });
    } catch (e: any) { set("model", { state: "bad", detail: String(e?.message || e) }); }
    // 2. the backup model (answers when the call model can't)
    set("backup", { state: "run" });
    try {
      const b = await getBackupModel();
      if (!b.enabled) set("backup", { state: "skip", detail: "off", fix: "Recommended with free models: Settings → Backup model." });
      else {
        const t = await testBackupModel();
        set("backup", t.ok ? { state: "ok", detail: `${t.model} · ${t.seconds} s` }
          : { state: "bad", detail: t.error, fix: "Settings → Backup model: check the server and that the model is downloaded." });
      }
    } catch (e: any) { set("backup", { state: "bad", detail: String(e?.message || e) }); }
    // 3 + 4. voice, then hear that voice back with speech-to-text
    set("voice", { state: "run" }); set("hearing", { state: "run" });
    try {
      const sp = await testSpeech(true);
      if (!sp.tts_ms) {
        set("voice", { state: "bad", detail: sp.error, fix: "Calls → Speech: choose \"Local on this server\" (needs xplogent-docker speech on) or add the voice provider's key." });
        set("hearing", { state: "skip", detail: "needs a working voice first" });
      } else {
        set("voice", { state: "ok", detail: `${sp.tts_ms} ms for ${sp.audio_seconds} s of speech` });
        const said = new Set(words(sp.sentence || "")), heard = words(sp.transcript || "");
        const share = said.size ? Math.round(100 * heard.filter((w) => said.has(w)).length / said.size) : 0;
        set("hearing", sp.error
          ? { state: "bad", detail: sp.error, fix: "Calls → Speech: built-in Whisper needs the voip extra; Deepgram needs its key." }
          : { state: share >= 60 ? "ok" : "bad", detail: `"${sp.transcript}" · ${share}% of the words · ${sp.stt_ms} ms`,
              fix: share >= 60 ? undefined : "The voice or speech-to-text model may be poor; try another voice or a larger Whisper model." });
      }
    } catch (e: any) { set("voice", { state: "bad", detail: String(e?.message || e) }); set("hearing", { state: "skip" }); }
  };

  const browserChecks = async () => {
    // 5. microphone (browser side)
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      set("mic", { state: "bad", detail: "this page is plain http://, and browsers only allow the microphone on HTTPS or localhost",
        fix: "sudo xplogent-docker https your.domain (or an SSH tunnel to http://localhost:8765). Typing works meanwhile." });
    } else {
      let perm = "prompt";
      try { perm = (await navigator.permissions.query({ name: "microphone" as PermissionName })).state; } catch { /* not supported */ }
      set("mic", perm === "denied"
        ? { state: "bad", detail: "blocked for this site", fix: "Allow the microphone with the icon in the address bar, then reload." }
        : { state: "ok", detail: perm === "granted" ? "allowed" : "the browser will ask when you start the test call" });
    }
    // 6. the live connection a test call uses
    set("live", { state: "run" });
    await new Promise<void>((done) => {
      let ok = false, told = false;
      // The same path and sign-in as a test call, but nothing is started or stored.
      const sock = new WebSocket(wsUrl(`/ws/voip/ping?${wsTokenParam().slice(1)}`));
      const timer = setTimeout(() => { sock.close(); }, 8000);
      sock.onopen = () => sock.send("{}");
      sock.onmessage = (ev) => {
        if (typeof ev.data !== "string") return;
        const m = JSON.parse(ev.data);
        if (m.type === "pong") { ok = true; set("live", { state: "ok", detail: "connected" }); sock.close(); }
        if (m.type === "error") { told = true; set("live", { state: "bad", detail: m.error }); }
      };
      sock.onclose = () => {
        clearTimeout(timer);
        if (!ok && !told) set("live", { state: "bad", detail: "could not open the live connection",
          fix: "If a reverse proxy (nginx…) is in front of Xplogent it must pass WebSocket upgrades for /ws." });
        done();
      };
    });
  };

  const ICON = { wait: Circle, run: Loader2, ok: CheckCircle2, bad: XCircle, skip: MinusCircle };
  const cls: Record<CheckState, string> = { wait: "faint", run: "dim", ok: "ok", bad: "err", skip: "faint" };
  return (
    <div className="subtle mt-4">
      <div className="row wrap" style={{ gap: 10 }}>
        <Button icon={Stethoscope} loading={busy} onClick={run}>{busy ? "Checking…" : "Run checks"}</Button>
        <span className="dim small grow">Tests every link of a call for the chosen conversation mode: model, voice, hearing (or the
          Voice Agent), your microphone and the live connection.</span>
      </div>
      {checks && (
        <ul className="list checks mt-2">
          {checks.map((c) => {
            const I = ICON[c.state];
            return (
              <li key={c.id} style={{ alignItems: "flex-start", flexWrap: "nowrap" }}>
                <I size={16} className={cls[c.state]} style={{ flexShrink: 0, marginTop: 2 }}
                  aria-label={c.state === "ok" ? "passed" : c.state === "bad" ? "failed" : c.state} />
                <div className="grow"><b>{c.label}</b>{c.detail && <span className="dim"> · {c.detail}</span>}
                  {c.fix && c.state !== "ok" && <div className="dim">→ {c.fix}</div>}</div>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

export function TestCall({ contacts }: { contacts?: { id: number; name: string }[] }) {
  const [state, setState] = useState<"idle" | "live" | "ended">("idle");
  const [lines, setLines] = useState<Line[]>([]);
  const [text, setText] = useState("");
  const [voice, setVoice] = useState(true);
  const [contact, setContact] = useState<string>("");
  const [mic, setMic] = useState(false);
  const [thinking, setThinking] = useState(false);
  useEffect(() => {                       // a turn that produced nothing must not leave it on
    if (!thinking) return;
    const id = setTimeout(() => setThinking(false), 60000);
    return () => clearTimeout(id);
  }, [thinking]);
  const ws = useRef<WebSocket | null>(null);
  const audio = useRef<{ ctx: AudioContext; next: number; sources: AudioBufferSourceNode[] } | null>(null);
  const micStop = useRef<(() => void) | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const canMic = typeof window !== "undefined" && window.isSecureContext && !!navigator.mediaDevices?.getUserMedia;
  const [useMic, setUseMic] = useState(canMic);
  useEffect(() => { box.current?.scrollTo({ top: box.current.scrollHeight }); }, [lines.length]);
  useEffect(() => () => { ws.current?.close(); micStop.current?.(); audio.current?.ctx.close(); }, []);

  const add = (l: Line) => setLines((ls) => [...ls, l]);
  const play = (buf: ArrayBuffer) => {
    const a = audio.current;
    if (!a) return;
    const bytes = new Uint8Array(buf);
    const ab = a.ctx.createBuffer(1, bytes.length, 8000);
    const ch = ab.getChannelData(0);
    for (let i = 0; i < bytes.length; i++) ch[i] = MU_DECODE[bytes[i]];
    const src = a.ctx.createBufferSource();
    src.buffer = ab; src.connect(a.ctx.destination);
    a.next = Math.max(a.next, a.ctx.currentTime + 0.08);
    src.start(a.next); a.next += ab.duration;
    a.sources.push(src);
    src.onended = () => { a.sources = a.sources.filter((s) => s !== src); };
  };
  const clearAudio = () => {
    const a = audio.current;
    if (!a) return;
    a.sources.forEach((s) => { try { s.stop(); } catch { /* already stopped */ } });
    a.sources = []; a.next = 0;
  };

  const start = () => {
    setLines([]); setState("live"); setThinking(false);
    if (voice || useMic) {
      // Created and resumed inside the click, so the browser lets it play.
      const ctx = new AudioContext();
      ctx.resume().catch(() => undefined);
      audio.current = { ctx, next: 0, sources: [] };
    }
    // Asking here (still inside the click) is what makes the browser show its
    // microphone prompt; a refusal leaves the call running as text.
    if (useMic && canMic) startMic().catch((e) => add({ who: "note", text: `⚠ microphone: ${micError(e)}` }));
    let started = false;
    const sock = new WebSocket(wsUrl(`/ws/voip/test?${wsTokenParam().slice(1)}`));
    sock.binaryType = "arraybuffer";
    ws.current = sock;
    let opened = false;
    sock.onopen = () => { opened = true; sock.send(JSON.stringify({ type: "start", voice, contact_id: contact ? Number(contact) : null })); };
    sock.onmessage = (ev) => {
      if (typeof ev.data !== "string") { play(ev.data as ArrayBuffer); return; }
      const m = JSON.parse(ev.data);
      if (m.type === "started") { started = true; add({ who: "note", text: m.contact ? `test call as ${m.contact}` : "test call started" }); }
      else if (m.type === "turn") { if (m.speaker === "agent") setThinking(false); add({ who: m.speaker === "agent" ? "agent" : "caller", text: m.text }); }
      else if (m.type === "thinking") setThinking(true);
      else if (m.type === "events") setThinking(false);
      else if (m.type === "voice_failed") add({ who: "note", text: `⚠ voice failed: ${m.error} — continuing as text. Check the voice under Speech (or untick "play the agent's voice").` });
      else if (m.type === "events") m.items.forEach((e: string) => add({ who: "note", text: `🔧 ${e}` }));
      else if (m.type === "clear") clearAudio();
      else if (m.type === "transfer") add({ who: "note", text: `📞 the agent transfers the call${m.to ? ` to ${m.to}` : ""} (${m.reason || "no reason"})` });
      else if (m.type === "hangup") add({ who: "note", text: "📴 the agent hung up" });
      else if (m.type === "dtmf_sent") add({ who: "note", text: `🔢 the agent pressed ${m.digits}` });
      else if (m.type === "error") { setThinking(false); add({ who: "note", text: `⚠ ${m.error}` }); }
      else if (m.type === "ended") {
        setThinking(false);
        const slowest = Math.max(0, ...(m.latency_ms ?? []));
        if (slowest > 3000) add({ who: "note", text: `⚠ ${(slowest / 1000).toFixed(1)} s before the agent's first word: callers start hanging up after about 3 s. If the call model runs on this server's CPU, pick a hosted or GPU model (Calls → Model for calls → Test this model).` });
        add({ who: "note", text: `ended · outcome: ${m.outcome || "—"}${m.summary ? ` · ${m.summary}` : ""}${m.latency_ms?.length ? ` · first-word delay ${m.latency_ms.join(", ")} ms` : ""}` });
      }
    };
    sock.onclose = (ev) => {
      setState("ended"); setThinking(false); stopMic();
      if (!started) add({ who: "note", text: `⚠ the test call could not start${
        !opened ? ": the browser could not open a live connection to the server. If a reverse proxy (nginx, Apache…) is in front of Xplogent, it must pass WebSocket upgrades for /ws; check that Xplogent is running."
        : ev.code === 1008 ? " (not signed in, or the session expired: reload the page)"
        : ev.reason ? `: ${ev.reason}` : " (the server closed the connection)"}` });
    };
  };
  const send = () => {
    if (!text.trim() || ws.current?.readyState !== WebSocket.OPEN) return;
    clearAudio();
    ws.current.send(JSON.stringify({ type: "text", text }));
    setText("");
  };
  const hang = () => ws.current?.send(JSON.stringify({ type: "hangup" }));
  const press = (digit: string) => {
    if (ws.current?.readyState !== WebSocket.OPEN) return;
    clearAudio();
    ws.current.send(JSON.stringify({ type: "dtmf", digit }));
  };

  const micError = (e: any) => e?.name === "NotAllowedError"
    ? "permission denied. Allow the microphone for this site (the icon in the address bar) and start again; typing still works."
    : e?.name === "NotFoundError" ? "no microphone found." : String(e?.message || e);
  const stopMic = () => { micStop.current?.(); micStop.current = null; setMic(false); };
  const startMic = async () => {
    const a = audio.current ?? { ctx: new AudioContext(), next: 0, sources: [] };
    audio.current = a;
    const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
    const srcNode = a.ctx.createMediaStreamSource(stream);
    const proc = a.ctx.createScriptProcessor(4096, 1, 1);
    const ratio = a.ctx.sampleRate / 8000;
    let pos = 0;
    let out: number[] = [];
    proc.onaudioprocess = (e) => {
      const input = e.inputBuffer.getChannelData(0);
      for (; pos < input.length; pos += ratio) out.push(muEncode(input[Math.floor(pos)]));
      pos -= input.length;
      while (out.length >= 160) {
        if (ws.current?.readyState === WebSocket.OPEN) ws.current.send(new Uint8Array(out.slice(0, 160)));
        out = out.slice(160);
      }
    };
    srcNode.connect(proc); proc.connect(a.ctx.destination);
    micStop.current = () => { proc.disconnect(); srcNode.disconnect(); stream.getTracks().forEach((t) => t.stop()); };
    setMic(true);
  };

  return (
    <Card title="Test call in the browser"
      description={`Talk to the phone agent exactly as a caller would (same greeting, playbook, knowledge and tools), without a phone line. Type as the caller${canMic ? " or talk into your microphone" : ""}.`}>
      {!canMic && (
        <p className="dim small"><MicOff size={12} aria-hidden /> Browsers only allow the microphone on HTTPS (or
          on <span className="mono">localhost</span>), so it is off on this address; typing works. To talk: run{" "}
          <span className="mono">sudo xplogent-docker https your.domain</span>, or open the dashboard through an SSH
          tunnel (<span className="mono">ssh -L 8765:localhost:8765 you@server</span>, then{" "}
          <span className="mono">http://localhost:8765</span>).</p>)}
      {state !== "live" ? (
        <div className="row wrap" style={{ gap: 16 }}>
          <label className="check"><input type="checkbox" checked={voice} onChange={(e) => setVoice(e.target.checked)} />
            Play the agent's voice</label>
          <label className="check" title={canMic ? "" : "needs HTTPS or localhost"}>
            <input type="checkbox" checked={useMic && canMic} disabled={!canMic} onChange={(e) => setUseMic(e.target.checked)} />
            Talk with my microphone</label>
          {contacts && contacts.length > 0 && (
            <select value={contact} onChange={(e) => setContact(e.target.value)} aria-label="Call as" style={{ width: "auto", minWidth: 200 }}>
              <option value="">As an unknown caller</option>
              {contacts.map((c) => <option key={c.id} value={c.id}>As {c.name || `contact ${c.id}`}</option>)}
            </select>)}
          <Button variant="primary" icon={Phone} onClick={start}>Start test call</Button>
        </div>
      ) : (
        <div className="row wrap" style={{ gap: 8 }}>
          <Button variant="danger" icon={PhoneOff} onClick={hang}>Hang up</Button>
          {canMic ? (mic
            ? <Button icon={MicOff} onClick={stopMic}>Mic off</Button>
            : <Button icon={Mic} onClick={() => startMic().catch((e) => add({ who: "note", text: `⚠ microphone: ${micError(e)}` }))}>Use microphone</Button>)
            : <Button icon={MicOff} disabled title="Browsers only allow the microphone on HTTPS or localhost">Microphone needs HTTPS</Button>}
          {mic && <Badge tone="ok">Listening: just talk</Badge>}
        </div>
      )}
      {state !== "live" && <RunChecks />}
      {lines.length > 0 && (
        <div ref={box} className="transcript" aria-live="polite" style={{ marginTop: 12 }}>
          {lines.map((l, i) => l.who === "note"
            ? <div key={i} className="dim xsmall">{l.text}</div>
            : <div key={i} className={`turn ${l.who}`}><b>{l.who === "agent" ? "Agent" : "You"}:</b> {l.text}</div>)}
          {thinking && state === "live" && <div className="dim xsmall">… the agent is thinking</div>}
        </div>
      )}
      {state === "live" && (
        <div className="keypad" aria-label="phone keypad">
          {["1", "2", "3", "4", "5", "6", "7", "8", "9", "*", "0", "#"].map((k) => (
            <button key={k} type="button" className="btn" aria-label={`Press ${k}`} onClick={() => press(k)}>{k}</button>))}
        </div>
      )}
      {state === "live" && (
        <div className="row" style={{ gap: 6, marginTop: 8 }}>
          <input className="grow" value={text} placeholder="Say something as the caller…" autoFocus aria-label="Say as the caller"
            onChange={(e) => setText(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") send(); }} />
          <Button variant="primary" icon={Send} disabled={!text.trim()} onClick={send} aria-label="Send">Send</Button>
        </div>
      )}
    </Card>
  );
}
