import { useEffect, useState } from "react";
import { CallDebugData, downloadDebugBundle, getCallDebug } from "../api";

type Toaster = (m: string, kind?: any) => void;
const ms = (v: any) => (typeof v === "number" && v > 0 ? `${Math.round(v)} ms` : "—");
const KIND: Record<string, string> = {
  answered: "answered", greeting: "greeting started", greeting_end: "greeting finished", caller: "caller",
  agent: "agent", barge_in: "caller interrupted", dtmf_in: "key pressed", dtmf_out: "agent pressed keys",
  transfer: "transfer started", transfer_done: "transferred", transfer_failed: "transfer failed",
  hangup: "hang-up", ended: "ended", listening: "listening first", voicemail: "answering machine",
};

/** Per-call diagnostics: timeline, stage timings, provider health, SIP ladder / Telnyx events, RTP. */
export function CallDebug({ callId, toast }: { callId: number; toast: Toaster }) {
  const [d, setD] = useState<CallDebugData | null>(null);
  const load = () => getCallDebug(callId).then(setD).catch(() => {});
  useEffect(() => {
    load();
    const id = setInterval(() => { if (d?.live !== false) load(); }, 3000);
    return () => clearInterval(id);
    // eslint-disable-next-line
  }, [callId, d?.live]);
  if (!d) return <p className="dim">loading diagnostics…</p>;
  const dbg = d.debug ?? {};
  const turns = dbg.turns ?? [];
  const sip = dbg.sip?.messages ?? [];
  return (
    <div className="debug-panel">
      <div className="row wrap" style={{ gap: 8, alignItems: "center", justifyContent: "space-between" }}>
        <b>Call debug {d.live && <span className="badge warn">live</span>}</b>
        <button className="btn" onClick={() => downloadDebugBundle(callId).then(() => toast("debug bundle saved", "success"))
          .catch((e) => toast(String(e), "error"))}>Download debug bundle</button>
      </div>
      {(d.kept || d.live) && sip.length === 0 && !(dbg.rtp && Object.keys(dbg.rtp).length) && !(dbg.telnyx_events ?? []).length && (
        <p className="dim" style={{ fontSize: 12 }}>No phone-line data for this call (a browser test call has no phone line):
          SIP messages and audio stats appear on SIP calls, Telnyx events on Telnyx calls.</p>)}
      {!d.kept && !d.live && <p className="dim">No diagnostics were kept for this call (older than the retention period, or made before this version).</p>}

      {turns.length > 0 && (<>
        <h4>Turns (time to first audio after the caller stops)</h4>
        <div className="table-scroll"><table className="data-table">
          <thead><tr><th>#</th><th>Speech-to-text</th><th>Model</th><th>Voice</th><th>Total</th><th>Served by</th><th>Problems</th></tr></thead>
          <tbody>{turns.map((t, i) => (
            <tr key={i}>
              <td>{i + 1}</td><td>{ms(t.stt_ms)}</td><td>{ms(t.llm_first_sentence_ms)}</td>
              <td>{ms(t.tts_first_audio_ms)}</td><td><b>{ms(t.total_ms)}</b></td>
              <td className="dim mono" style={{ fontSize: 11 }}>{[t.stt, t.llm, t.tts].filter(Boolean).join(" · ")}</td>
              <td>{(t.fallbacks ?? []).map((f: string) => <span key={f} className="badge warn" style={{ marginRight: 4 }}>{f}</span>)}
                {t.error && <span className="warn" style={{ fontSize: 12 }}>{t.error}</span>}</td>
            </tr>))}</tbody>
        </table></div>
      </>)}

      {(dbg.guard_blocked ?? []).length > 0 && (<>
        <h4>Not said: figures the agent couldn't confirm</h4>
        <p className="dim" style={{ fontSize: 12 }}>Each sentence below named pay, hours or a date that isn't in the
          vacancy's details, the knowledge base or what the caller said. The caller heard "the recruiter will confirm it"
          instead. If the figure is right, add it to the vacancy in the CRM or to Knowledge.</p>
        <ul className="list">{(dbg.guard_blocked ?? []).map((g, i) => (
          <li key={i}><span className="warn">✗</span> “{g.text}” <span className="dim">· {g.claims.join(", ")}</span></li>))}</ul>
      </>)}

      {(dbg.timeline ?? []).length > 0 && (<>
        <h4>Timeline</h4>
        <ul className="list timeline">{(dbg.timeline ?? []).map((e, i) => (
          <li key={i}><span className="mono dim">{e.t.toFixed(1)}s</span> <b>{KIND[e.kind] ?? e.kind}</b>
            {e.detail && <span className="dim"> · {e.detail}</span>}</li>))}</ul>
      </>)}

      {sip.length > 0 && (<>
        <h4>SIP messages{dbg.line ? ` (${dbg.line} line)` : ""}{dbg.sip?.dropped ? ` · ${dbg.sip.dropped} older not kept` : ""}</h4>
        <ul className="list ladder">{sip.map((m, i) => (
          <li key={i} title={Object.entries(m.headers).map(([k, v]) => `${k}: ${v}`).join("\n")}>
            <span className="mono dim">{m.t.toFixed(3)}s</span>{" "}
            <span className={m.dir === "out" ? "ok" : ""}>{m.dir === "out" ? "→ sent" : "← received"}</span>{" "}
            <b className="mono">{m.line}</b>{m.sdp && <span className="dim"> (SDP)</span>}
            <span className="dim"> {m.peer}</span>
          </li>))}</ul>
      </>)}

      {dbg.rtp && Object.keys(dbg.rtp).length > 0 && (<>
        <h4>Audio (RTP)</h4>
        <p className="dim mono" style={{ fontSize: 12 }}>{Object.entries(dbg.rtp).map(([k, v]) => `${k} ${String(v)}`).join(" · ")}</p>
      </>)}

      {(dbg.telnyx_events ?? []).length > 0 && (<>
        <h4>Telnyx events</h4>
        <ul className="list">{(dbg.telnyx_events ?? []).map((e, i) => (
          <li key={i}><span className="mono dim">{e.t.toFixed(1)}s</span> {e.event}{e.leg && <span className="dim"> ({e.leg} leg)</span>}{e.digit && ` · ${e.digit}`}</li>))}</ul>
        {dbg.media && <p className="dim mono" style={{ fontSize: 12 }}>{Object.entries(dbg.media).map(([k, v]) => `${k} ${String(v)}`).join(" · ")}</p>}
      </>)}
      {(dbg.errors ?? []).length > 0 && <p className="warn">{(dbg.errors ?? []).join("; ")}</p>}
    </div>
  );
}
