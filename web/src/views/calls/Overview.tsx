import { Fragment, useEffect, useState } from "react";
import { ChevronDown, ChevronRight, Download, GraduationCap, PhoneCall, PhoneIncoming, PhoneOutgoing, Play, Trash2 } from "lucide-react";
import {
  CallRow, CallTurn, ReadinessItem, VoipStatus, clearSipBusy, deleteRecording, getRecordingUrl, resendCrm, setDemo,
  trainOnRecording,
} from "../../api";
import { CardBoundary } from "../../components/CardBoundary";
import { Badge, Button, Card, EmptyState, Switch } from "../../ui";
import { useConfirm } from "../../ui/confirm";
import { CallNow, LiveCalls } from "../CallControls";
import { CallDebug } from "../CallDebug";
import { GoLive } from "../GoLive";
import { CallbacksCard } from "../CallbacksCard";
import { fmtDur, fmtTime, type Saver, type Toaster } from "./shared";
import { Transcript } from "./Transcript";

export const CRM_TONE: Record<string, "ok" | "warn" | "bad"> = { sent: "ok", failed: "warn", skipped: "bad", dead: "bad", retrying: "warn",
  sending: "warn", pending: "warn" };

export function Overview({ cfg, status, calls, turns, open, setOpen, ready, loaded, save, toast, reload, loadReady, goTest }: {
  cfg: Record<string, any>; status: VoipStatus | null; calls: CallRow[]; turns: CallTurn[]; open: number | null;
  setOpen: (id: number | null) => void; ready: { ready: boolean; items: ReadinessItem[] } | null; loaded: boolean;
  save: Saver; toast: Toaster; reload: () => void; loadReady: () => void; goTest: () => void;
}) {
  const real = calls.filter((c) => !c.demo).length;
  const demo = calls.length - real;
  return (
    <div className="stack">
      {ready && loaded && <CardBoundary name="Go live"><GoLive ready={ready.ready} items={ready.items} calls={calls} cfg={cfg}
        onSwitch={(on) => save({ enabled: on })} /></CardBoundary>}
      {ready && (
        <details className="card" open={ready.items.some((i) => !i.ok && i.required)}>
          <summary><b>All checks</b> <span className="dim">({ready.items.filter((i) => i.ok).length} of {ready.items.length} fine)</span></summary>
          <Readiness items={ready.items} onClearBusy={async (line) => { await clearSipBusy(line); loadReady(); }} />
        </details>
      )}
      <CardBoundary name="Live calls"><LiveCalls active={status?.active ?? []} toast={toast} onChange={reload} /></CardBoundary>
      <CardBoundary name="Callbacks"><CallbacksCard toast={toast} /></CardBoundary>
      {loaded && <CardBoundary name="Call now"><CallNow toast={toast} onCalled={reload}
        via={cfg.telephony === "extension" ? ["extension"] : [...(cfg.telephony === "sip" ? ["sip"] : cfg.telephony === "both" ? ["telnyx", "sip"] : ["telnyx"]),
          ...(cfg.sip_extension?.enabled && cfg.sip_extension?.server ? ["extension"] : [])]} /></CardBoundary>}

      <Card data-tour="calls-list" title={<>Recent calls <Badge>{real}</Badge>{demo > 0 && <Badge tone="demo">+{demo} demo</Badge>}</>}
        actions={<Switch checked={demo > 0} label="Demo data" onChange={async (on) => {
          await setDemo(on); toast(on ? "demo data added" : "demo data cleared", "success"); reload();
        }} />}>
        {calls.length === 0 ? (
          <EmptyState icon={PhoneCall} title="No calls yet"
            actions={<>
              <Button variant="primary" onClick={goTest}>Make a test call</Button>
              <Button onClick={async () => { await setDemo(true); reload(); }}>Show demo data</Button>
            </>}>Try your agent first in the browser: it takes a minute.</EmptyState>
        ) : (
          <div className="table-scroll">
            <table className="data-table">
              <thead><tr><th aria-label="Direction" /><th>When</th><th>Caller</th><th>Duration</th><th>Outcome</th><th>Est. cost</th></tr></thead>
              <tbody>
                {calls.map((c) => (
                  <Fragment key={c.id}>
                    <tr className="clickable" onClick={() => setOpen(open === c.id ? null : c.id)}>
                      <td>
                        <button type="button" className="linkish" aria-expanded={open === c.id}
                          aria-label={`${open === c.id ? "Hide" : "Show"} the call with ${c.contact_name || c.from_number || c.to_number || "unknown"}`}
                          onClick={(e) => { e.stopPropagation(); setOpen(open === c.id ? null : c.id); }}>
                          {open === c.id ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                          {c.direction === "inbound" ? <PhoneIncoming size={14} aria-label="inbound" /> : <PhoneOutgoing size={14} aria-label="outbound" />}
                        </button>
                      </td>
                      <td className="dim" data-label="When">{fmtTime(c.started_at)}</td>
                      <td data-label="Caller">
                        <span className="row wrap" style={{ gap: 6 }}>
                          {c.contact_name || (c.direction === "outbound" ? c.to_number : c.from_number) || "unknown"}
                          {!!c.demo && <Badge tone="demo">demo</Badge>}
                          {!!c.test && <Badge>test</Badge>}
                          {!!c.via_queue && <Badge tone="warn" title={`came through the office queue${c.queue_wait_s ? ` after ${c.queue_wait_s}s` : ""}${c.original_called_number ? `; dialled ${c.original_called_number}` : ""}`}>via queue</Badge>}
                        </span>
                      </td>
                      <td data-label="Duration">{fmtDur(c)}{c.status === "active" && <> <Badge tone="warn">live</Badge></>}</td>
                      <td data-label="Outcome">
                        <span className="row wrap" style={{ gap: 6 }}>
                          {c.outcome ? <Badge>{c.outcome}</Badge> : <span className="dim">—</span>}
                          {c.crm_status && <Badge tone={CRM_TONE[c.crm_status]} title={c.crm_detail || ""}>CRM {c.crm_status}</Badge>}
                        </span>
                      </td>
                      <td className="dim" data-label="Est. cost">${(c.cost ?? 0).toFixed(3)}</td>
                    </tr>
                    {open === c.id && (
                      <tr><td colSpan={6}><CallDetail call={c} turns={turns} toast={toast} reload={reload} /></td></tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}

export function CallDetail({ call: c, turns, toast, reload }: {
  call: Pick<CallRow, "id" | "summary" | "crm_status" | "crm_detail"> & { recording?: string | boolean };
  turns: CallTurn[]; toast: Toaster; reload: () => void;
}) {
  const [debug, setDebug] = useState(false);
  return (
    <div className="call-detail">
      {c.summary && <p className="mb-0"><b>Summary:</b> <span className="dim">{c.summary}</span></p>}
      {c.recording && <Recording callId={c.id} toast={toast} onDeleted={reload} />}
      {(c.crm_status === "failed" || c.crm_status === "skipped" || c.crm_status === "dead") && (
        <div className="row wrap">
          <span className="warn small">CRM: {c.crm_status === "dead" ? "gave up" : c.crm_status}{c.crm_detail ? ` (${c.crm_detail})` : ""}
            {c.crm_status === "failed" ? " · retried automatically (1, 5, 15 min, then hourly)" : ""}</span>
          <Button size="sm" onClick={async () => {
            const r = await resendCrm(c.id);
            toast(r.ok ? "sent to the CRM" : r.error ?? r.detail ?? "still failing", r.ok ? "success" : "error");
            reload();
          }}>Resend</Button>
        </div>
      )}
      <div><button type="button" className="linkx" onClick={() => setDebug(!debug)}>{debug ? "Hide debug" : "Debug this call"}</button></div>
      {debug && <CallDebug callId={c.id} toast={toast} />}
      <Transcript turns={turns} empty="No transcript yet." />
    </div>
  );
}

function Recording({ callId, toast, onDeleted }: { callId: number; toast: Toaster; onDeleted: () => void }) {
  const [url, setUrl] = useState("");
  const confirm = useConfirm();
  useEffect(() => () => { if (url) URL.revokeObjectURL(url); }, [url]);
  const load = async () => {
    try { setUrl(await getRecordingUrl(callId)); } catch { toast("recording not found", "error"); }
  };
  return (
    <div className="row wrap" style={{ gap: 8 }}>
      {url ? <audio controls autoPlay src={url} style={{ height: 36, maxWidth: "100%" }} />
        : <Button size="sm" icon={Play} onClick={load}>Play recording</Button>}
      {url && <a className="btn sm" href={url} download={`call-${callId}.wav`}><Download size={14} aria-hidden /> Download</a>}
      <Button size="sm" icon={GraduationCap} title="Transcribe it and add it to Call training (caller left, agent right)"
        onClick={async () => { const r = await trainOnRecording(callId); toast(r.ok ? "added to Call training" : r.error ?? "failed", r.ok ? "success" : "error"); }}>
        Use for training</Button>
      <Button size="sm" variant="ghost" icon={Trash2} onClick={async () => {
        if (!(await confirm({ title: "Delete this recording?", body: "The transcript stays.", confirmLabel: "Delete", danger: true }))) return;
        await deleteRecording(callId); setUrl(""); onDeleted();
      }}>Delete</Button>
      <span className="dim xsmall">left: caller · right: agent</span>
    </div>
  );
}

function Readiness({ items, onClearBusy }: { items: ReadinessItem[]; onClearBusy: (line: string) => void }) {
  return (
    <ul className="list">
      {items.map((i) => (
        <li key={i.id}>
          <Badge tone={i.ok ? "ok" : i.required ? "bad" : "warn"}>{i.ok ? "OK" : i.required ? "Needed" : "Optional"}</Badge>
          <b>{i.label}</b>{i.detail && <span className="dim">{i.detail}</span>}
          {i.id.endsWith("_capacity") && i.detail.includes("got busy") && (
            <button type="button" className="linkx" onClick={() => onClearBusy(i.id === "sip_capacity" ? "trunk" : "extension")}>clear</button>)}
        </li>
      ))}
    </ul>
  );
}
