// Calls: the phone agent. Sub-tabs (in the URL, e.g. #/calls/speech):
// Overview · Test call · Setup · Speech · Phone lines · CRM
import { useCallback, useEffect, useState } from "react";
import { AudioLines, FlaskConical, LayoutDashboard, Link2, PhoneCall, RefreshCw, Router, SlidersHorizontal } from "lucide-react";
import {
  CallRow, CallTurn, ReadinessItem, VoipStatus, getCall, getCalls, getContacts, getFullConfig, getVoipReadiness,
  getVoipStatus, patchConfig,
} from "../api";
import { useToast } from "../components/Toast";
import { CardBoundary } from "../components/CardBoundary";
import { useSubRoute } from "../lib/router";
import { Badge, IconButton, PageHeader, Tabs } from "../ui";
import { TestCall } from "./CallControls";
import { CallAgentCard } from "./CallAgentCard";
import { Overview } from "./calls/Overview";
import { Setup } from "./calls/Setup";
import { SpeechSection } from "./calls/Speech";
import { Lines } from "./calls/Lines";
import { visibleInterval } from "./calls/shared";

const SUBS = ["overview", "test", "setup", "speech", "lines", "crm"] as const;
type Sub = typeof SUBS[number];

export function Calls() {
  const toast = useToast();
  const [sub, setSub] = useSubRoute<Sub>("calls", SUBS, "overview");
  const [status, setStatus] = useState<VoipStatus | null>(null);
  const [calls, setCalls] = useState<CallRow[]>([]);
  const [open, setOpen] = useState<number | null>(null);
  const [turns, setTurns] = useState<CallTurn[]>([]);
  const [cfg, setCfg] = useState<Record<string, any>>({});
  const [loaded, setLoaded] = useState(false);
  const [ready, setReady] = useState<{ ready: boolean; items: ReadinessItem[] } | null>(null);
  const [secrets, setSecrets] = useState<Record<string, boolean>>({});
  const [presets, setPresets] = useState<{ label: string; model: string }[]>([]);
  const [mainModel, setMainModel] = useState("");
  const [contacts, setContacts] = useState<{ id: number; name: string }[]>([]);

  const loadCfg = useCallback(() => getFullConfig().then((c) => {
    setCfg(c.voip ?? {}); setPresets(c.models ?? []); setMainModel(c.model ?? ""); setSecrets(c.secrets ?? {}); setLoaded(true);
  }).catch(() => setLoaded(true)), []);
  const loadReady = useCallback(() => getVoipReadiness().then(setReady).catch(() => {}), []);
  const reload = useCallback(() => {
    getVoipStatus().then(setStatus).catch(() => {});
    getCalls().then((r) => setCalls(r.calls)).catch(() => {});
    if (open !== null) getCall(open).then((r) => setTurns(r.call?.turns ?? [])).catch(() => {});
  }, [open]);

  useEffect(() => { loadCfg(); loadReady(); }, [loadCfg, loadReady]);
  useEffect(() => { if (sub === "test") getContacts("").then((r) => setContacts(r.contacts.slice(0, 200))).catch(() => {}); }, [sub]);
  // Live transcript and status while Overview is on screen (paused when the tab is hidden).
  useEffect(() => {
    reload();
    if (sub !== "overview") return;
    return visibleInterval(reload, 3000);
  }, [reload, sub]);

  const save = useCallback(async (updates: Record<string, any>) => {
    setCfg((c) => ({ ...c, ...updates }));
    await patchConfig({ voip: updates });
    toast("saved", "success");
    reload(); loadReady();
  }, [toast, reload, loadReady]);

  const live = status?.active.length ?? 0;
  const failing = ready ? ready.items.filter((i) => !i.ok && i.required).length : 0;
  const tabs = [
    { id: "overview" as Sub, label: "Overview", icon: LayoutDashboard, badge: live ? <Badge tone="warn">{live} live</Badge> : undefined },
    { id: "test" as Sub, label: "Test call", icon: FlaskConical },
    { id: "setup" as Sub, label: "Setup", icon: SlidersHorizontal, badge: failing ? <Badge tone="bad">{failing}</Badge> : undefined },
    { id: "speech" as Sub, label: "Speech", icon: AudioLines },
    { id: "lines" as Sub, label: "Phone lines", icon: Router },
    { id: "crm" as Sub, label: "CRM", icon: Link2 },
  ];

  return (
    <div className="pane">
      <div className="pane-wide">
        <PageHeader icon={PhoneCall} title="Calls"
          description="Answers and places calls using your scripts and knowledge base, transcribes them live and hands off to a person when it can't help."
          actions={<>
            <Badge tone={status?.enabled ? "ok" : "bad"}>{status?.enabled ? "Phone agent on" : "Phone agent off"}</Badge>
            <IconButton icon={RefreshCw} label="Refresh" onClick={() => { reload(); loadReady(); loadCfg(); }} />
          </>} />
        <Tabs<Sub> label="Calls sections" value={sub} onChange={setSub} tabs={tabs} />
        {sub === "overview" && <Overview cfg={cfg} status={status} calls={calls} turns={turns} open={open}
          setOpen={(id) => { setOpen(id); setTurns([]); }} ready={ready} loaded={loaded} save={save} toast={toast}
          reload={reload} loadReady={loadReady} goTest={() => setSub("test")} />}
        {sub === "test" && <div data-tour="test-call"><CardBoundary name="Test call in the browser"><TestCall contacts={contacts} /></CardBoundary></div>}
        {sub === "setup" && loaded && <Setup cfg={cfg} status={status} mainModel={mainModel} presets={presets} secrets={secrets} save={save} toast={toast} />}
        {sub === "speech" && loaded && <CardBoundary name="Speech"><SpeechSection cfg={cfg} secrets={secrets} save={save} toast={toast} onKey={loadCfg} /></CardBoundary>}
        {sub === "lines" && loaded && <Lines cfg={cfg} setCfg={setCfg} secrets={secrets} save={save} toast={toast} loadCfg={loadCfg} loadReady={loadReady} />}
        {sub === "crm" && <div data-tour="crm"><CardBoundary name="Call agent"><CallAgentCard toast={toast} onChange={loadReady} /></CardBoundary></div>}
        {!loaded && sub !== "overview" && sub !== "test" && sub !== "crm" && <div className="card"><div className="skeleton" style={{ height: 120 }} /></div>}
      </div>
    </div>
  );
}
