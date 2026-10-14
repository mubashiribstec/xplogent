// Thin client for the Xplogent backend: REST helpers + a typed WebSocket wrapper.

// ── Access token (enterprise security) ────────────────────────────────────────
const TOKEN_KEY = "xplogent_token";
const _origFetch = window.fetch.bind(window);

// Storage may be blocked by browser privacy settings: degrade to "no saved token".
export function getToken(): string { try { return localStorage.getItem(TOKEN_KEY) || ""; } catch { return ""; } }
export function setToken(t: string) {
  try { t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY); } catch { /* not persisted */ }
}
export function wsTokenParam(): string { const t = getToken(); return t ? `&token=${encodeURIComponent(t)}` : ""; }

// Fired when the server rejects our token (rotated, or auth newly enabled): the app
// listens for it and shows the login screen instead of failing every call silently.
export const UNAUTHORIZED_EVENT = "xplogent:unauthorized";

// Inject the bearer token into every same-origin request so existing call sites
// don't each need to know about auth.
window.fetch = ((input: RequestInfo | URL, init: RequestInit = {}) => {
  const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
  const sameOrigin = url.startsWith("/") || url.startsWith(location.origin);
  const tok = getToken();
  if (sameOrigin && tok) {
    const headers = new Headers(init.headers);
    if (!headers.has("Authorization")) headers.set("Authorization", `Bearer ${tok}`);
    init = { ...init, headers };
  }
  return _origFetch(input as RequestInfo, init).then((res) => {
    if (sameOrigin && res.status === 401 && !url.includes("/auth/check")) {
      window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
    }
    return res;
  });
}) as typeof fetch;

// A malformed frame must not throw inside a socket handler (which kills the view).
function parseFrame(data: unknown): XplogentEvent | null {
  try {
    const ev = JSON.parse(String(data));
    return ev && typeof ev === "object" && typeof ev.type === "string" ? (ev as XplogentEvent) : null;
  } catch {
    return null;
  }
}

function wsUrl(path: string): string {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}${path}`;
}

// Validate a token (or the absence of auth) without going through the wrapper,
// so we can test a freshly typed token before storing it. Defaults to the stored
// token: checking with none made every reload of a token-protected server show the
// login screen again, even right after a successful login.
export async function checkAuth(token: string = getToken()): Promise<{ required: boolean; ok: boolean }> {
  const headers: Record<string, string> = {};
  if (token) headers["Authorization"] = `Bearer ${token}`;
  try { return await (await _origFetch("/auth/check", { headers })).json(); }
  catch { return { required: false, ok: true }; }
}

export interface XplogentEvent {
  type: string;
  [key: string]: unknown;
}

export interface ApprovalRequest {
  id: string;
  tool: string;
  risk: string;
  reason: string;
  arguments: Record<string, unknown>;
}

export async function getSkills(): Promise<{ skills: { name: string; description: string; uses: number }[] }> {
  const r = await fetch("/skills");
  return r.json();
}

export interface TaskOptions {
  model?: string;
  effort?: string;
  thinking?: boolean;
  temperature?: number;
}

export type ConnStatus = "connecting" | "online" | "offline";

export class XplogentSocket {
  private ws!: WebSocket;
  private closed = false;
  private retries = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private outbox: string[] = [];   // sent before the socket opened; flushed on open

  constructor(
    private onEvent: (ev: XplogentEvent) => void,
    private sessionId?: number | null,
    private onStatus?: (s: ConnStatus) => void,
  ) {
    this.connect();
  }

  private connect() {
    this.timer = null;
    if (this.closed) return;
    const qs = this.sessionId ? `?session_id=${this.sessionId}` : "?";
    this.onStatus?.("connecting");
    const ws = new WebSocket(wsUrl(`/ws${qs}${wsTokenParam()}`));
    this.ws = ws;
    ws.onopen = () => {
      this.retries = 0;
      this.onStatus?.("online");
      for (const m of this.outbox.splice(0)) ws.send(m);
    };
    ws.onmessage = (m) => { const ev = parseFrame(m.data); if (ev) this.onEvent(ev); };
    ws.onclose = () => {
      if (this.closed || ws !== this.ws) return;
      this.onStatus?.("offline");
      const delay = Math.min(1000 * 2 ** this.retries++, 10000);
      this.timer = setTimeout(() => this.connect(), delay);
    };
    ws.onerror = () => ws.close();
  }

  private send(obj: unknown) {
    const msg = JSON.stringify(obj);
    if (this.ws.readyState === WebSocket.OPEN) this.ws.send(msg);
    else if (!this.closed && this.outbox.length < 20) this.outbox.push(msg);
  }

  sendTask(task: string, opts: TaskOptions & { images?: string[] } = {}) {
    this.send({ type: "task", task, ...opts });
  }

  sendCouncil(task: string, models: string[], synthModel?: string) {
    this.send({ type: "task", task, models, synthesize: true, synth_model: synthModel });
  }

  cancel() { this.send({ type: "cancel" }); }
  sendSteer(text: string) { this.send({ type: "steer", text }); }
  resolveApproval(id: string, allowed: boolean) { this.send({ type: "approval", id, allowed }); }

  close() {
    // No reconnect after an explicit close (a pending retry timer would revive it).
    this.closed = true;
    if (this.timer) clearTimeout(this.timer);
    this.outbox = [];
    this.ws.close();
  }
}

// Computer-use operator: drives the screen in a screenshot→analyze→act loop.
export class OperatorSocket {
  private ws: WebSocket;
  private outbox: string[] = [];
  private closed = false;
  constructor(onEvent: (ev: XplogentEvent) => void, onClose?: () => void) {
    this.ws = new WebSocket(wsUrl(`/ws/operator?${wsTokenParam()}`));
    this.ws.onopen = () => { for (const m of this.outbox.splice(0)) this.ws.send(m); };
    this.ws.onmessage = (m) => { const ev = parseFrame(m.data); if (ev) onEvent(ev); };
    this.ws.onclose = () => { if (!this.closed) onClose?.(); };
  }
  private send(o: unknown) {
    const msg = JSON.stringify(o);
    if (this.ws.readyState === WebSocket.OPEN) this.ws.send(msg);
    else if (this.ws.readyState === WebSocket.CONNECTING) this.outbox.push(msg);
  }
  start(goal: string, maxSteps = 30) { this.send({ type: "start", goal, max_steps: maxSteps }); }
  cancel() { this.send({ type: "cancel" }); }
  resolveApproval(id: string, allowed: boolean) { this.send({ type: "approval", id, allowed }); }
  close() { this.closed = true; this.ws.close(); }
}

// ── Multi-agent orchestration + monitoring ────────────────────────────────────
export interface OrchestrateOptions {
  goal?: string;
  specs?: { name: string; role: string; task: string }[];
  max_concurrent?: number;
  mode?: string;
  auto_approve?: boolean;
}

export async function orchestrate(opts: OrchestrateOptions): Promise<{ run_id: string }> {
  const r = await fetch("/orchestrate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(opts),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok || !data.run_id) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}

export async function controlAgent(agentId: string, action: "pause" | "resume" | "cancel") {
  await fetch(`/agents/${agentId}/${action}`, { method: "POST" });
}

// ── Settings / config ─────────────────────────────────────────────────────────
export async function getFullConfig(): Promise<Record<string, any>> {
  return (await fetch("/config/full")).json();
}

export async function patchConfig(updates: Record<string, any>) {
  await fetch("/config", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ updates }),
  });
}

export async function putSecrets(keys: Record<string, string>) {
  await fetch("/secrets", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ keys }),
  });
}

export async function getTools(): Promise<{ tools: any[]; groups: string[] }> {
  return (await fetch("/tools")).json();
}

export async function getRoles(): Promise<{ roles: Record<string, any> }> {
  return (await fetch("/roles")).json();
}

export async function putRole(name: string, role: Record<string, any>) {
  await fetch(`/roles/${name}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(role),
  });
}

export async function getFacts(): Promise<{ facts: { id: number; content: string }[] }> {
  return (await fetch("/memory/facts")).json();
}

export async function addFact(content: string) {
  await fetch("/memory/facts", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content }),
  });
}

export async function deleteFact(id: number) {
  await fetch(`/memory/facts/${id}`, { method: "DELETE" });
}

export async function deleteSkill(name: string) {
  await fetch(`/skills/${name}`, { method: "DELETE" });
}

// ── Persona (SOUL.md) + curated memory (MEMORY.md) ────────────────────────────
export async function getSoul(): Promise<{ content: string }> {
  return (await fetch("/persona/soul")).json();
}
export async function putSoul(content: string) {
  await fetch("/persona/soul", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ content }) });
}
export async function getMemoryMd(): Promise<{ content: string }> {
  return (await fetch("/persona/memory")).json();
}
export async function putMemoryMd(content: string) {
  await fetch("/persona/memory", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ content }) });
}
export async function compactMemory(): Promise<{ ok: boolean; content?: string }> {
  return (await fetch("/memory/compact", { method: "POST" })).json();
}
export async function consolidateMemory(): Promise<{ ok: boolean; skills_saved: number; memory: string }> {
  return (await fetch("/memory/consolidate", { method: "POST" })).json();
}

// ── Skill activity history ────────────────────────────────────────────────────
export interface SkillEvent { name: string; action: string; level: string; stars: number; detail: string; created_at: number; }
export async function getSkillHistory(limit = 50): Promise<{ events: SkillEvent[] }> {
  return (await fetch(`/skills/history?limit=${limit}`)).json();
}

// ── Documents / RAG ───────────────────────────────────────────────────────────
export interface DocInfo { id: number; source: string; title: string; chunks: number; created_at: number; }
export async function getDocs(): Promise<{ documents: DocInfo[] }> {
  return (await fetch("/docs")).json();
}
export async function ingestDocs(body: { path?: string; content?: string; title?: string }): Promise<any> {
  return (await fetch("/docs/ingest", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).json();
}
export async function searchDocs(q: string): Promise<{ hits: any[] }> {
  return (await fetch(`/docs/search?q=${encodeURIComponent(q)}`)).json();
}
export async function deleteDoc(id: number) {
  await fetch(`/docs/${id}`, { method: "DELETE" });
}

// ── Analytics ─────────────────────────────────────────────────────────────────
export interface UsageBucket {
  input_tokens: number; output_tokens: number; cost: number; turns: number;
  day?: string; model?: string;
}
export interface Analytics {
  totals: UsageBucket;
  by_day: UsageBucket[];
  by_model: UsageBucket[];
}
export async function getAnalytics(days = 30): Promise<Analytics> {
  return (await fetch(`/analytics?days=${days}`)).json();
}

// ── Budget / cost guardrails ──────────────────────────────────────────────────
export interface Budget { daily_usd?: number; session_usd?: number; on_exceed?: string; downgrade_model?: string; }
export async function getBudget(): Promise<{ budget: Budget; today_spend: number }> {
  return (await fetch("/budget")).json();
}
export async function setBudget(b: Budget): Promise<{ ok: boolean; budget: Budget }> {
  return (await fetch("/budget", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b) })).json();
}

// ── Evals ─────────────────────────────────────────────────────────────────────
export interface EvalCase { id?: number; prompt: string; criteria: string; }
export interface EvalRun { passed: number; total: number; score: number; model: string; created_at: number; }
export interface EvalSuite {
  id: number; name: string; description: string;
  cases: EvalCase[]; runs: EvalRun[];
}
export async function getEvals(): Promise<{ evals: EvalSuite[] }> {
  return (await fetch("/evals")).json();
}
export async function saveEval(body: { id?: number; name: string; description?: string; cases: EvalCase[] }): Promise<any> {
  return (await fetch("/evals", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).json();
}
export async function runEval(id: number): Promise<any> {
  return (await fetch(`/evals/${id}/run`, { method: "POST" })).json();
}
export async function deleteEval(id: number) {
  await fetch(`/evals/${id}`, { method: "DELETE" });
}
export interface AbVariant { name: string; system_prompt?: string; model?: string; }
export interface AbResult { name: string; system_prompt: string; model: string; passed: number; total: number; score: number; }
export async function runEvalAB(id: number, variants: AbVariant[]): Promise<{ ok: boolean; error?: string; variants: AbResult[]; winner: string; winner_detail: AbResult }> {
  return (await fetch(`/evals/${id}/ab`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ variants }) })).json();
}
export async function promoteEval(body: { system_prompt?: string; model?: string }): Promise<{ ok: boolean; promoted: string[] }> {
  return (await fetch("/evals/promote", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).json();
}

// ── Chrome extension (real-browser control) ───────────────────────────────────
export interface BrowserTab { id: number; title: string; url: string; active: boolean; }
export interface InputActivity { field: string; type: string; page: string; url: string; redacted?: boolean; ts?: number; }
export interface ExtensionStatus { connected: boolean; stale?: boolean; tabs: BrowserTab[]; inputs: InputActivity[]; last_seen: number; }
export async function getExtensionStatus(): Promise<ExtensionStatus> {
  return (await fetch("/extension/status")).json();
}

// ── Phone calling agent (VoIP) ────────────────────────────────────────────────
export interface CallRow {
  id: number; direction: string; contact_name?: string | null; from_number: string; to_number: string;
  status: string; outcome?: string | null; summary?: string | null; cost: number;
  started_at: number; ended_at?: number | null;
}
export interface CallTurn { speaker: string; text: string; created_at: number; }
export interface Contact {
  id: number; name: string; phone: string; email: string; timezone: string; status: string;
  notes: string; fields: Record<string, string>;
}
export interface VoipStatus {
  enabled: boolean; public_url: string; has_secret: boolean; contacts: number; calls: number;
  active: { call_id: number; from: string; started: number }[];
}
export async function getVoipStatus(): Promise<VoipStatus> { return (await fetch("/voip/status")).json(); }
export async function getCalls(): Promise<{ calls: CallRow[] }> { return (await fetch("/voip/calls")).json(); }
export async function getCall(id: number): Promise<{ call: (CallRow & { turns: CallTurn[] }) | null }> {
  return (await fetch(`/voip/calls/${id}`)).json();
}
export async function getContacts(q = ""): Promise<{ contacts: Contact[]; total: number }> {
  return (await fetch(`/voip/contacts?q=${encodeURIComponent(q)}`)).json();
}
export async function importContactsCsv(csv: string): Promise<{ ok: boolean; imported: number; skipped: number; dnc?: number; error?: string; errors?: string[] }> {
  return (await fetch("/voip/contacts/import", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ csv }) })).json();
}
export async function deleteContact(id: number) { await fetch(`/voip/contacts/${id}`, { method: "DELETE" }); }
export async function addDnc(phone: string) {
  await fetch("/voip/dnc", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ phone }) });
}

// ── Knowledge graph ───────────────────────────────────────────────────────────
export interface GraphNode { name: string; type: string; mentions: number; }
export interface GraphEdge { subject: string; relation: string; object: string; }
export async function getGraph(limit = 300): Promise<{ nodes: GraphNode[]; edges: GraphEdge[] }> {
  return (await fetch(`/graph?limit=${limit}`)).json();
}

// ── Visual workflows ──────────────────────────────────────────────────────────
export interface WfNode { id: string; type: "input" | "agent" | "tool"; name: string; x: number; y: number; config: Record<string, any>; }
export interface WfEdge { from: string; to: string; }
export interface WfGraph { nodes: WfNode[]; edges: WfEdge[]; }
export interface Workflow { id: number; name: string; graph: WfGraph; updated_at: number; }
export interface WfResult { node_id: string; name: string; status: string; output: string; }

export async function getWorkflows(): Promise<{ workflows: Workflow[] }> {
  return (await fetch("/workflows")).json();
}
export async function saveWorkflow(body: { id?: number; name: string; graph: WfGraph }): Promise<{ ok: boolean; workflow: Workflow }> {
  return (await fetch("/workflows", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).json();
}
export async function runWorkflow(id: number): Promise<{ ok: boolean; error?: string; results: WfResult[] }> {
  return (await fetch(`/workflows/${id}/run`, { method: "POST" })).json();
}
export async function deleteWorkflow(id: number) {
  await fetch(`/workflows/${id}`, { method: "DELETE" });
}

// ── Proactive triggers ────────────────────────────────────────────────────────
export interface Trigger {
  id: number; name: string; type: "webhook" | "file"; spec: string;
  prompt: string; mode: string; enabled: number; last_fired: number | null; last_status: string | null;
}
export async function getTriggers(): Promise<{ triggers: Trigger[] }> {
  return (await fetch("/triggers")).json();
}
export async function createTrigger(body: { name: string; type: string; prompt: string; spec?: string; mode?: string }): Promise<{ ok: boolean; trigger: Trigger }> {
  return (await fetch("/triggers", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).json();
}
export async function toggleTrigger(id: number) { await fetch(`/triggers/${id}/toggle`, { method: "POST" }); }
export async function deleteTrigger(id: number) { await fetch(`/triggers/${id}`, { method: "DELETE" }); }

// ── Audit log ─────────────────────────────────────────────────────────────────
export interface AuditEntry {
  actor: string; action: string; target: string; risk: string;
  allowed: number | null; detail: string; created_at: number;
}
export async function getAudit(action = "", risk = ""): Promise<{ entries: AuditEntry[] }> {
  const qs = new URLSearchParams();
  if (action) qs.set("action", action);
  if (risk) qs.set("risk", risk);
  return (await fetch(`/audit?${qs.toString()}`)).json();
}

// ── Skills hub ────────────────────────────────────────────────────────────────
export interface SkillPack { name: string; description: string; trigger: string; tools: string[]; path?: string; }
export async function getSkillLibrary(): Promise<{ packs: SkillPack[] }> {
  return (await fetch("/skills/library")).json();
}
export async function installSkill(body: { src?: string; skill_md?: string }): Promise<any> {
  return (await fetch("/skills/install", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).json();
}

// ── Update ────────────────────────────────────────────────────────────────────
export async function checkUpdate(): Promise<Record<string, any>> {
  return (await fetch("/update/check")).json();
}

export async function applyUpdate(): Promise<Record<string, any>> {
  return (await fetch("/update", { method: "POST" })).json();
}

export async function health(): Promise<boolean> {
  try {
    return (await fetch("/health")).ok;
  } catch {
    return false;
  }
}

// ── Status / health aggregate ─────────────────────────────────────────────────
export interface StatusInfo {
  status: string;
  model: string;
  providers: string[];
  secrets: Record<string, boolean>;
  ollama: { host: string; reachable: boolean };
}

export async function getStatus(): Promise<StatusInfo> {
  return (await fetch("/status")).json();
}

export async function ollamaPull(model: string): Promise<{ ok: boolean; output?: string; error?: string }> {
  return (await fetch("/providers/ollama/pull", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model }),
  })).json();
}

// ── Provider model discovery ──────────────────────────────────────────────────
export async function getProviderModels(name: string): Promise<{ models: string[]; error: string }> {
  return (await fetch(`/providers/${encodeURIComponent(name)}/models`)).json();
}

// ── Vision ────────────────────────────────────────────────────────────────────
export async function enableLocalVision(model = "llava"): Promise<{ ok: boolean; vision_model?: string; error?: string }> {
  return (await fetch("/vision/enable-local", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ model }),
  })).json();
}
export async function testVision(): Promise<{ ok: boolean; model: string; reply: string }> {
  return (await fetch("/vision/test", { method: "POST" })).json();
}

// ── Runs & observability ──────────────────────────────────────────────────────
export interface RunInfo {
  id: string; goal: string; mode: string; status: string;
  started_at: number; ended_at: number | null;
}

export async function getRuns(): Promise<{ runs: RunInfo[]; active: string[] }> {
  return (await fetch("/runs")).json();
}

export async function getRun(id: string): Promise<{ run: RunInfo | null; metrics: any[] }> {
  return (await fetch(`/runs/${id}`)).json();
}

export async function getRunEvents(id: string): Promise<{ events: any[] }> {
  return (await fetch(`/runs/${id}/events`)).json();
}

export async function getRunMessages(runId: string): Promise<{ messages: any[] }> {
  return (await fetch(`/messages?run_id=${runId}`)).json();
}

// ── Models & sessions ─────────────────────────────────────────────────────────
export interface ModelPreset {
  label: string;
  model: string;
  temperature?: number;
  effort?: string;
  thinking?: boolean;
}

export async function getModels(): Promise<{ models: ModelPreset[]; active: string }> {
  return (await fetch("/models")).json();
}

export async function getSessions(): Promise<{ sessions: any[] }> {
  return (await fetch("/sessions")).json();
}

export async function newSession(): Promise<{ id: number }> {
  return (await fetch("/sessions", { method: "POST" })).json();
}

export async function getSessionMessages(id: number): Promise<{ messages: any[] }> {
  return (await fetch(`/sessions/${id}/messages`)).json();
}

export async function deleteSession(id: number) {
  await fetch(`/sessions/${id}`, { method: "DELETE" });
}

export async function undoTurns(id: number, n = 1): Promise<{ ok: boolean; removed: number }> {
  return (await fetch(`/sessions/${id}/undo?n=${n}`, { method: "POST" })).json();
}

export async function renameSession(id: number, title: string) {
  await fetch(`/sessions/${id}`, {
    method: "PATCH", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });
}

export async function searchMemory(q: string): Promise<{ facts: string[]; messages: any[] }> {
  return (await fetch(`/memory/search?q=${encodeURIComponent(q)}`)).json();
}

// ── Knowledge export / import + backup ────────────────────────────────────────
export async function exportKnowledge(): Promise<any> {
  return (await fetch("/export/knowledge")).json();
}

export async function importKnowledge(data: any): Promise<any> {
  return (await fetch("/import/knowledge", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  })).json();
}

export async function restoreBackup(file: ArrayBuffer): Promise<any> {
  return (await fetch("/restore", {
    method: "POST", headers: { "Content-Type": "application/gzip" }, body: file,
  })).json();
}

// ── Scheduler ─────────────────────────────────────────────────────────────────
export interface Schedule {
  id: number;
  name: string;
  prompt: string;
  mode: string;
  spec: string;
  tz: string;
  enabled: number;
  next_run: number | null;
  last_run: number | null;
  last_status: string | null;
}

export async function getSchedules(): Promise<{ schedules: Schedule[] }> {
  return (await fetch("/schedules")).json();
}

export async function addSchedule(body: {
  prompt: string; schedule: string; mode?: string; name?: string; tz?: string;
}): Promise<Record<string, any>> {
  const r = await fetch("/schedules", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return r.json();
}

export async function toggleSchedule(id: number) {
  await fetch(`/schedules/${id}/toggle`, { method: "POST" });
}

export async function deleteSchedule(id: number) {
  await fetch(`/schedules/${id}`, { method: "DELETE" });
}

// ── Guide ─────────────────────────────────────────────────────────────────────
export async function getGuidePages(): Promise<{ pages: { slug: string; title: string }[] }> {
  return (await fetch("/guide")).json();
}

export async function getGuidePage(slug: string): Promise<{ content: string }> {
  return (await fetch(`/guide/${slug}`)).json();
}

// Live monitor stream for one orchestration run.
export class MonitorSocket {
  private ws: WebSocket;
  private closed = false;

  // ``onEnd`` fires when the server ends the stream (the run finished), not on close().
  constructor(runId: string, onEvent: (ev: XplogentEvent) => void, onEnd?: () => void) {
    this.ws = new WebSocket(wsUrl(`/ws/monitor?run_id=${encodeURIComponent(runId)}${wsTokenParam()}`));
    this.ws.onmessage = (m) => { const ev = parseFrame(m.data); if (ev) onEvent(ev); };
    this.ws.onclose = () => { if (!this.closed) onEnd?.(); };
  }

  close() {
    this.closed = true;
    this.ws.close();
  }
}
