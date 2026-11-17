import { useEffect, useState } from "react";
import { Activity, Brain, Cpu, KeyRound, Monitor, Moon, Palette, Plus, Settings as SettingsIcon, ShieldCheck, Sun, Trash2 } from "lucide-react";
import { FREE_MODELS } from "./lib/models";
import { useToast } from "./components/Toast";
import { useSubRoute } from "./lib/router";
import { type ThemeMode, useTheme } from "./lib/theme";
import { Badge, Button, Callout, Card, PageHeader, ResultLine, SecretField, Segmented, SelectField, Tabs, TextField } from "./ui";
import { useConfirm } from "./ui/confirm";
import { BrandingCard } from "./views/BrandingCard";
import { CardBoundary } from "./components/CardBoundary";
import { ModelTestButton } from "./components/ModelTest";
import {
  addFact, deleteFact, forgetLearnedFacts, deleteSkill, exportKnowledge, getFacts, getFullConfig,
  getProviderModels, getRoles, getSkills, getStatus, getTools, importKnowledge, patchConfig, putRole,
  putSecrets, restoreBackup, saveOllama, StatusInfo, testOllama, listOllamaServers, saveOllamaServer,
  deleteOllamaServer, type OllamaServer, StatusError,
  BackupStatus, getBackupModel, saveBackupModel, testBackupModel,
} from "./api";

const RISK_TIERS = ["low", "medium", "high", "critical"];
const ACTIONS = ["auto", "confirm", "deny"];
const SECRET_KEYS = ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY", "AI_GATEWAY_API_KEY"];
// Which model "Test key" tries: the one you use with that provider, else a cheap default.
const usedWith = (cfg: Record<string, any>, p: string) =>
  [cfg.voip?.model, cfg.model].find((m) => typeof m === "string" && m.startsWith(`${p}:`)) as string | undefined;
const KEY_TEST_MODEL: Record<string, (cfg: Record<string, any>) => string> = {
  OPENAI_API_KEY: (c) => usedWith(c, "openai") ?? "openai:gpt-4o-mini",
  ANTHROPIC_API_KEY: (c) => usedWith(c, "anthropic") ?? "",
  GEMINI_API_KEY: (c) => usedWith(c, "gemini") ?? "gemini:gemini-3.6-flash",
  OPENROUTER_API_KEY: (c) => usedWith(c, "openrouter") ?? FREE_MODELS[1].model,
  AI_GATEWAY_API_KEY: (c) => usedWith(c, "vercel") ?? "vercel:auto",        // the server picks a quick model
};
// Tool groups that drive a real screen/mouse: unusable on a server without a display.
const DISPLAY_GROUPS = ["gui"];

const SECTIONS = ["models", "keys", "appearance", "safety", "memory", "status"] as const;
type Section = typeof SECTIONS[number];
const SPEECH_KEYS = ["DEEPGRAM_API_KEY", "CARTESIA_API_KEY", "ELEVENLABS_API_KEY"];

export function Settings() {
  const [cfg, setCfg] = useState<Record<string, any> | null>(null);
  const [tools, setTools] = useState<any[]>([]);
  const [roles, setRoles] = useState<Record<string, any>>({});
  const [facts, setFacts] = useState<{ id: number; content: string; source?: string; created_at?: number }[]>([]);
  const [skills, setSkills] = useState<{ name: string; uses: number; description: string }[]>([]);
  const [caps, setCaps] = useState<StatusInfo["capabilities"]>();
  const [section, setSection] = useSubRoute<Section>("settings", SECTIONS, "models");
  const toast = useToast();
  const confirm = useConfirm();

  const reload = async () => {
    setCfg(await getFullConfig());
    setTools((await getTools()).tools);
    setRoles((await getRoles()).roles);
    setFacts((await getFacts()).facts);
    setSkills((await getSkills()).skills);
  };
  useEffect(() => { reload(); getStatus().then((s) => setCaps(s.capabilities)).catch(() => {}); }, []);

  const flash = (m: string) => toast(m, /fail|couldn|could not|error/i.test(m) ? "error" : "success");
  const save = async (updates: Record<string, any>) => { await patchConfig(updates); flash("saved"); };

  const tabs = [
    { id: "models" as Section, label: "Models", icon: Cpu },
    { id: "keys" as Section, label: "API keys", icon: KeyRound },
    { id: "appearance" as Section, label: "Appearance & backup", icon: Palette },
    { id: "safety" as Section, label: "Safety & tools", icon: ShieldCheck },
    { id: "memory" as Section, label: "Memory & skills", icon: Brain },
    { id: "status" as Section, label: "Status", icon: Activity },
  ];

  const enabledGroups: string[] = cfg?.tools_enabled ?? [];
  const policy: Record<string, string> = cfg?.safety?.policy ?? {};

  return (
    <div className="pane">
      <div className="pane-wide">
        <PageHeader icon={SettingsIcon} title="Settings" description="Models, keys, look and safety for the whole workspace." />
        <Tabs<Section> label="Settings sections" value={section} onChange={setSection} tabs={tabs} />
        {!cfg ? <div className="card"><div className="skeleton" style={{ height: 160 }} /></div> : <>

        {section === "models" && <>
          <Card title="Model" description="The main model for chat; calls can use their own (Calls → Setup).">
            <div className="form-grid">
              <TextField label="Active model" value={cfg.model} onCommit={(v) => save({ model: v })} />
              <TextField label="Reflection model" value={cfg.reflection_model} onCommit={(v) => save({ reflection_model: v })} />
              <TextField label="Embedding model" value={cfg.embedding_model} onCommit={(v) => save({ embedding_model: v })} />
              <TextField label="Vision model" optional value={cfg.vision_model}
                placeholder="e.g. ollama:llava, openai:gpt-4o" hint="For image chats and analyze_image; blank = the active model."
                onCommit={(v) => save({ vision_model: v })} />
            </div>
            <p className="fld-hint mt-3">Providers: {(cfg.providers ?? []).join(", ")}</p>
          </Card>
          <CardBoundary name="Backup model"><BackupModelCard flash={flash} /></CardBoundary>
          <CardBoundary name="Ollama server"><OllamaCard host={cfg.ollama?.host ?? ""} hasToken={!!cfg.secrets?.OLLAMA_API_KEY}
            onUse={async (u) => { await save(u); reload(); }} onSaved={reload} flash={flash} /></CardBoundary>
          <CardBoundary name="Ollama servers"><OllamaServersCard onUse={async (u) => { await save(u); reload(); }} flash={flash} /></CardBoundary>
          <CardBoundary name="Browse provider models"><ModelBrowser providers={cfg.providers ?? []} models={cfg.models ?? []}
            onAddPresets={async (m) => { await save({ models: m }); reload(); }} onSave={save} flash={flash} /></CardBoundary>
          <CardBoundary name="Models"><ModelsManager models={cfg.models ?? []} onSave={async (m) => { await save({ models: m }); reload(); }} /></CardBoundary>
        </>}

        {section === "keys" && <>
          <Callout tone="warn">Keys are stored on the server and never shown again. Anyone with the dashboard's access token can use
            them: keep the token private{caps?.docker ? " (rotate it with: xplogent-docker token --rotate)." : " and the server bound to 127.0.0.1."}</Callout>
          <Card title="Model providers">
            <div className="form-grid">
              {SECRET_KEYS.map((k) => (
                <div key={k} className="fld">
                  <SecretField label={k} isSet={!!cfg.secrets?.[k]}
                    onSave={async (v) => { await putSecrets({ [k]: v }); flash(`${k} saved`); reload(); }} />
                  {cfg.secrets?.[k] && KEY_TEST_MODEL[k](cfg) && <div><ModelTestButton spec={KEY_TEST_MODEL[k](cfg)} label="Test key" /></div>}
                </div>
              ))}
            </div>
          </Card>
          <Card title="Speech" description="Used by Calls → Speech (listening, voices and the Deepgram Voice Agent), where each has a Test button.">
            <div className="form-grid">
              {SPEECH_KEYS.map((k) => (
                <SecretField key={k} label={k} isSet={!!cfg.secrets?.[k]}
                  onSave={async (v) => { await putSecrets({ [k]: v }); flash(`${k} saved`); reload(); }} />
              ))}
            </div>
          </Card>
        </>}

        {section === "appearance" && <>
          <Appearance />
          <CardBoundary name="Branding"><BrandingCard toast={toast} /></CardBoundary>
          <CardBoundary name="Backup"><BackupCard flash={flash} reload={reload} /></CardBoundary>
        </>}

        {section === "safety" && <>
          <Card title="Safety policy" description="What the agent may do on its own, by the risk of the action.">
            <div className="form-grid">
              {RISK_TIERS.map((tier) => (
                <SelectField key={tier} label={`${tier[0].toUpperCase()}${tier.slice(1)} risk`} value={policy[tier] ?? "confirm"}
                  options={ACTIONS.map((a) => ({ value: a, label: a === "auto" ? "Allow" : a === "confirm" ? "Ask me first" : "Never" }))}
                  onChange={(v) => { setCfg({ ...cfg, safety: { ...(cfg.safety ?? {}), policy: { ...policy, [tier]: v } } }); save({ safety: { policy: { [tier]: v } } }); }} />
              ))}
            </div>
          </Card>
          <Card title="Tools" description="Groups of tools the chat assistant may use.">
            <div className="toolgrid">
              {Array.from(new Set(tools.map((t) => t.group))).map((group) => {
                const noScreen = DISPLAY_GROUPS.includes(group) && caps && !caps.display;
                return (
                  <label key={group} className="check" style={noScreen ? { opacity: 0.55 } : undefined}
                    title={noScreen ? "Needs a screen: not available on this server" : undefined}>
                    <input type="checkbox" defaultChecked={enabledGroups.includes(group)} disabled={!!noScreen}
                      onChange={(e) => {
                        const next = e.target.checked ? [...enabledGroups, group] : enabledGroups.filter((g) => g !== group);
                        setCfg({ ...cfg, tools_enabled: next });
                        save({ tools: { enabled: next } });
                      }} />
                    {group}{noScreen && <span className="dim small"> (needs a screen)</span>}
                  </label>
                );
              })}
            </div>
          </Card>
          <ExecutionBackend execution={cfg.execution ?? {}} onSave={save} />
          <RolesEditor roles={roles} allTools={tools.map((t) => t.name)} onSaved={() => { flash("role saved"); reload(); }} />
        </>}

        {section === "memory" && <>
          <Card title={<>Memory <Badge>{facts.length} facts</Badge></>}
            description={'"Learned" facts were picked up by the agent from chats; check them now and then.'}
            actions={facts.some((f) => f.source === "reflection" && (f.created_at ?? 0) * 1000 > Date.now() - 864e5) &&
              <Button size="sm" onClick={async () => {
                if (!(await confirm({ title: "Forget recently learned facts?", body: "Every fact the agent learned by itself in the last 24 hours is removed. Facts you added stay.", confirmLabel: "Forget them", danger: true }))) return;
                const r = await forgetLearnedFacts(24); flash(`forgot ${r.removed} learned fact(s)`); reload();
              }}>Forget the last 24 h</Button>}>
            <form className="fld-row" onSubmit={async (e) => {
              e.preventDefault();
              const el = (e.currentTarget.elements.namedItem("fact") as HTMLInputElement);
              if (el.value.trim()) { await addFact(el.value.trim()); el.value = ""; reload(); }
            }}>
              <input name="fact" placeholder="Remember a fact…" aria-label="New fact" />
              <Button type="submit" icon={Plus}>Add</Button>
            </form>
            <ul className="list">
              {facts.map((f) => (
                <li key={f.id}>
                  <span className="grow">{f.content}{" "}
                    <span className="faint xsmall">{f.source === "reflection" ? "learned" : f.source === "gui" ? "added by you" : f.source || ""}
                      {f.created_at ? ` · ${new Date(f.created_at * 1000).toLocaleDateString()}` : ""}</span></span>
                  <button className="x" aria-label="Forget this fact" title="Forget" onClick={async () => {
                    if (await confirm({ title: "Forget this fact?", body: f.content, confirmLabel: "Forget", danger: true })) { await deleteFact(f.id); reload(); }
                  }}><Trash2 size={14} /></button>
                </li>
              ))}
              {facts.length === 0 && <li className="dim">No facts yet.</li>}
            </ul>
          </Card>
          <Card title={<>Skills <Badge>{skills.length}</Badge></>} description="Skills the assistant learned or installed (more under Persona & Skills).">
            <ul className="list">
              {skills.map((sk) => (
                <li key={sk.name}><b>{sk.name}</b> <span className="dim">used {sk.uses}×</span>
                  <button className="x" aria-label={`Delete the skill ${sk.name}`} title="Delete" onClick={async () => {
                    if (await confirm({ title: `Delete the skill "${sk.name}"?`, confirmLabel: "Delete", danger: true })) { await deleteSkill(sk.name); reload(); }
                  }}><Trash2 size={14} /></button>
                </li>
              ))}
              {skills.length === 0 && <li className="dim">No skills yet.</li>}
            </ul>
          </Card>
        </>}

        {section === "status" && <CardBoundary name="Status & health"><HealthCard /></CardBoundary>}
        </>}
      </div>
    </div>
  );
}

function OllamaCard({ host, hasToken, onUse, onSaved, flash }: {
  host: string; hasToken: boolean; flash: (m: string) => void; onSaved: () => void;
  onUse: (u: Record<string, any>) => Promise<void>;
}) {
  const [url, setUrl] = useState(host);
  const [token, setTokenValue] = useState("");
  const [probe, setProbe] = useState<{ ok: boolean; models: string[]; error?: string } | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => setUrl(host), [host]);

  const test = async () => {
    setBusy(true);
    try { setProbe(await testOllama(url.trim(), token.trim())); }
    catch (e) { setProbe({ ok: false, models: [], error: String(e) }); }
    finally { setBusy(false); }
  };
  const store = async () => {
    const res = await saveOllama(url.trim(), token.trim());
    if (!res.ok) { flash(res.error || "not saved"); return; }
    setTokenValue("");
    flash("Ollama server saved");
    onSaved();  // refresh "✓ set" and the saved URL
    await test();
  };
  return (
    <div className="card">
      <h3>Ollama server (default)</h3>
      <p className="dim">Your own Ollama, e.g. <code>https://ai.example.com</code>. If it sits behind a proxy that
        needs a bearer token, enter it below (stored encrypted, never shown again). More servers, each with its
        own token: <b>Other Ollama servers</b> below.</p>
      <label>Server URL
        <input value={url} placeholder="https://ai.example.com" onChange={(e) => setUrl(e.target.value)} />
      </label>
      <label>Bearer token {hasToken ? <span className="secret-state set">saved</span> : <span className="dim">(optional)</span>}
        <input type="password" value={token} placeholder={hasToken ? "•••••• (set; type to replace)" : "paste token"}
               onChange={(e) => setTokenValue(e.target.value)} />
      </label>
      <div className="row" style={{ gap: 8 }}>
        <button onClick={test} disabled={busy || !url.trim()}>{busy ? "Testing…" : "Test connection"}</button>
        <button className="primary" onClick={store} disabled={!url.trim()}>Save</button>
      </div>
      {probe && (probe.ok
        ? <ResultLine ok>Connected: {probe.models.length} model(s)</ResultLine>
        : <ResultLine ok={false}>{probe.error}</ResultLine>)}
      {probe?.ok && probe.models.length > 0 && <ModelUseTable models={probe.models} server="" onUse={onUse} flash={flash} />}
    </div>
  );
}

/** "use for chat / calls / …" buttons for the models of one Ollama server. */
function ModelUseTable({ models, server, onUse, flash }: {
  models: string[]; server: string; flash: (m: string) => void;
  onUse: (u: Record<string, any>) => Promise<void>;
}) {
  const spec = (m: string) => `ollama:${m}${server ? `@${server}` : ""}`;
  const use = (m: string) => [
    ["chat", { model: spec(m) }],
    ["embeddings", { embedding_model: spec(m) }],
    ["calls", { voip: { model: spec(m) } }],
    ["reflection", { reflection_model: spec(m) }],
  ] as const;
  return (
    <table className="data-table" style={{ marginTop: 8 }}>
      <tbody>
        {models.map((m) => (
          <tr key={m}><td><code>{m}</code></td><td style={{ textAlign: "right" }}>
            {use(m).map(([label, update]) => (
              <button key={label} className="btn" style={{ marginLeft: 4 }}
                onClick={async () => { await onUse(update); flash(`${spec(m)} → ${label}`); }}>use for {label}</button>
            ))}
          </td></tr>
        ))}
      </tbody>
    </table>
  );
}

/** Any number of extra Ollama servers, each with its own bearer token (OLLAMA_API_KEY_<NAME>). */
function OllamaServersCard({ onUse, flash }: {
  flash: (m: string) => void; onUse: (u: Record<string, any>) => Promise<void>;
}) {
  const [servers, setServers] = useState<OllamaServer[]>([]);
  const [adding, setAdding] = useState(false);
  const load = () => listOllamaServers().then((r) => setServers(r.servers)).catch(() => {});
  useEffect(() => { load(); }, []);
  return (
    <div className="card" data-tour="ollama-servers">
      <h3>Other Ollama servers</h3>
      <p className="dim">For example a GPU machine for calls while this server stays small. Each server has its own
        address and bearer token; pick its models for chat, calls, embeddings or as the backup. In settings they
        are written as <code>ollama:model@name</code>.</p>
      {servers.map((s) => <OllamaServerRow key={s.name} server={s} onUse={onUse} flash={flash} onChange={load} />)}
      {servers.length === 0 && !adding && <p className="dim">No other servers yet.</p>}
      {adding
        ? <OllamaServerRow server={{ name: "", host: "", token_set: false }} onUse={onUse} flash={flash}
            onChange={() => { setAdding(false); load(); }} onCancel={() => setAdding(false)} />
        : <button onClick={() => setAdding(true)}>+ Add server</button>}
    </div>
  );
}

function OllamaServerRow({ server, onUse, flash, onChange, onCancel }: {
  server: OllamaServer; flash: (m: string) => void; onChange: () => void; onCancel?: () => void;
  onUse: (u: Record<string, any>) => Promise<void>;
}) {
  const isNew = !server.name;
  const confirm = useConfirm();
  const [name, setName] = useState(server.name);
  const [url, setUrl] = useState(server.host);
  const [token, setToken] = useState("");
  const [probe, setProbe] = useState<{ ok: boolean; models: string[]; error?: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const test = async () => {
    setBusy(true);
    try { setProbe(await testOllama(url.trim(), token.trim(), name.trim())); }
    catch (e) { setProbe({ ok: false, models: [], error: String(e) }); }
    finally { setBusy(false); }
  };
  const save = async () => {
    const r = await saveOllamaServer({ name: name.trim().toLowerCase(), host: url.trim(), token: token.trim() || undefined });
    if (!r.ok) { flash(r.error || "not saved"); return; }
    setToken(""); flash(`server ${name.trim()} saved`); onChange(); await test();
  };
  return (
    <div className="server-row" style={{ borderTop: "1px solid var(--glass-border)", paddingTop: 10, marginTop: 10 }}>
      <div className="row wrap" style={{ gap: 8, alignItems: "flex-end" }}>
        <label className="bud"><span className="lbl-sm">Name</span>
          <input value={name} disabled={!isNew} placeholder="gpu" style={{ width: 110 }}
            onChange={(e) => setName(e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, ""))} /></label>
        <label className="bud"><span className="lbl-sm">Server URL</span>
          <input value={url} placeholder="https://gpu.example.com" style={{ width: 230 }} onChange={(e) => setUrl(e.target.value)} /></label>
        <label className="bud"><span className="lbl-sm">Bearer token {server.token_set ? <span className="secret-state set">saved</span> : "(optional)"}</span>
          <input type="password" value={token} placeholder={server.token_set ? "•••••• (type to replace)" : "paste token"}
            style={{ width: 170 }} onChange={(e) => setToken(e.target.value)} /></label>
      </div>
      <div className="row wrap" style={{ gap: 8 }}>
        <button onClick={test} disabled={busy || !url.trim()}>{busy ? "Testing…" : "Test connection"}</button>
        <button className="primary" onClick={save} disabled={!name.trim() || !url.trim()}>Save</button>
        {isNew ? <button onClick={onCancel}>Cancel</button>
          : <button className="danger" onClick={async () => {
              if (!(await confirm({ title: `Remove the server "${server.name}"?`, body: "Its saved token is removed too.", confirmLabel: "Remove", danger: true }))) return;
              const res = await deleteOllamaServer(server.name);
              if (!res.ok) { flash(res.error || `couldn't remove ${server.name}`); return; }
              flash(`server ${server.name} removed`); onChange();
            }}>Remove</button>}
      </div>
      {probe && (probe.ok
        ? <ResultLine ok>Connected: {probe.models.length} model(s)</ResultLine>
        : <ResultLine ok={false}>{probe.error}</ResultLine>)}
      {probe?.ok && probe.models.length > 0 && !isNew && <ModelUseTable models={probe.models} server={server.name} onUse={onUse} flash={flash} />}
    </div>
  );
}

// Good backups sit on a different provider than the main model, so one outage can't take both.
const BACKUP_SUGGESTIONS = [
  ...FREE_MODELS.map((m) => ({ model: m.model, note: `${m.label}: free, rate-limited` })),
  { model: "gemini:gemini-3.6-flash", note: "Gemini Flash: fast, cheap" },
  { model: "openai:gpt-4o-mini", note: "GPT-4o mini" },
  { model: "anthropic:claude-haiku-4-5", note: "Claude Haiku" },
];

function BackupModelCard({ flash }: { flash: (m: string) => void }) {
  const [st, setSt] = useState<BackupStatus | null>(null);
  const [model, setModel] = useState("");
  const [busy, setBusy] = useState("");
  const [result, setResult] = useState<string>("");
  const [servers, setServers] = useState<OllamaServer[]>([]);
  const load = () => getBackupModel().then((s) => { setSt(s); setModel((m) => m || s.model); }).catch(() => {});
  useEffect(() => { load(); listOllamaServers().then((r) => setServers(r.servers)).catch(() => {}); }, []);
  if (!st) return null;

  const run = async (label: string, fn: () => Promise<void>) => {
    setBusy(label); setResult("");
    try { await fn(); } catch (e) { setResult(`✗ ${String(e)}`); } finally { setBusy(""); load(); }
  };
  const changed = model.trim() !== st.model;
  return (
    <div className="card">
      <h3>Backup model</h3>
      <p className="dim">If your main model or server can't answer (down, no key, a free model's rate limit), the
        backup answers instead, for chat and phone calls. Pick one on a different provider than your main model:
        a hosted model, or a model on one of your Ollama servers (<code>ollama:llama3.2:3b@gpu</code>).
        Nothing runs on this server itself.</p>
      <label>Backup model
        <input list="backup-models" value={model} placeholder="provider:model, e.g. gemini:gemini-3.6-flash"
          onChange={(e) => setModel(e.target.value)} />
        <datalist id="backup-models">
          {BACKUP_SUGGESTIONS.map((b) => <option key={b.model} value={b.model}>{b.note}</option>)}
          {servers.map((s) => <option key={s.name} value={`ollama:llama3.2:3b@${s.name}`}>on your Ollama server "{s.name}"</option>)}
        </datalist>
      </label>
      <div className="row wrap" style={{ gap: 8 }}>
        <span className={`badge ${st.enabled ? "ok" : ""}`}>{st.enabled ? "backup on" : "backup off"}</span>
        {st.model && <span className={`badge ${st.ready ? "ok" : "warn"}`} title={st.problem}>
          {st.model} {st.ready ? "ready" : `· ${st.problem}`}</span>}
      </div>
      <div className="row wrap" style={{ gap: 8 }}>
        {changed && <button disabled={!!busy || !model.trim()} onClick={() => run("model", async () => {
          const r = await saveBackupModel({ model: model.trim() });
          if (r.ok) flash("backup model saved"); else setResult(`✗ ${r.error}`);
        })}>Save model</button>}
        <button className="primary" disabled={!!busy || (!st.enabled && !(changed ? model.trim() : st.model))}
          onClick={() => run("save", async () => {
            const r = await saveBackupModel({ ...(changed ? { model: model.trim() } : {}), enabled: !st.enabled });
            if (r.ok) flash(st.enabled ? "backup model off" : "backup model on"); else setResult(`✗ ${r.error}`);
          })}>{st.enabled ? "Turn backup off" : "Turn backup on"}</button>
        <button disabled={!!busy || !st.model || changed} title={changed ? "save the model first" : ""}
          onClick={() => run("test", async () => {
            const r = await testBackupModel();
            setResult(r.ok ? `✓ ${r.model} answered in ${r.seconds}s: "${r.reply}"` : `✗ ${r.error}`);
          })}>{busy === "test" ? "Testing…" : "Test backup"}</button>
      </div>
      {result && <ResultLine ok={result.startsWith("✓")}>{result.replace(/^[✓✗] /, "")}</ResultLine>}
      {st.models && st.models.length > 0 && <p className="dim">On that Ollama server: {st.models.join(", ")}</p>}
    </div>
  );
}

function HealthCard() {
  const [st, setSt] = useState<StatusInfo | null>(null);
  const [problem, setProblem] = useState("");
  const reload = () => getStatus().then((s) => { setSt(s); setProblem(""); })
    .catch((e) => { setSt(null); setProblem(e instanceof StatusError ? e.message : ""); });
  useEffect(() => { reload(); const id = setInterval(reload, 10000); return () => clearInterval(id); }, []);
  return (
    <div className="card">
      <h3>Status &amp; health</h3>
      {!st ? (problem
        ? <p className="warn">The backend answered, but the status check failed ({problem}).
            The server log has the details: <code>xplogent-docker logs</code>.</p>
        : <p className="warn">Backend unreachable.</p>) : (
        <>
          {st.errors && Object.keys(st.errors).length > 0 && (
            <p className="warn">Part of the status failed: {Object.entries(st.errors)
              .map(([k, v]) => `${k}: ${v}`).join("; ")}</p>)}
          <div className="row wrap" style={{ gap: 8 }}>
            <span className="badge ok">backend online</span>
            {st.ollama.in_use && <span className={`badge ${st.ollama.reachable ? "ok" : "bad"}`}
                  title={st.ollama.error || st.ollama.host}>
              Ollama {st.ollama.reachable ? "reachable" : "down"}</span>}
            <span className="badge">active model: {st.model}</span>
            <span className="badge" title="the running build: compare with the newest on GitHub; update with xplogent-docker update">
              build {st.version}{st.revision ? ` · ${st.revision}` : ""}</span>
          </div>
          <h4 style={{ margin: "14px 0 6px", fontSize: 13 }} className="dim">Providers</h4>
          <div className="row wrap" style={{ gap: 8 }}>
            {st.providers.map((p) => {
              const keyMap: Record<string, string> = {
                openai: "OPENAI_API_KEY", anthropic: "ANTHROPIC_API_KEY",
                openrouter: "OPENROUTER_API_KEY", gemini: "GEMINI_API_KEY", vercel: "AI_GATEWAY_API_KEY",
              };
              const env = keyMap[p];
              const ok = !env || st.secrets[env];
              return <span key={p} className={`badge ${ok ? "ok" : "warn"}`}>{p}{env && !ok ? " · no key" : ""}</span>;
            })}
          </div>
        </>
      )}
    </div>
  );
}

function Appearance() {
  const { mode, setMode } = useTheme();
  return (
    <Card title="Theme" description="Also in the top bar. Saved in this browser.">
      <Segmented<ThemeMode> label="Theme" value={mode} onChange={setMode}
        options={[{ value: "auto", label: "Device", icon: Monitor }, { value: "light", label: "Light", icon: Sun }, { value: "dark", label: "Dark", icon: Moon }]} />
    </Card>
  );
}

function BackupCard({ flash, reload }: { flash: (m: string) => void; reload: () => void }) {
  const [msg, setMsg] = useState("");
  const doExport = async () => {
    const data = await exportKnowledge();
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "xplogent-knowledge.json";
    a.click();
  };
  const doImport = async (file: File) => {
    const data = JSON.parse(await file.text());
    const res = await importKnowledge(data);
    setMsg(`+${res.facts_added} facts, +${res.skills_added} skills`
           + (res.warnings?.length ? ` · ⚠ ${res.warnings[0]}` : ""));
    reload();
  };
  const doRestore = async (file: File) => {
    const res = await restoreBackup(await file.arrayBuffer());
    flash(res.ok ? "restored — restart to apply" : "restore failed");
  };
  return (
    <div className="card">
      <h3>Backup &amp; knowledge</h3>
      <p className="dim small">The full backup downloads the database, skills and settings as a .tar.gz.</p>
      <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
        <a className="btn primary" href="/backup">Download backup</a>
        <button onClick={doExport}>Export skills + memory</button>
        <label className="filebtn">Import knowledge
          <input type="file" accept="application/json" style={{ display: "none" }}
                 onChange={(e) => e.target.files?.[0] && doImport(e.target.files[0])} />
        </label>
        <label className="filebtn">Restore backup
          <input type="file" accept=".gz,.tar.gz,application/gzip" style={{ display: "none" }}
                 onChange={(e) => e.target.files?.[0] && doRestore(e.target.files[0])} />
        </label>
      </div>
      {msg && <p className="ok">{msg}</p>}
    </div>
  );
}

function ExecutionBackend({ execution, onSave }:
  { execution: Record<string, any>; onSave: (u: Record<string, any>) => void }) {
  const backend = execution.backend ?? "local";
  const docker = execution.docker ?? {};
  const ssh = execution.ssh ?? {};
  return (
    <div className="card">
      <h3>Execution backend</h3>
      <p className="dim">Where <code>shell</code> &amp; <code>python_exec</code> run. The deny-list still applies on every backend.</p>
      <label>Backend
        <select defaultValue={backend} onChange={(e) => onSave({ execution: { backend: e.target.value } })}>
          {["local", "docker", "ssh"].map((b) => <option key={b} value={b}>{b}</option>)}
        </select>
      </label>
      {backend === "docker" && (
        <>
          <label>Image
            <input defaultValue={docker.image ?? "python:3.11-slim"}
                   onBlur={(e) => onSave({ execution: { docker: { image: e.target.value } } })} />
          </label>
          <label>Container <span className="dim">(blank = ephemeral docker run)</span>
            <input defaultValue={docker.container ?? ""}
                   onBlur={(e) => onSave({ execution: { docker: { container: e.target.value } } })} />
          </label>
        </>
      )}
      {backend === "ssh" && (
        <>
          <label>Host
            <input defaultValue={ssh.host ?? ""}
                   onBlur={(e) => onSave({ execution: { ssh: { host: e.target.value } } })} />
          </label>
          <label>User
            <input defaultValue={ssh.user ?? ""}
                   onBlur={(e) => onSave({ execution: { ssh: { user: e.target.value } } })} />
          </label>
          <label>Key path
            <input defaultValue={ssh.key_path ?? ""}
                   onBlur={(e) => onSave({ execution: { ssh: { key_path: e.target.value } } })} />
          </label>
        </>
      )}
    </div>
  );
}

function ModelBrowser({ providers, models, onAddPresets, onSave, flash }: {
  providers: string[]; models: any[];
  onAddPresets: (m: any[]) => void; onSave: (u: Record<string, any>) => void; flash: (m: string) => void;
}) {
  const [provider, setProvider] = useState(providers[0] || "openrouter");
  const [list, setList] = useState<string[]>([]);
  const [filter, setFilter] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const fetchModels = async () => {
    setBusy(true); setError(""); setList([]);
    const res = await getProviderModels(provider);
    setBusy(false);
    setList(res.models || []);
    if (res.error) setError(res.error);
  };

  const spec = (id: string) => `${provider}:${id}`;
  const addPreset = (id: string) => {
    if (models.some((m) => m.model === spec(id))) { flash("already a preset"); return; }
    onAddPresets([...models, { label: id, model: spec(id), temperature: 0.7, effort: "off", thinking: false }]);
  };

  const shown = filter ? list.filter((m) => m.toLowerCase().includes(filter.toLowerCase())) : list;

  return (
    <div className="card">
      <h3>Browse provider models</h3>
      <p className="dim">Fetch a provider's live catalog and add models without typing ids by hand.</p>
      {FREE_MODELS.some((f) => !models.some((m) => m.model === f.model)) && (
        <p className="dim" style={{ fontSize: 12 }}>
          <button onClick={() => onAddPresets([...models, ...FREE_MODELS.filter((f) => !models.some((m) => m.model === f.model))
            .map((f) => ({ ...f, temperature: 0.7, effort: "off", thinking: false }))])}>Add free OpenRouter models</button>{" "}
          {FREE_MODELS.map((f) => f.label).join(" and ")}: they support tools, need an OpenRouter key and are rate-limited.</p>
      )}
      <div className="row wrap" style={{ gap: 8 }}>
        <select value={provider} onChange={(e) => setProvider(e.target.value)}>
          {providers.map((p) => <option key={p} value={p}>{p}</option>)}
        </select>
        <button onClick={fetchModels} disabled={busy}>{busy ? "Fetching…" : "Fetch models"}</button>
        {list.length > 0 && <input placeholder={`filter ${list.length} models…`} value={filter}
          onChange={(e) => setFilter(e.target.value)} style={{ flex: 1, minWidth: 160 }} />}
      </div>
      {error && <p className="err" style={{ fontSize: 12, marginTop: 6 }}>{error}</p>}
      {list.length > 0 && (
        <ul className="list model-browser" style={{ maxHeight: 320, overflowY: "auto", marginTop: 8 }}>
          {shown.slice(0, 300).map((id) => (
            <li key={id}>
              <span className="mono" style={{ flex: 1, fontSize: 12 }}>{id}</span>
              <button onClick={() => addPreset(id)}>Add preset</button>
              <button onClick={() => { onSave({ model: spec(id) }); flash(`active → ${id}`); }}>Use as active</button>
              <button onClick={() => { onSave({ vision_model: spec(id) }); flash(`vision → ${id}`); }}>Vision</button>
            </li>
          ))}
          {shown.length === 0 && <p className="dim">no matches for "{filter}"</p>}
        </ul>
      )}
    </div>
  );
}

function ModelsManager({ models, onSave }:
  { models: any[]; onSave: (m: any[]) => void }) {
  const blank = { label: "", model: "", temperature: 0.7, effort: "off", thinking: false };
  const [draft, setDraft] = useState(blank);
  return (
    <div className="card">
      <h3>Models ({models.length})</h3>
      <ul className="list">
        {models.map((m, i) => (
          <li key={i}><b>{m.label}</b> <span className="dim">{m.model} · {m.effort}{m.thinking ? " · think" : ""}</span>
            <button className="x" aria-label="Remove this preset" title="Remove" onClick={() => onSave(models.filter((_, j) => j !== i))}><Trash2 size={14} /></button>
          </li>
        ))}
      </ul>
      <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
        <input placeholder="label" value={draft.label}
               onChange={(e) => setDraft({ ...draft, label: e.target.value })} />
        <input placeholder="provider:model" value={draft.model}
               onChange={(e) => setDraft({ ...draft, model: e.target.value })} />
        <select value={draft.effort} onChange={(e) => setDraft({ ...draft, effort: e.target.value })}>
          {["off", "low", "medium", "high"].map((x) => <option key={x}>{x}</option>)}
        </select>
        <label className="check"><input type="checkbox" checked={draft.thinking}
               onChange={(e) => setDraft({ ...draft, thinking: e.target.checked })} />think</label>
        <button onClick={() => { if (draft.label && draft.model) { onSave([...models, draft]); setDraft(blank); } }}>Add</button>
      </div>
    </div>
  );
}

function RolesEditor({ roles, allTools, onSaved }:
  { roles: Record<string, any>; allTools: string[]; onSaved: () => void }) {
  const names = Object.keys(roles);
  const [sel, setSel] = useState(names[0] ?? "");
  const role = roles[sel];
  if (!role) return null;
  const allowed: string[] = role.allowed_tools === "*" ? allTools : (role.allowed_tools ?? []);

  return (
    <div className="card">
      <h3>Roles &amp; permissions</h3>
      <select value={sel} onChange={(e) => setSel(e.target.value)}>
        {names.map((n) => <option key={n}>{n}</option>)}
      </select>
      <p className="dim">network: {String(role.network)} · max steps: {role.max_steps}</p>
      <div className="toolgrid">
        {allTools.map((t) => {
          const on = role.allowed_tools === "*" || allowed.includes(t);
          return (
            <label key={t} className="check">
              <input type="checkbox" checked={on}
                     onChange={(e) => {
                       const base = role.allowed_tools === "*" ? allTools : [...allowed];
                       const next = e.target.checked ? [...new Set([...base, t])] : base.filter((x) => x !== t);
                       putRole(sel, { ...role, allowed_tools: next }).then(onSaved);
                     }} />
              {t}
            </label>
          );
        })}
      </div>
    </div>
  );
}
