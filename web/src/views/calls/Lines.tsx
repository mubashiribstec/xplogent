import { useState } from "react";
import { Cable, Copy, Network, Phone, PhoneForwarded, RefreshCw } from "lucide-react";
import { putSecrets, saveSip, testTelnyx } from "../../api";
import { CardBoundary } from "../../components/CardBoundary";
import { Button, Callout, Card, ChoiceCards, ResultLine, SecretField, TextField } from "../../ui";
import { SipCard } from "../SipCard";
import { SipExtensionCard } from "../SipExtensionCard";
import { randomSecret, type Saver, type Toaster } from "./shared";

type Line = "telnyx" | "sip" | "both" | "extension";

export function Lines({ cfg, setCfg, secrets, save, toast, loadCfg, loadReady }: {
  cfg: Record<string, any>; setCfg: (f: (c: Record<string, any>) => Record<string, any>) => void;
  secrets: Record<string, boolean>; save: Saver; toast: Toaster; loadCfg: () => void; loadReady: () => void;
}) {
  const line = (["telnyx", "sip", "both", "extension"].includes(cfg.telephony) ? cfg.telephony : "telnyx") as Line;
  const usesTelnyx = line === "telnyx" || line === "both";
  const webhook = cfg.public_url && cfg.webhook_secret
    ? `${String(cfg.public_url).replace(/\/$/, "")}/telephony/webhook/${cfg.webhook_secret}` : "";
  return (
    <div className="stack">
      <Card title="Phone line" description="How calls reach the agent. You can use Telnyx and a SIP trunk together.">
        <ChoiceCards<Line> label="Phone line" value={line} onChange={async (telephony) => {
          setCfg((c) => ({ ...c, telephony }));
          const r = await saveSip({ telephony });
          toast(r.ok ? "phone line saved" : r.error ?? "could not save", r.ok ? "success" : "error");
          loadReady();
        }} options={[
          { value: "telnyx", icon: Phone, title: "Telnyx Voice API", description: "A Telnyx number; needs a public HTTPS address." },
          { value: "sip", icon: Cable, title: "SIP trunk", description: "FreePBX, Telnyx SIP or another provider." },
          { value: "both", icon: Network, title: "Both", description: "Telnyx and a SIP trunk side by side." },
          { value: "extension", icon: PhoneForwarded, title: "PBX extension only", description: "Internal calls and testing." },
        ]} />
      </Card>

      {usesTelnyx && <>
        <Card title="Public address" description="Telnyx sends call events to this server's webhook.">
          <div className="form-grid">
            <TextField label="Public HTTPS address of this server" value={cfg.public_url ?? ""} placeholder="https://agent.example.com"
              inputMode="url" onCommit={(v) => save({ public_url: v.trim().replace(/\/$/, "") })} />
          </div>
          <div className="row wrap mt-3" style={{ gap: 8 }}>
            <Button icon={RefreshCw} onClick={() => save({ webhook_secret: randomSecret() })}>
              {cfg.webhook_secret ? "Regenerate webhook secret" : "Generate webhook secret"}</Button>
            {webhook && <>
              <code className="grow" style={{ overflowWrap: "anywhere" }}>{webhook}</code>
              <Button size="sm" icon={Copy} onClick={() => { navigator.clipboard?.writeText(webhook); toast("webhook URL copied", "success"); }}>Copy</Button>
            </>}
          </div>
        </Card>
        <CardBoundary name="Telnyx"><TelnyxCard cfg={cfg} keySet={!!secrets.TELNYX_API_KEY} save={save} toast={toast}
          onKeySaved={() => { loadCfg(); loadReady(); }} /></CardBoundary>
      </>}
      {(line === "sip" || line === "both") && <CardBoundary name="SIP trunk">
        <SipCard cfg={cfg} passwordSet={!!secrets.SIP_PASSWORD} toast={toast} onChange={() => { loadCfg(); loadReady(); }} /></CardBoundary>}
      <div data-tour="pbx-extension"><CardBoundary name="PBX extension">
        <SipExtensionCard cfg={cfg} passwordSet={!!secrets.SIP_EXTENSION_PASSWORD} toast={toast} onChange={() => { loadCfg(); loadReady(); }} />
      </CardBoundary></div>
    </div>
  );
}

function TelnyxCard({ cfg, keySet, save, toast, onKeySaved }: {
  cfg: Record<string, any>; keySet: boolean; save: Saver; toast: Toaster; onKeySaved: () => void;
}) {
  const [result, setResult] = useState<Record<string, any> | null>(null);
  const [busy, setBusy] = useState(false);
  const test = async () => {
    setBusy(true);
    try { setResult(await testTelnyx()); } catch (e) { setResult({ ok: false, error: String(e) }); } finally { setBusy(false); }
  };
  return (
    <Card title="Telnyx (Voice API)" actions={<Button loading={busy} onClick={test} disabled={!keySet}>Test Telnyx</Button>}>
      <div className="form-grid">
        <SecretField label="TELNYX_API_KEY" isSet={keySet} placeholder={keySet ? undefined : "KEY0123…"}
          onSave={async (v) => { await putSecrets({ TELNYX_API_KEY: v }); onKeySaved(); toast("Telnyx key saved", "success"); }} />
        <TextField label="Voice API application ID" value={cfg.connection_id ?? ""} placeholder="1234567890123456789"
          onCommit={(v) => save({ connection_id: v.trim() })} />
        <TextField label="Your Telnyx number" value={cfg.from_number ?? ""} placeholder="+15551234567" inputMode="tel"
          onCommit={(v) => save({ from_number: v.trim() })} />
        <TextField label="Webhook public key" className="span-all" value={cfg.telnyx_public_key ?? ""} placeholder="base64 public key"
          hint="Telnyx portal → Keys & Credentials → Public Key. With it, forged webhooks are rejected even if the URL leaks."
          onCommit={(v) => save({ telnyx_public_key: v.trim() })} />
      </div>
      {result && (result.ok
        ? <ResultLine ok>Key valid{result.application ? ` · application "${result.application}"` : ""}{result.webhook_matches ? " · webhook URL matches" : ""}</ResultLine>
        : <ResultLine ok={false}>{result.error}</ResultLine>)}
      {result?.warning && <Callout tone="warn">{result.warning} (Telnyx has: <code>{result.webhook_in_telnyx || "nothing"}</code>)</Callout>}
      <details className="mt-3">
        <summary className="dim">How to set this up in Telnyx (and with FreePBX)</summary>
        <ol className="dim small">
          <li>Telnyx portal → <b>Voice → Programmable Voice → Applications</b>: create an application (Call Control / Voice API).</li>
          <li>Set its <b>webhook URL</b> to the address shown above (needs a public <code>https://</code> address).</li>
          <li>Copy the application's <b>ID</b> into "Voice API application ID".</li>
          <li><b>Numbers</b>: assign your phone number to that application. Calls to it now reach the AI.</li>
          <li><b>Auth → API Keys</b>: create a key and paste it above, then press "Test Telnyx".</li>
          <li>FreePBX (optional): send calls to the AI with an inbound route / follow-me to the number above through your
            Telnyx SIP trunk, and put a FreePBX extension's DID in "Human hand-off number" (Setup) so "talk to a person"
            rings your team. To connect FreePBX directly instead, choose "SIP trunk" above.</li>
        </ol>
      </details>
    </Card>
  );
}
