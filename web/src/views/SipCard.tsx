import { useEffect, useState } from "react";
import { SipStatus, detectSipIp, getSipStatus, saveSip, testSip } from "../api";

type Toaster = (m: string, kind?: any) => void;

/** Built-in SIP trunk client: account, network, registration status. */
export function SipCard({ cfg, toast, onChange }: { cfg: Record<string, any>; toast: Toaster; onChange: () => void }) {
  const sip = (cfg.sip ?? {}) as Record<string, any>;
  const [st, setSt] = useState<SipStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [ips, setIps] = useState<{ lan: string; internet: string; note: string } | null>(null);
  const [form, setForm] = useState<Record<string, any>>({});
  const val = (k: string, d: any = "") => (k in form ? form[k] : sip[k] ?? d);
  const set = (k: string, v: any) => setForm((f) => ({ ...f, [k]: v }));

  const load = () => getSipStatus().then(setSt).catch(() => {});
  useEffect(() => { load(); const id = setInterval(load, 5000); return () => clearInterval(id); }, []);

  const save = async () => {
    setBusy(true); setMsg(null);
    try {
      const body: Record<string, any> = { ...form };
      if (!body.password) delete body.password;
      const r = await saveSip(body);
      if (!r.ok) { setMsg({ ok: false, text: r.error ?? "could not save" }); return; }
      setForm({}); onChange();
      const t = await testSip();
      setSt(t.status);
      setMsg(t.ok ? { ok: true, text: t.note ?? "registered with the SIP server" } : { ok: false, text: t.error ?? "not registered" });
      toast(t.ok ? "SIP trunk registered" : "SIP settings saved", t.ok ? "success" : "info");
    } finally { setBusy(false); }
  };
  const test = async () => {
    setBusy(true); setMsg(null);
    try {
      const t = await testSip();
      setSt(t.status);
      setMsg(t.ok ? { ok: true, text: t.note ?? "registered with the SIP server" } : { ok: false, text: t.error ?? "not registered" });
    } finally { setBusy(false); }
  };

  const input = (k: string, label: string, ph = "", width = 170, type = "text") => (
    <label className="bud"><span className="lbl-sm">{label}</span>
      <input type={type} value={val(k)} placeholder={ph} style={{ width }}
        onChange={(e) => set(k, type === "number" ? Number(e.target.value) : e.target.value)} /></label>
  );

  const badge = !st ? null : st.registered
    ? <span className="badge ok">registered{st.expires ? ` · renews in ${Math.round(st.expires * 0.8 / 60)} min` : ""}</span>
    : st.running ? <span className="badge bad">not registered{st.error ? `: ${st.error}` : ""}</span>
    : <span className="badge">{st.error || "not running"}</span>;

  return (
    <div className="card">
      <h3>SIP trunk {badge}</h3>
      <p className="dim" style={{ marginTop: 0 }}>Xplogent registers like a desk phone (one extension or SIP account) and answers
        and places calls itself. No public HTTPS address is needed; the server must be reachable on UDP {val("local_port", 5060)} and
        the RTP ports below.</p>
      <div className="row wrap" style={{ gap: 10 }}>
        {input("server", "SIP server / PBX", "pbx.example.com or 192.168.1.10", 210)}
        {input("port", "Port", "5060", 80, "number")}
        {input("username", "Username / extension", "2000", 130)}
        <label className="bud"><span className="lbl-sm">Password {st?.password_set ? "✓ set" : "✗ not set"}</span>
          <input type="password" value={form.password ?? ""} placeholder={st?.password_set ? "•••••• (type to replace)" : "secret"}
            onChange={(e) => set("password", e.target.value)} style={{ width: 150 }} /></label>
        {input("auth_username", "Auth username (if different)", "", 150)}
        {input("caller_id", "Caller ID for outbound", "+15551234567", 150)}
      </div>
      <details style={{ marginTop: 8 }}>
        <summary className="dim">Network (NAT, ports)</summary>
        <div className="row wrap" style={{ gap: 10, marginTop: 8, alignItems: "flex-end" }}>
          {input("public_ip", "Address the PBX sends audio to", "auto", 170)}
          <button className="btn" onClick={() => detectSipIp().then(setIps).catch(() => {})}>Detect</button>
          {input("local_port", "Local SIP port (UDP)", "5060", 100, "number")}
          {input("rtp_start", "RTP ports from", "10000", 90, "number")}
          {input("rtp_end", "to", "10100", 90, "number")}
          {input("domain", "SIP domain (blank = server)", "", 150)}
          {input("ring_timeout", "Ring time for outbound calls (s)", "45", 90, "number")}
          {input("allow_from", "Also accept calls from (IPs)", "", 170)}
          <label className="row" style={{ gap: 6 }}>
            <input type="checkbox" checked={!!val("strict_source", false)} onChange={(e) => set("strict_source", e.target.checked)} />
            only accept calls from these addresses</label>
        </div>
        <p className="dim" style={{ fontSize: 12 }}>By default a call is also accepted from another address when it is addressed
          to this extension (needed in Docker and with carriers that use several servers).</p>
        {ips && (
          <p className="dim" style={{ fontSize: 13 }}>
            {ips.lan && <>LAN: <button className="linkish" onClick={() => set("public_ip", ips.lan)}><code>{ips.lan}</code></button> (PBX on the same network) · </>}
            {ips.internet && <>Internet: <button className="linkish" onClick={() => set("public_ip", ips.internet)}><code>{ips.internet}</code></button> (provider over the internet; forward the ports)</>}
            {ips.note && <><br />{ips.note}</>}
          </p>)}
      </details>
      <div className="row wrap" style={{ gap: 8, marginTop: 10 }}>
        <button className="btn primary" disabled={busy || !Object.keys(form).length} onClick={save}>{busy ? "Working…" : "Save & connect"}</button>
        <button className="btn" disabled={busy || !st?.configured} onClick={test}>Test registration</button>
      </div>
      {msg && <p className={msg.ok ? "ok" : "warn"}>{msg.ok ? "✓" : "✗"} {msg.text}</p>}
      {st?.last_error && <p className="dim" style={{ fontSize: 12 }}>Last problem: {st.last_error}</p>}
      <details style={{ marginTop: 8 }}>
        <summary className="dim">How to connect FreePBX / Asterisk or a Telnyx SIP connection</summary>
        <ol className="dim" style={{ fontSize: 13 }}>
          <li><b>FreePBX</b>: Applications → Extensions → add a <b>PJSIP</b> extension (e.g. 2000) for the AI. Use its number
            and secret above, and the PBX address as server. Point an inbound route (DID) or ring group at extension 2000.
            Outbound calls from the AI use the PBX's outbound routes, like any phone; the hand-off number can be an
            extension (e.g. 101), transferred with SIP REFER.</li>
          <li>In the extension's advanced settings keep <b>NAT = yes</b> (rtp_symmetric, force_rport, rewrite_contact) and allow
            the <b>ulaw/alaw</b> codecs.</li>
          <li><b>Telnyx SIP connection</b>: Voice → SIP Trunking → create a connection with <b>credentials</b> authentication;
            server <code>sip.telnyx.com</code>, its username and password above. Assign your number to that connection, and
            set the outbound voice profile so the AI can dial out.</li>
          <li><b>Docker</b>: run <code>sudo xplogent-docker sip on</code> once on the server; it publishes UDP 5060 and the RTP
            ports and tells Xplogent the host's address. Open the same UDP ports in the firewall.</li>
        </ol>
      </details>
    </div>
  );
}
