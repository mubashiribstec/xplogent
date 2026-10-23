import { useEffect, useRef, useState } from "react";
import { Mic, MicOff, Pause, Phone, PhoneForwarded, PhoneOff, Play, Send } from "lucide-react";
import {
  CallResult, CallTurn, LiveCallInfo, dialCall, getCall, hangupCall, instructCall, pauseAI, sayOnCall,
  transferCall, wsTokenParam, wsUrl,
} from "../api";

type Toaster = (m: string, kind?: any) => void;
const since = (t: number) => { const s = Math.max(0, Math.round(Date.now() / 1000 - t)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };

/** Dial, asking before calling outside calling hours. */
export async function placeCall(body: Parameters<typeof dialCall>[0], toast: Toaster): Promise<boolean> {
  let res: CallResult = await dialCall(body);
  if (!res.ok && res.hours && confirm(`${res.error}.\n\nCall anyway?`)) res = await dialCall({ ...body, ignore_hours: true });
  if (res.ok) toast(body.test ? "calling your phone…" : "calling…", "success");
  else if (!res.hours || res.error) toast(res.error ?? "call failed", "error");
  return !!res.ok;
}

// ── live calls ───────────────────────────────────────────────────────────────
export function LiveCalls({ active, toast, onChange }: { active: LiveCallInfo[]; toast: Toaster; onChange: () => void }) {
  if (!active.length) return null;
  return (
    <div className="card">
      <h3>Live calls ({active.length})</h3>
      {active.map((c) => <LiveCall key={c.call_id} call={c} toast={toast} onChange={onChange} />)}
    </div>
  );
}

function LiveCall({ call, toast, onChange }: { call: LiveCallInfo; toast: Toaster; onChange: () => void }) {
  const [turns, setTurns] = useState<CallTurn[]>([]);
  const [text, setText] = useState("");
  const [, tick] = useState(0);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const load = () => { getCall(call.call_id).then((r) => setTurns(r.call?.turns ?? [])).catch(() => {}); tick((n) => n + 1); };
    load();
    const id = setInterval(load, 1500);
    return () => clearInterval(id);
  }, [call.call_id]);
  useEffect(() => { box.current?.scrollTo({ top: box.current.scrollHeight }); }, [turns.length]);

  const act = async (p: Promise<CallResult>, ok: string) => {
    const r = await p;
    if (r.ok) toast(ok, "success"); else toast(r.error ?? "failed", "error");
    onChange();
    return r.ok;
  };
  const say = async () => { if (text.trim() && await act(sayOnCall(call.call_id, text), "the agent is saying it")) setText(""); };
  const instruct = async () => { if (text.trim() && await act(instructCall(call.call_id, text), "instruction added")) setText(""); };
  const transfer = () => {
    const to = prompt("Transfer to which number? (blank = the hand-off number)", "");
    if (to !== null) act(transferCall(call.call_id, to.trim()), "transferring");
  };

  return (
    <div className="live-call">
      <div className="row wrap" style={{ gap: 8, justifyContent: "space-between" }}>
        <div>
          <b>{call.contact_name || call.number || "unknown"}</b>{" "}
          <span className="dim">{call.direction} · {call.transport}{call.test && call.direction !== "test" ? " · test" : ""} · {since(call.started)}</span>
          {call.paused && <span className="badge warn" style={{ marginLeft: 6 }}>AI paused</span>}
        </div>
        <div className="row" style={{ gap: 6 }}>
          <button className="btn" onClick={() => act(pauseAI(call.call_id, !call.paused), call.paused ? "AI resumed" : "AI paused")}
            title={call.paused ? "Let the AI answer again" : "Stop the AI from answering (type what to say instead)"}>
            {call.paused ? <><Play size={14} /> Resume AI</> : <><Pause size={14} /> Pause AI</>}</button>
          <button className="btn" disabled={!call.can_transfer} onClick={transfer}><PhoneForwarded size={14} /> Transfer</button>
          <button className="btn danger" onClick={() => confirm("Hang up this call?") && act(hangupCall(call.call_id), "hung up")}>
            <PhoneOff size={14} /> Hang up</button>
        </div>
      </div>
      <div ref={box} className="live-transcript">
        {turns.length === 0 && <span className="dim">waiting for the first words…</span>}
        {turns.map((t, i) => (
          <div key={i}><b style={{ color: t.speaker === "agent" ? "var(--accent)" : "var(--amber)" }}>
            {t.speaker === "agent" ? "Agent" : "Caller"}:</b> {t.text}</div>
        ))}
      </div>
      {call.notes.length > 0 && <div className="dim" style={{ fontSize: 12 }}>Instructions: {call.notes.join(" · ")}</div>}
      <div className="row" style={{ gap: 6, marginTop: 6 }}>
        <input style={{ flex: 1 }} value={text} placeholder="Type a sentence for the agent to say, or an instruction"
          onChange={(e) => setText(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") say(); }} />
        <button className="btn primary" disabled={!text.trim()} onClick={say} title="The agent says this now">Say</button>
        <button className="btn" disabled={!text.trim()} onClick={instruct}
          title="A private note the agent follows from its next reply on">Instruct</button>
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
  useEffect(() => { if (!via.includes(line) && via[0]) setLine(via[0]); }, [via.join(",")]);
  const call = async (n: string, test: boolean) => {
    setBusy(true);
    try { if (await placeCall({ number: n, via: line, test }, toast)) onCalled(); } finally { setBusy(false); }
  };
  return (
    <div className="card">
      <h3><Phone size={16} /> Call now</h3>
      <div className="row wrap" style={{ gap: 8 }}>
        <input value={number} placeholder="+15551234567" onChange={(e) => setNumber(e.target.value)} style={{ width: 200 }} />
        {via.length > 1 && <select value={line} onChange={(e) => setLine(e.target.value)}>
          {via.map((v) => <option key={v} value={v}>{v === "sip" ? "SIP trunk" : v === "extension" ? "PBX extension" : "Telnyx API"}</option>)}</select>}
        <button className="btn primary" disabled={busy || !number.trim()} onClick={() => call(number, false)}>Call</button>
      </div>
      <p className="dim" style={{ fontSize: 12 }}>The AI agent places the call. Numbers on the do-not-call list are refused;
        outside calling hours you're asked first. You can also call a customer from Contacts.</p>
      <h3 style={{ marginTop: 14 }}>Test on my phone</h3>
      <div className="row wrap" style={{ gap: 8 }}>
        <input value={me} placeholder="your mobile, +1555…" style={{ width: 200 }}
          onChange={(e) => { setMe(e.target.value); try { localStorage.setItem("xpl_test_phone", e.target.value); } catch { /* not saved */ } }} />
        <button className="btn" disabled={busy || !me.trim()} onClick={() => call(me, true)}>Call me</button>
      </div>
      <p className="dim" style={{ fontSize: 12 }}>A test call: not counted in stats and never changes a contact.</p>
    </div>
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

export function TestCall({ contacts }: { contacts?: { id: number; name: string }[] }) {
  const [state, setState] = useState<"idle" | "live" | "ended">("idle");
  const [lines, setLines] = useState<Line[]>([]);
  const [text, setText] = useState("");
  const [voice, setVoice] = useState(true);
  const [contact, setContact] = useState<string>("");
  const [mic, setMic] = useState(false);
  const ws = useRef<WebSocket | null>(null);
  const audio = useRef<{ ctx: AudioContext; next: number; sources: AudioBufferSourceNode[] } | null>(null);
  const micStop = useRef<(() => void) | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const canMic = typeof window !== "undefined" && window.isSecureContext && !!navigator.mediaDevices;
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
    setLines([]); setState("live");
    if (voice) {
      const ctx = new AudioContext();
      audio.current = { ctx, next: 0, sources: [] };
    }
    const sock = new WebSocket(wsUrl(`/ws/voip/test?${wsTokenParam().slice(1)}`));
    sock.binaryType = "arraybuffer";
    ws.current = sock;
    sock.onopen = () => sock.send(JSON.stringify({ type: "start", voice, contact_id: contact ? Number(contact) : null }));
    sock.onmessage = (ev) => {
      if (typeof ev.data !== "string") { play(ev.data as ArrayBuffer); return; }
      const m = JSON.parse(ev.data);
      if (m.type === "started") add({ who: "note", text: m.contact ? `test call as ${m.contact}` : "test call started" });
      else if (m.type === "turn") add({ who: m.speaker === "agent" ? "agent" : "caller", text: m.text });
      else if (m.type === "events") m.items.forEach((e: string) => add({ who: "note", text: `🔧 ${e}` }));
      else if (m.type === "clear") clearAudio();
      else if (m.type === "transfer") add({ who: "note", text: `📞 the agent transfers the call${m.to ? ` to ${m.to}` : ""} (${m.reason || "no reason"})` });
      else if (m.type === "hangup") add({ who: "note", text: "📴 the agent hung up" });
      else if (m.type === "dtmf_sent") add({ who: "note", text: `🔢 the agent pressed ${m.digits}` });
      else if (m.type === "error") add({ who: "note", text: `⚠ ${m.error}` });
      else if (m.type === "ended") {
        add({ who: "note", text: `ended · outcome: ${m.outcome || "—"}${m.summary ? ` · ${m.summary}` : ""}${m.latency_ms?.length ? ` · first-word delay ${m.latency_ms.join(", ")} ms` : ""}` });
      }
    };
    sock.onclose = () => { setState("ended"); stopMic(); };
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
    <div className="card">
      <h3>Test call in the browser</h3>
      <p className="dim">Talk to the phone agent exactly as a caller would (same greeting, playbook, knowledge and
        tools), without a phone line. Type as the caller{canMic ? " or use the microphone" : ""}.
        {!canMic && " The microphone needs HTTPS; typing works everywhere."}</p>
      {state !== "live" ? (
        <div className="row wrap" style={{ gap: 10 }}>
          <label className="row" style={{ gap: 6 }}><input type="checkbox" checked={voice} onChange={(e) => setVoice(e.target.checked)} />
            play the agent's voice</label>
          {contacts && contacts.length > 0 && (
            <select value={contact} onChange={(e) => setContact(e.target.value)}>
              <option value="">as an unknown caller</option>
              {contacts.map((c) => <option key={c.id} value={c.id}>as {c.name || `contact ${c.id}`}</option>)}
            </select>)}
          <button className="btn primary" onClick={start}><Phone size={14} /> Start test call</button>
        </div>
      ) : (
        <div className="row wrap" style={{ gap: 8 }}>
          <button className="btn danger" onClick={hang}><PhoneOff size={14} /> Hang up</button>
          {canMic && (mic
            ? <button className="btn" onClick={stopMic}><MicOff size={14} /> Mic off</button>
            : <button className="btn" onClick={() => startMic().catch((e) => add({ who: "note", text: `⚠ microphone: ${e.message}` }))}>
                <Mic size={14} /> Use microphone</button>)}
        </div>
      )}
      {lines.length > 0 && (
        <div ref={box} className="live-transcript" style={{ marginTop: 10 }}>
          {lines.map((l, i) => l.who === "note"
            ? <div key={i} className="dim" style={{ fontSize: 12 }}>{l.text}</div>
            : <div key={i}><b style={{ color: l.who === "agent" ? "var(--accent)" : "var(--amber)" }}>
                {l.who === "agent" ? "Agent" : "You"}:</b> {l.text}</div>)}
        </div>
      )}
      {state === "live" && (
        <div className="keypad" aria-label="phone keypad">
          {["1", "2", "3", "4", "5", "6", "7", "8", "9", "*", "0", "#"].map((k) => (
            <button key={k} className="btn" onClick={() => press(k)}>{k}</button>))}
        </div>
      )}
      {state === "live" && (
        <div className="row" style={{ gap: 6, marginTop: 6 }}>
          <input style={{ flex: 1 }} value={text} placeholder="Say something as the caller…" autoFocus
            onChange={(e) => setText(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") send(); }} />
          <button className="btn primary" disabled={!text.trim()} onClick={send}><Send size={14} /></button>
        </div>
      )}
    </div>
  );
}
