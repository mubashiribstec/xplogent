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

export function wsUrl(path: string): string {
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

// ── Phone calling agent (VoIP) ────────────────────────────────────────────────
export interface CallRow {
  id: number; direction: string; contact_name?: string | null; from_number: string; to_number: string;
  status: string; outcome?: string | null; summary?: string | null; cost: number;
  started_at: number; ended_at?: number | null; test?: number; transport?: string; recording?: string;
  line?: string; via_queue?: number; original_called_number?: string; queue_wait_s?: number | null;
}
export interface CallTurn { speaker: string; text: string; created_at: number; }
export interface Contact {
  id: number; name: string; phone: string; email: string; timezone: string; status: string;
  notes: string; fields: Record<string, string>;
}
export interface VoipStatus {
  enabled: boolean; public_url: string; has_secret: boolean; contacts: number; calls: number;
  active: LiveCallInfo[];
}
export interface LiveCallInfo {
  call_id: number; number: string; direction: string; transport: string; test: boolean;
  contact_name: string; started: number; paused: boolean; notes: string[]; can_transfer: boolean; via_queue?: boolean;
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
  ollama: { host: string; reachable: boolean; auth?: boolean; error?: string };
  capabilities?: { display: boolean; docker: boolean; git: boolean };
}

async function postJSON<T>(url: string, body: unknown = {}): Promise<T> {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
                               body: JSON.stringify(body) });
  return r.json();
}

// ── Ollama server (remote, optional bearer token) ─────────────────────────────
export interface OllamaProbe { ok: boolean; models: string[]; error?: string }
export const testOllama = (host: string, token: string) =>
  postJSON<OllamaProbe>("/providers/ollama/test", { host, token });
export const saveOllama = (host: string, token: string) =>
  postJSON<{ ok: boolean; host?: string; error?: string }>("/providers/ollama/settings", { host, token });

// ── Backup model (local Ollama in Docker) ─────────────────────────────────────
export interface BackupStatus {
  enabled: boolean; model: string; host: string; reachable: boolean; models: string[];
  installed: boolean; error?: string;
}
export async function getBackupModel(): Promise<BackupStatus> { return (await fetch("/providers/backup")).json(); }
export const saveBackupModel = (u: { enabled?: boolean; model?: string; host?: string }) =>
  postJSON<Record<string, any>>("/providers/backup", u);
export const pullBackupModel = (model: string) => postJSON<Record<string, any>>("/providers/backup/pull", { model });
export const testBackupModel = () => postJSON<Record<string, any>>("/providers/backup/test");

// ── Phone agent setup ─────────────────────────────────────────────────────────
export interface ReadinessItem { id: string; label: string; ok: boolean; detail: string; required: boolean }
export async function getVoipReadiness(): Promise<{ ready: boolean; items: ReadinessItem[] }> {
  return (await fetch("/voip/readiness")).json();
}
export interface CallAgentInfo {
  templates: { name: string; direction: string; fields: string[] }[];
  template: string;
  crm: { enabled: boolean; base_url: string; token_env: string; token_set: boolean;
         deliveries: { ts: number; call_id: number; attempt: number; status: string; detail: string }[] };
  knowledge_gaps: { query: string; count: number; last_ts: number }[];
}
export async function getCallAgent(): Promise<CallAgentInfo> {
  return (await fetch("/voip/agent")).json();
}
export const testCrm = () => postJSON<{ ok: boolean; status?: string; detail?: string; error?: string }>("/voip/crm/test");
export const clearSipBusy = (line = "") => postJSON<{ ok: boolean }>("/voip/sip/busy/clear", line ? { line } : {});
export async function getVoices(provider = ""): Promise<{ ok: boolean; voices: { id: string; name: string }[]; error?: string }> {
  return (await fetch(`/voip/speech/voices${provider ? `?provider=${encodeURIComponent(provider)}` : ""}`)).json();
}
export const testTelnyx = () => postJSON<Record<string, any>>("/voip/telnyx/test");
export const testSpeech = (stt = true) => postJSON<Record<string, any>>("/voip/speech/test", { stt });

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
// ── Guide ─────────────────────────────────────────────────────────────────────
export async function getGuidePages(): Promise<{ pages: { slug: string; title: string }[] }> {
  return (await fetch("/guide")).json();
}

export async function getGuidePage(slug: string): Promise<{ content: string }> {
  return (await fetch(`/guide/${slug}`)).json();
}

// ── Phone agent training ─────────────────────────────────────────────────────
export interface TrainingItem {
  id: number; kind: "recording" | "document" | "qa"; filename: string; size: number;
  status: "queued" | "processing" | "done" | "error"; progress: number; error: string;
  chunks: number; doc_id: number | null; options: { layout?: string }; has_transcript?: boolean;
  transcript?: string; created_at: number;
}
export interface PlaybookJob { status: "idle" | "running" | "done" | "error"; progress: number; draft: string; error: string }
export async function getTraining(): Promise<{ items: TrainingItem[]; busy: boolean }> {
  return (await fetch("/voip/training")).json();
}
export async function getTrainingItem(id: number): Promise<{ item: TrainingItem | null }> {
  return (await fetch(`/voip/training/${id}`)).json();
}
export async function deleteTrainingItem(id: number): Promise<{ ok: boolean; error?: string }> {
  return (await fetch(`/voip/training/${id}`, { method: "DELETE" })).json();
}
export const retryTrainingItem = (id: number, layout?: string) =>
  postJSON<{ ok: boolean; error?: string }>(`/voip/training/${id}/retry`, layout ? { layout } : {});
/** Multipart upload with progress (XHR: fetch can't report upload progress). */
export function uploadTraining(files: File[], layout: string, onProgress: (f: number) => void):
    Promise<{ ok: boolean; added: { id: number; file: string; kind: string }[]; rejected: { file: string; error: string }[] }> {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    files.forEach((f) => form.append("files", f, f.name));
    form.append("layout", layout);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/voip/training/upload");
    const tok = getToken();
    if (tok) xhr.setRequestHeader("Authorization", `Bearer ${tok}`);
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(e.loaded / e.total); };
    xhr.onload = () => {
      if (xhr.status === 401) window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
      try { resolve(JSON.parse(xhr.responseText)); } catch { reject(new Error(`upload failed (HTTP ${xhr.status})`)); }
    };
    xhr.onerror = () => reject(new Error("upload failed: network error"));
    xhr.send(form);
  });
}
export async function getPlaybook(): Promise<{ content: string; job: PlaybookJob }> {
  return (await fetch("/voip/playbook")).json();
}
export async function savePlaybook(content: string): Promise<{ ok: boolean; error?: string }> {
  return (await fetch("/voip/playbook", { method: "PUT", headers: { "Content-Type": "application/json" },
                                          body: JSON.stringify({ content }) })).json();
}
export const buildPlaybook = () => postJSON<{ ok: boolean; job: PlaybookJob }>("/voip/playbook/build");

// ── Live call control, manual + test calls ──────────────────────────────────
export type CallResult = { ok: boolean; error?: string; hours?: boolean; [k: string]: any };
export const hangupCall = (id: number) => postJSON<CallResult>(`/voip/calls/${id}/hangup`);
export const transferCall = (id: number, to = "") => postJSON<CallResult>(`/voip/calls/${id}/transfer`, to ? { to } : {});
export const sayOnCall = (id: number, text: string) => postJSON<CallResult>(`/voip/calls/${id}/say`, { text });
export const instructCall = (id: number, text: string) => postJSON<CallResult>(`/voip/calls/${id}/instruct`, { text });
export const pauseAI = (id: number, paused: boolean) => postJSON<CallResult>(`/voip/calls/${id}/pause-ai`, { paused });
export const dialCall = (b: { number?: string; contact_id?: number; via?: string; test?: boolean; ignore_hours?: boolean }) =>
  postJSON<CallResult>("/voip/dial", b);

// ── SIP trunk ────────────────────────────────────────────────────────────────
export interface SipExtensionStatus {
  enabled: boolean; configured: boolean; password_set: boolean; running: boolean; registered: boolean;
  error: string; server?: string; username?: string; local_port?: number; contact_ip?: string; rtp?: string;
  calls?: number; last_error?: string; expires?: number; line?: string;
}
export interface SipStatus {
  telephony: string; configured: boolean; password_set: boolean; running: boolean; registered: boolean;
  error: string; server?: string; username?: string; local_port?: number; contact_ip?: string; rtp?: string;
  calls?: number; last_error?: string; expires?: number;
  extension?: SipExtensionStatus;
}
export async function getSipStatus(): Promise<SipStatus> { return (await fetch("/voip/sip/status")).json(); }
export const saveSip = (b: Record<string, any>) => postJSON<{ ok: boolean; error?: string; status?: SipStatus }>("/voip/sip/settings", b);
export const testSip = () => postJSON<{ ok: boolean; error?: string; note?: string; status: SipStatus }>("/voip/sip/test");
export const saveSipExtension = (b: Record<string, any>) => postJSON<{ ok: boolean; error?: string; status?: SipExtensionStatus }>("/voip/sip/extension/settings", b);
export const testSipExtension = () => postJSON<{ ok: boolean; error?: string; note?: string; status: SipExtensionStatus }>("/voip/sip/extension/test");
export async function detectSipIp(): Promise<{ lan: string; internet: string; note: string }> {
  return (await fetch("/voip/sip/detect-ip")).json();
}

// ── Call recordings ──────────────────────────────────────────────────────────
/** The WAV as a blob URL (an <audio> tag can't send the access token itself). */
export async function getRecordingUrl(callId: number): Promise<string> {
  const r = await fetch(`/voip/calls/${callId}/recording`);
  if (!r.ok) throw new Error("no recording");
  return URL.createObjectURL(await r.blob());
}
export async function deleteRecording(callId: number): Promise<void> {
  await fetch(`/voip/calls/${callId}/recording`, { method: "DELETE" });
}
export const trainOnRecording = (callId: number) =>
  postJSON<{ ok: boolean; error?: string }>(`/voip/calls/${callId}/recording/train`);
