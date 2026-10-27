import { useEffect, useState } from "react";
import { CallAgentInfo, CallRow, ReadinessItem, getCallAgent } from "../api";

type Health = "green" | "amber" | "red" | "off";
const find = (items: ReadinessItem[], id: string) => items.find((i) => i.id === id);
const health = (i?: ReadinessItem): Health => (!i ? "off" : i.ok ? "green" : i.required ? "red" : "amber");
const scrollTo = (id: string) => document.querySelector(`[data-tour="${id}"]`)?.scrollIntoView({ behavior: "smooth", block: "center" });

/** "Is my agent live?" — the go-live checklist and one health row, from real state. */
export function GoLive({ ready, items, calls, cfg }: {
  ready: boolean; items: ReadinessItem[]; calls: CallRow[]; cfg: Record<string, any>;
}) {
  const [agent, setAgent] = useState<CallAgentInfo | null>(null);
  useEffect(() => { getCallAgent().then(setAgent).catch(() => {}); }, [calls.length]);

  const mode = cfg.telephony ?? "telnyx";
  const telnyxIds = ["public_url", "webhook_secret", "telnyx_key", "connection_id"];
  const telnyxOk = telnyxIds.every((id) => find(items, id)?.ok ?? true) && telnyxIds.some((id) => find(items, id));
  const sipOk = !!find(items, "sip_registered")?.ok;
  const lineOk = mode === "sip" ? sipOk : mode === "both" ? sipOk || telnyxOk : telnyxOk;
  const extItem = find(items, "sip_extension_registered");
  const tested = calls.some((c) => !!c.test && !c.demo);
  const crmSent = !!agent?.crm.deliveries.some((d) => d.status === "sent");
  const crmOn = !!(agent?.crm.enabled && agent.crm.base_url && agent.crm.token_set);
  const live = ready && !!cfg.enabled;

  const steps = [
    { done: lineOk, title: "Connect a phone line", hint: "Telnyx or a SIP trunk: where your calls come in.", go: "setup" },
    { done: !!extItem?.ok, title: "Add the FreePBX extension (optional)",
      hint: "A phone number inside your office phone system that the AI answers: colleagues can ring it and busy queues can overflow to it.",
      go: "pbx-extension" },
    { done: tested, title: "Make a test call", hint: "Talk to your agent in the browser, or press Call me.", go: "test-call" },
    { done: crmSent, title: "Connect your CRM", hint: crmOn ? "Connected: make a call to send the first summary." : "So every call lands on the right customer record.", go: "crm" },
    { done: live, title: "Go live", hint: live ? "Your agent is answering calls." : "Switch the phone agent on once everything above is green.", go: "setup" },
  ];
  const doneCount = steps.filter((s) => s.done).length;
  const crmHealth: Health = !agent?.crm.enabled ? "off" : agent.crm.deliveries[0]?.status === "sent" ? "green"
    : agent.crm.deliveries[0] ? "red" : "amber";
  const pills: { label: string; state: Health; title: string }[] = [
    ...(mode !== "sip" ? [{ label: "Telnyx", state: (telnyxOk ? "green" : "red") as Health, title: telnyxOk ? "ready" : "finish the Telnyx card" }] : []),
    ...(mode !== "telnyx" ? [{ label: "SIP trunk", state: health(find(items, "sip_registered")), title: find(items, "sip_registered")?.detail ?? "" }] : []),
    ...(extItem ? [{ label: "PBX extension", state: health(extItem), title: extItem.detail }] : []),
    { label: "Voices", state: health(find(items, "speech")), title: find(items, "speech")?.detail ?? "" },
    { label: "CRM", state: crmHealth, title: agent?.crm.deliveries[0] ? `last delivery: ${agent.crm.deliveries[0].status}` : "no deliveries yet" },
    ...(find(items, "model_speed") ? [{ label: "Model speed", state: health(find(items, "model_speed")), title: find(items, "model_speed")!.detail }] : []),
    ...(find(items, "company") && !find(items, "company")!.ok ? [{ label: "Company name", state: "amber" as Health, title: find(items, "company")!.detail }] : []),
    ...["sip_capacity", "sip_extension_capacity"].filter((id) => find(items, id)).map((id) => ({
      label: id === "sip_capacity" ? "Trunk capacity" : "Extension capacity", state: health(find(items, id)), title: find(items, id)!.detail })),
  ];
  return (
    <div className="card" data-tour="golive">
      <h3>{live ? "✓ Your agent is live" : "Go live"} <span className="dim" style={{ fontSize: 13, fontWeight: 400 }}>{doneCount} of {steps.length} done</span></h3>
      <div className="golive-progress"><div style={{ width: `${(doneCount / steps.length) * 100}%` }} /></div>
      <ol className="golive-steps">
        {steps.map((s, n) => (
          <li key={s.title} className={s.done ? "done" : ""}>
            <span className="mark">{s.done ? "✓" : n + 1}</span>
            <span className="what"><b>{s.title}</b><small>{s.hint}</small></span>
            {!s.done && <button className="btn sm" onClick={() => scrollTo(s.go)}>Open</button>}
          </li>
        ))}
      </ol>
      <div className="health-row" aria-label="health">
        {pills.map((p) => <span key={p.label} className={`pill ${p.state}`} title={p.title}><span className="dot" />{p.label}</span>)}
      </div>
      {/* Warnings that don't block going live but will hurt a demo: said in words, not only on hover. */}
      {["model_speed", "company"].map((id) => find(items, id)).filter((i) => i && !i.ok).map((i) => (
        <p key={i!.id} className="warn" style={{ fontSize: 12, margin: "6px 0 0" }}>⚠ {i!.label}: {i!.detail}</p>))}
    </div>
  );
}
