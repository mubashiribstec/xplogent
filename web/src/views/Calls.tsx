import { Fragment, useEffect, useState } from "react";
import { ChevronDown, ChevronRight, Copy, PhoneCall, PhoneIncoming, PhoneOutgoing, RefreshCw } from "lucide-react";
import {
  CallRow, CallTurn, VoipStatus, getCall, getCalls, getFullConfig, getVoipStatus, patchConfig,
} from "../api";
import { useToast } from "../components/Toast";

const fmtTime = (t?: number | null) => (t ? new Date(t * 1000).toLocaleString() : "—");
const fmtDur = (c: CallRow) => {
  const end = c.ended_at ?? Date.now() / 1000;
  const s = Math.max(0, Math.round(end - c.started_at));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
};
const randomSecret = () =>
  Array.from(crypto.getRandomValues(new Uint8Array(18))).map((b) => b.toString(16).padStart(2, "0")).join("");

export function Calls() {
  const toast = useToast();
  const [status, setStatus] = useState<VoipStatus | null>(null);
  const [calls, setCalls] = useState<CallRow[]>([]);
  const [open, setOpen] = useState<number | null>(null);
  const [turns, setTurns] = useState<CallTurn[]>([]);
  const [cfg, setCfg] = useState<Record<string, any>>({});

  const reload = () => {
    getVoipStatus().then(setStatus).catch(() => {});
    getCalls().then((r) => setCalls(r.calls)).catch(() => {});
    if (open !== null) getCall(open).then((r) => setTurns(r.call?.turns ?? [])).catch(() => {});
  };
  useEffect(() => { getFullConfig().then((c) => setCfg(c.voip ?? {})).catch(() => {}); }, []);
  useEffect(() => {
    reload();
    const id = setInterval(reload, 3000);   // live transcript while a call is active
    return () => clearInterval(id);
    // eslint-disable-next-line
  }, [open]);

  const save = async (updates: Record<string, any>) => {
    setCfg({ ...cfg, ...updates });
    await patchConfig({ voip: updates });
    toast("saved", "success");
    reload();
  };

  const webhook = cfg.public_url && cfg.webhook_secret
    ? `${String(cfg.public_url).replace(/\/$/, "")}/telephony/webhook/${cfg.webhook_secret}` : "";

  return (
    <div className="pane">
      <div className="pane-wide">
        <div className="page-head">
          <h1><PhoneCall size={22} /> Calls</h1>
          <div className="row" style={{ gap: 8 }}>
            <span className={`badge ${status?.enabled ? "ok" : "bad"}`}>{status?.enabled ? "phone agent on" : "phone agent off"}</span>
            {(status?.active.length ?? 0) > 0 && <span className="badge warn">{status!.active.length} live</span>}
            <button className="icon-btn" onClick={reload} aria-label="refresh"><RefreshCw size={16} /></button>
          </div>
        </div>
        <p className="dim">Answers inbound calls using your scripts and knowledge base. Calls are transcribed live;
          the agent only answers from your knowledge and hands off to a person when it can't.</p>

        <div className="card">
          <h3>Setup</h3>
          <div className="row wrap" style={{ gap: 10 }}>
            <label className="bud"><span className="lbl-sm">Phone agent</span>
              <select value={cfg.enabled ? "on" : "off"} onChange={(e) => save({ enabled: e.target.value === "on" })}>
                <option value="off">off</option><option value="on">on</option></select></label>
            <label className="bud"><span className="lbl-sm">Company name</span>
              <input defaultValue={cfg.company ?? ""} onBlur={(e) => save({ company: e.target.value })} /></label>
            <label className="bud"><span className="lbl-sm">Public URL of this server</span>
              <input defaultValue={cfg.public_url ?? ""} placeholder="https://agent.example.com"
                onBlur={(e) => save({ public_url: e.target.value.trim() })} style={{ width: 240 }} /></label>
            <label className="bud"><span className="lbl-sm">Human hand-off number</span>
              <input defaultValue={cfg.transfer_number ?? ""} placeholder="+15551234567"
                onBlur={(e) => save({ transfer_number: e.target.value.trim() })} /></label>
          </div>
          <div className="row wrap" style={{ gap: 8, marginTop: 10, alignItems: "center" }}>
            <button className="btn" onClick={() => save({ webhook_secret: randomSecret() })}>
              {cfg.webhook_secret ? "Regenerate webhook secret" : "Generate webhook secret"}</button>
            {webhook && (
              <span className="mono dim" style={{ fontSize: 12, wordBreak: "break-all" }}>
                {webhook}
                <button className="linkx" title="copy" onClick={() => { navigator.clipboard?.writeText(webhook); toast("webhook URL copied", "success"); }}>
                  <Copy size={12} /></button>
              </span>
            )}
          </div>
          <p className="dim" style={{ fontSize: 12, marginTop: 8 }}>
            In Telnyx: create a Call Control application, set its webhook URL to the address above, attach your
            phone number to it, and put <code>TELNYX_API_KEY</code> in Settings → secrets. Add your scripts under
            Knowledge and your customer list under Contacts.</p>
        </div>

        <div className="card">
          <h3>Recent calls ({calls.length})</h3>
          <table className="data-table">
            <thead><tr><th></th><th>When</th><th>Caller</th><th>Duration</th><th>Outcome</th><th>Est. cost</th></tr></thead>
            <tbody>
              {calls.map((c) => (
                <Fragment key={c.id}>
                  <tr style={{ cursor: "pointer" }}
                    onClick={() => { const next = open === c.id ? null : c.id; setOpen(next); setTurns([]); }}>
                    <td>{open === c.id ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                      {c.direction === "inbound" ? <PhoneIncoming size={14} /> : <PhoneOutgoing size={14} />}</td>
                    <td className="dim">{fmtTime(c.started_at)}</td>
                    <td>{c.contact_name || (c.direction === "outbound" ? c.to_number : c.from_number) || "unknown"}</td>
                    <td>{fmtDur(c)}{c.status === "active" && <span className="badge warn" style={{ marginLeft: 6 }}>live</span>}</td>
                    <td>{c.outcome ? <span className="badge">{c.outcome}</span> : <span className="dim">—</span>}</td>
                    <td className="dim">${(c.cost ?? 0).toFixed(3)}</td>
                  </tr>
                  {open === c.id && (
                    <tr><td colSpan={6}>
                      {c.summary && <p className="dim" style={{ marginBottom: 6 }}><b>Summary:</b> {c.summary}</p>}
                      {turns.length === 0 && <p className="dim">no transcript yet</p>}
                      {turns.map((t, i) => (
                        <div key={i} style={{ margin: "3px 0" }}>
                          <b style={{ color: t.speaker === "agent" ? "var(--accent)" : "var(--amber)" }}>
                            {t.speaker === "agent" ? "Agent" : "Caller"}:</b> {t.text}
                        </div>
                      ))}
                    </td></tr>
                  )}
                </Fragment>
              ))}
              {calls.length === 0 && <tr><td colSpan={6} className="dim">no calls yet</td></tr>}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
