import { useEffect, useState } from "react";
import { SipExtensionStatus, detectSipIp, getSipStatus, saveSipExtension, testSipExtension } from "../api";

type Toaster = (m: string, kind?: any) => void;

/** Second SIP login: a FreePBX extension for the agent (internal calls, queue
 *  overflow), independent of the SIP trunk above it. */
export function SipExtensionCard({ cfg, toast, onChange }: { cfg: Record<string, any>; toast: Toaster; onChange: () => void }) {
  const ext = (cfg.sip_extension ?? {}) as Record<string, any>;
  const [st, setSt] = useState<SipExtensionStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [ips, setIps] = useState<{ lan: string; internet: string; note: string } | null>(null);
  const [form, setForm] = useState<Record<string, any>>({});
  const val = (k: string, d: any = "") => (k in form ? form[k] : ext[k] ?? d);
  const set = (k: string, v: any) => setForm((f) => ({ ...f, [k]: v }));
  const enabled = !!val("enabled", false);

  const load = () => getSipStatus().then((s) => setSt(s.extension ?? null)).catch(() => {});
  useEffect(() => { load(); const id = setInterval(load, 5000); return () => clearInterval(id); }, []);

  const save = async () => {
    setBusy(true); setMsg(null);
    try {
      const body: Record<string, any> = { ...form, enabled };
      if (!body.password) delete body.password;
      const r = await saveSipExtension(body);
      if (!r.ok) { setMsg({ ok: false, text: r.error ?? "could not save" }); return; }
      setForm({}); onChange();
      if (!enabled) { setSt(r.status ?? null); setMsg({ ok: true, text: "saved; the extension line is off" }); return; }
      const t = await testSipExtension();
      setSt(t.status);
      setMsg(t.ok ? { ok: true, text: t.note ?? "registered with the PBX" } : { ok: false, text: t.error ?? "not registered" });
      toast(t.ok ? "PBX extension registered" : "Extension settings saved", t.ok ? "success" : "info");
    } finally { setBusy(false); }
  };
  const test = async () => {
    setBusy(true); setMsg(null);
    try {
      const t = await testSipExtension();
      setSt(t.status);
      setMsg(t.ok ? { ok: true, text: t.note ?? "registered with the PBX" } : { ok: false, text: t.error ?? "not registered" });
    } finally { setBusy(false); }
  };

  const input = (k: string, label: string, ph = "", width = 170, type = "text") => (
    <label className="bud"><span className="lbl-sm">{label}</span>
      <input type={type} value={val(k)} placeholder={ph} style={{ width }}
        onChange={(e) => set(k, type === "number" ? Number(e.target.value) : e.target.value)} /></label>
  );

  const badge = !enabled ? <span className="badge">switched off</span>
    : !st ? null : st.registered
    ? <span className="badge ok">registered{st.expires ? ` · renews in ${Math.round(st.expires * 0.8 / 60)} min` : ""}</span>
    : st.running ? <span className="badge bad">not registered{st.error ? `: ${st.error}` : ""}</span>
    : <span className="badge">{st.error || "not running"}</span>;

  return (
    <div className="card">
      <h3>PBX extension (FreePBX) {badge}</h3>
      <p className="dim" style={{ marginTop: 0 }}>A second, separate login: Xplogent registers as one extension on your
        FreePBX, alongside the SIP trunk above (which stays reserved for your carrier). Call the agent by dialling its
        extension from any office phone, let a queue overflow to it, or have it call extensions back. It has its own
        ports — UDP {val("local_port", 5062)} and RTP {val("rtp_start", 10200)}–{val("rtp_end", 10300)} — so both lines
        can run at once.</p>
      <label className="row" style={{ gap: 6, marginBottom: 10 }}>
        <input type="checkbox" checked={enabled} onChange={(e) => set("enabled", e.target.checked)} />
        Extension line switched on</label>
      <div className="row wrap" style={{ gap: 10 }}>
        {input("server", "FreePBX / PBX address", "192.168.1.10", 210)}
        {input("port", "Port", "5060", 80, "number")}
        {input("username", "Extension number", "2000", 130)}
        <label className="bud"><span className="lbl-sm">Password {st?.password_set ? "✓ set" : "✗ not set"}</span>
          <input type="password" value={form.password ?? ""} placeholder={st?.password_set ? "•••••• (type to replace)" : "extension secret"}
            onChange={(e) => set("password", e.target.value)} style={{ width: 150 }} /></label>
        {input("auth_username", "Auth username (if different)", "", 150)}
        {input("caller_id", "Caller ID for outbound", "", 150)}
      </div>
      <details style={{ marginTop: 8 }}>
        <summary className="dim">Network (NAT, ports)</summary>
        <div className="row wrap" style={{ gap: 10, marginTop: 8, alignItems: "flex-end" }}>
          {input("public_ip", "Address the PBX sends audio to", "auto", 170)}
          <button className="btn" onClick={() => detectSipIp().then(setIps).catch(() => {})}>Detect</button>
          {input("local_port", "Local SIP port (UDP)", "5062", 100, "number")}
          {input("rtp_start", "RTP ports from", "10200", 90, "number")}
          {input("rtp_end", "to", "10300", 90, "number")}
          {input("domain", "SIP domain (blank = server)", "", 150)}
          {input("ring_timeout", "Ring time for outbound calls (s)", "45", 90, "number")}
          {input("allow_from", "Also accept calls from (IPs)", "", 170)}
          <label className="row" style={{ gap: 6 }}>
            <input type="checkbox" checked={!!val("strict_source", false)} onChange={(e) => set("strict_source", e.target.checked)} />
            only accept calls from these addresses</label>
        </div>
        {ips && (
          <p className="dim" style={{ fontSize: 13 }}>
            {ips.lan && <>LAN: <button className="linkish" onClick={() => set("public_ip", ips.lan)}><code>{ips.lan}</code></button> (PBX on the same network) · </>}
            {ips.internet && <>Internet: <button className="linkish" onClick={() => set("public_ip", ips.internet)}><code>{ips.internet}</code></button></>}
            {ips.note && <><br />{ips.note}</>}
          </p>)}
      </details>
      <div className="row wrap" style={{ gap: 8, marginTop: 10 }}>
        <button className="btn primary" disabled={busy || !Object.keys(form).length} onClick={save}>{busy ? "Working…" : "Save & connect"}</button>
        <button className="btn" disabled={busy || !st?.configured || !enabled} onClick={test}>Test registration</button>
      </div>
      {msg && <p className={msg.ok ? "ok" : "warn"}>{msg.ok ? "✓" : "✗"} {msg.text}</p>}
      {st?.last_error && <p className="dim" style={{ fontSize: 12 }}>Last problem: {st.last_error}</p>}
      <details style={{ marginTop: 8 }}>
        <summary className="dim">How to connect FreePBX</summary>
        <ol className="dim" style={{ fontSize: 13 }}>
          <li><b>FreePBX</b>: Applications → Extensions → add a <b>PJSIP</b> extension for the AI and put its number
            and secret above, with the PBX address as server.</li>
          <li>In the extension's advanced settings keep <b>NAT = yes</b> (rtp_symmetric, force_rport, rewrite_contact) and allow
            the <b>ulaw/alaw</b> codecs.</li>
          <li>Test it: dial the extension from any other office phone — the agent answers. To send callers to it, point a
            queue's fail-over destination or an inbound route at the extension.</li>
          <li><b>Docker</b>: publish UDP 5062 and 10200–10300 as well as the trunk ports, and open them in the firewall.</li>
        </ol>
      </details>
    </div>
  );
}
