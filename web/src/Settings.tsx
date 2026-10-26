import { useEffect, useState } from "react";
import {
  addFact, deleteFact, deleteSkill, exportKnowledge, getFacts, getFullConfig,
  getProviderModels, getRoles, getSkills, getStatus, getTools, importKnowledge, patchConfig, putRole,
  putSecrets, restoreBackup, saveOllama, StatusInfo, testOllama, listOllamaServers, saveOllamaServer,
  deleteOllamaServer, type OllamaServer,
  BackupStatus, getBackupModel, pullBackupModel, saveBackupModel, testBackupModel,
} from "./api";

const RISK_TIERS = ["low", "medium", "high", "critical"];
const ACTIONS = ["auto", "confirm", "deny"];
const SECRET_KEYS = ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY"];
// Tool groups that drive a real screen/mouse: unusable on a server without a display.
const DISPLAY_GROUPS = ["gui"];

export function Settings() {
  const [cfg, setCfg] = useState<Record<string, any> | null>(null);
  const [tools, setTools] = useState<any[]>([]);
  const [roles, setRoles] = useState<Record<string, any>>({});
  const [facts, setFacts] = useState<{ id: number; content: string }[]>([]);
  const [skills, setSkills] = useState<{ name: string; uses: number; description: string }[]>([]);
  const [saved, setSaved] = useState("");
  const [caps, setCaps] = useState<StatusInfo["capabilities"]>();

  const reload = async () => {
    setCfg(await getFullConfig());
    setTools((await getTools()).tools);
    setRoles((await getRoles()).roles);
    setFacts((await getFacts()).facts);
    setSkills((await getSkills()).skills);
  };
  useEffect(() => { reload(); getStatus().then((s) => setCaps(s.capabilities)).catch(() => {}); }, []);

  const flash = (m: string) => { setSaved(m); setTimeout(() => setSaved(""), 1500); };
  const save = async (updates: Record<string, any>) => { await patchConfig(updates); flash("saved"); };

  if (!cfg) return <div className="settings"><p className="dim">loading…</p></div>;

  const enabledGroups: string[] = cfg.tools_enabled ?? [];
  const policy: Record<string, string> = cfg.safety?.policy ?? {};

  return (
    <div className="settings">
      <div className="warn-banner">⚠ Anyone with the access token controls this agent. Keep the token private
        {caps?.docker ? " (rotate it on the server with: xplogent-docker token --rotate)." : " and the server bound to 127.0.0.1."}</div>
      {saved && <div className="toast">{saved}</div>}

      <HealthCard />
      <OllamaCard host={cfg.ollama?.host ?? ""} hasToken={!!cfg.secrets?.OLLAMA_API_KEY}
        onUse={async (u) => { await save(u); reload(); }} onSaved={reload} flash={flash} />
      <OllamaServersCard onUse={async (u) => { await save(u); reload(); }} flash={flash} />
      <BackupModelCard flash={flash} />

      <div className="card">
        <h3>Model</h3>
        <label>Active model
          <input defaultValue={cfg.model} onBlur={(e) => save({ model: e.target.value })} />
        </label>
        <label>Reflection model
          <input defaultValue={cfg.reflection_model}
                 onBlur={(e) => save({ reflection_model: e.target.value })} />
        </label>
        <label>Embedding model
          <input defaultValue={cfg.embedding_model}
                 onBlur={(e) => save({ embedding_model: e.target.value })} />
        </label>
        <label>Vision model <span className="dim">(for image chats &amp; analyze_image; blank = use active model)</span>
          <input defaultValue={cfg.vision_model}
                 placeholder="e.g. ollama:llava, openai:gpt-4o, anthropic:claude-sonnet-4-6"
                 onBlur={(e) => save({ vision_model: e.target.value })} />
        </label>
        <p className="dim">Providers: {(cfg.providers ?? []).join(", ")}</p>
      </div>

      <ModelBrowser providers={cfg.providers ?? []} models={cfg.models ?? []}
        onAddPresets={async (m) => { await save({ models: m }); reload(); }}
        onSave={save} flash={flash} />
      <ModelsManager models={cfg.models ?? []} onSave={async (m) => { await save({ models: m }); reload(); }} />

      <Appearance />
      <BackupCard flash={flash} reload={reload} />
      <ExecutionBackend execution={cfg.execution ?? {}} onSave={save} />

      <div className="card">
        <h3>API keys</h3>
        {SECRET_KEYS.map((k) => (
          <label key={k}>{k} {cfg.secrets?.[k] ? "✓ set" : "✗ unset"}
            <input type="password" placeholder={cfg.secrets?.[k] ? "•••••• (set)" : "paste key"}
                   onBlur={async (e) => {
                     if (e.target.value) { await putSecrets({ [k]: e.target.value }); e.target.value = ""; reload(); }
                   }} />
          </label>
        ))}
      </div>

      <div className="card">
        <h3>Safety policy</h3>
        {RISK_TIERS.map((tier) => (
          <label key={tier}>{tier}
            <select defaultValue={policy[tier] ?? "confirm"}
                    onChange={(e) => save({ safety: { policy: { [tier]: e.target.value } } })}>
              {ACTIONS.map((a) => <option key={a} value={a}>{a}</option>)}
            </select>
          </label>
        ))}
      </div>

      <div className="card">
        <h3>Tools</h3>
        {Array.from(new Set(tools.map((t) => t.group))).map((group) => {
          const noScreen = DISPLAY_GROUPS.includes(group) && caps && !caps.display;
          return (
          <label key={group} className="check" style={noScreen ? { opacity: 0.5 } : undefined}
                 title={noScreen ? "Needs a screen: not available on this server" : undefined}>
            <input type="checkbox" defaultChecked={enabledGroups.includes(group)} disabled={!!noScreen}
                   onChange={(e) => {
                     const next = e.target.checked
                       ? [...enabledGroups, group]
                       : enabledGroups.filter((g) => g !== group);
                     save({ tools: { enabled: next } });
                   }} />
            {group}{noScreen && <span className="dim"> (needs a screen: not available on this server)</span>}
          </label>
          );
        })}
      </div>

      <RolesEditor roles={roles} allTools={tools.map((t) => t.name)}
                   onSaved={() => { flash("role saved"); reload(); }} />

      <div className="card">
        <h3>Memory ({facts.length} facts)</h3>
        <div className="row">
          <input id="newfact" placeholder="remember a fact…" />
          <button onClick={async () => {
            const el = document.getElementById("newfact") as HTMLInputElement;
            if (el.value) { await addFact(el.value); el.value = ""; reload(); }
          }}>Add</button>
        </div>
        <ul className="list">
          {facts.map((f) => (
            <li key={f.id}>{f.content}
              <button className="x" onClick={async () => { await deleteFact(f.id); reload(); }}>✕</button>
            </li>
          ))}
        </ul>
      </div>

      <div className="card">
        <h3>Skills ({skills.length})</h3>
        <ul className="list">
          {skills.map((s) => (
            <li key={s.name}><b>{s.name}</b> <span className="dim">×{s.uses}</span>
              <button className="x" onClick={async () => { await deleteSkill(s.name); reload(); }}>✕</button>
            </li>
          ))}
        </ul>
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
      <label>Bearer token {hasToken ? "✓ set" : <span className="dim">(optional)</span>}
        <input type="password" value={token} placeholder={hasToken ? "•••••• (set; type to replace)" : "paste token"}
               onChange={(e) => setTokenValue(e.target.value)} />
      </label>
      <div className="row" style={{ gap: 8 }}>
        <button onClick={test} disabled={busy || !url.trim()}>{busy ? "Testing…" : "Test connection"}</button>
        <button className="primary" onClick={store} disabled={!url.trim()}>Save</button>
      </div>
      {probe && (probe.ok
        ? <p className="ok">✓ Connected: {probe.models.length} model(s)</p>
        : <p className="warn">✗ {probe.error}</p>)}
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
        <label className="bud"><span className="lbl-sm">Bearer token {server.token_set ? "✓ set" : "(optional)"}</span>
          <input type="password" value={token} placeholder={server.token_set ? "•••••• (type to replace)" : "paste token"}
            style={{ width: 170 }} onChange={(e) => setToken(e.target.value)} /></label>
      </div>
      <div className="row wrap" style={{ gap: 8 }}>
        <button onClick={test} disabled={busy || !url.trim()}>{busy ? "Testing…" : "Test connection"}</button>
        <button className="primary" onClick={save} disabled={!name.trim() || !url.trim()}>Save</button>
        {isNew ? <button onClick={onCancel}>Cancel</button>
          : <button className="danger" onClick={async () => {
              if (!confirm(`Remove the server "${server.name}" and its token?`)) return;
              await deleteOllamaServer(server.name); flash(`server ${server.name} removed`); onChange();
            }}>Remove</button>}
      </div>
      {probe && (probe.ok
        ? <p className="ok">✓ Connected: {probe.models.length} model(s)</p>
        : <p className="warn">✗ {probe.error}</p>)}
      {probe?.ok && probe.models.length > 0 && !isNew && <ModelUseTable models={probe.models} server={server.name} onUse={onUse} flash={flash} />}
    </div>
  );
}

const BACKUP_SUGGESTIONS = [
  { model: "llama3.2:3b", note: "~2 GB, good default for 8-16 GB RAM" },
  { model: "qwen2.5:3b", note: "~2 GB, strong at following instructions" },
  { model: "llama3.2:1b", note: "~1.3 GB, fastest, weakest" },
  { model: "llama3.1:8b", note: "~4.7 GB, needs 16 GB+ RAM or a GPU" },
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
  const where = st.server ? `Ollama "${st.server}"` : "local Ollama";

  const run = async (label: string, fn: () => Promise<void>) => {
    setBusy(label); setResult("");
    try { await fn(); } catch (e) { setResult(`✗ ${String(e)}`); } finally { setBusy(""); load(); }
  };
  return (
    <div className="card">
      <h3>Backup model</h3>
      <p className="dim">If your main model or server can't answer (down, no key, a free model's rate limit), the
        backup answers instead, for chat and phone calls. It can run on this server's own Ollama
        (<code>xplogent-docker backup-model on</code> once) or on one of your other Ollama servers.</p>
      <label>Runs on
        <select value={st.server ?? ""} onChange={(e) => run("server", async () => {
          await saveBackupModel({ server: e.target.value }); flash("backup server saved");
        })}>
          <option value="">this server's Docker Ollama</option>
          {servers.map((s) => <option key={s.name} value={s.name}>Ollama server "{s.name}" ({s.host})</option>)}
        </select>
      </label>
      <div className="row wrap" style={{ gap: 8 }}>
        <span className={`badge ${st.reachable ? "ok" : "bad"}`} title={st.error || st.host}>
          {where} {st.reachable ? "reachable" : "not reachable"}</span>
        <span className={`badge ${st.installed ? "ok" : "warn"}`}>{st.model} {st.installed ? "downloaded" : "not downloaded"}</span>
        <span className={`badge ${st.enabled ? "ok" : ""}`}>{st.enabled ? "backup on" : "backup off"}</span>
      </div>
      <label>Backup model
        <input list="backup-models" value={model} onChange={(e) => setModel(e.target.value)} />
        <datalist id="backup-models">
          {BACKUP_SUGGESTIONS.map((b) => <option key={b.model} value={b.model}>{b.note}</option>)}
        </datalist>
      </label>
      <div className="row wrap" style={{ gap: 8 }}>
        <button disabled={!!busy || !st.reachable} onClick={() => run("pull", async () => {
          const r = await pullBackupModel(model.trim());
          setResult(r.ok ? `✓ ${r.model} downloaded` : `✗ ${r.error}`);
          if (r.ok) await saveBackupModel({ model: model.trim() });
        })}>{busy === "pull" ? "Downloading… (a few minutes)" : "Download model"}</button>
        <button className="primary" disabled={!!busy} onClick={() => run("save", async () => {
          await saveBackupModel({ model: model.trim(), enabled: !st.enabled });
          flash(st.enabled ? "backup model off" : "backup model on");
        })}>{st.enabled ? "Turn backup off" : "Turn backup on"}</button>
        <button disabled={!!busy || !st.reachable} onClick={() => run("test", async () => {
          const r = await testBackupModel();
          setResult(r.ok ? `✓ ${r.model} answered in ${r.seconds}s: "${r.reply}"` : `✗ ${r.error}`);
        })}>{busy === "test" ? "Testing…" : "Test backup"}</button>
      </div>
      {result && <p className={result.startsWith("✓") ? "ok" : "warn"}>{result}</p>}
      {st.reachable && st.models.length > 0 && <p className="dim">On the {where}: {st.models.join(", ")}</p>}
    </div>
  );
}

function HealthCard() {
  const [st, setSt] = useState<StatusInfo | null>(null);
  const reload = () => getStatus().then(setSt).catch(() => setSt(null));
  useEffect(() => { reload(); const id = setInterval(reload, 10000); return () => clearInterval(id); }, []);
  return (
    <div className="card">
      <h3>Status &amp; health</h3>
      {!st ? <p className="warn">Backend unreachable.</p> : (
        <>
          <div className="row wrap" style={{ gap: 8 }}>
            <span className="badge ok">backend online</span>
            <span className={`badge ${st.ollama.reachable ? "ok" : "bad"}`}
                  title={st.ollama.error || st.ollama.host}>
              Ollama {st.ollama.reachable ? "reachable" : "down"}</span>
            <span className="badge">active model: {st.model}</span>
          </div>
          <h4 style={{ margin: "14px 0 6px", fontSize: 13 }} className="dim">Providers</h4>
          <div className="row wrap" style={{ gap: 8 }}>
            {st.providers.map((p) => {
              const keyMap: Record<string, string> = {
                openai: "OPENAI_API_KEY", anthropic: "ANTHROPIC_API_KEY",
                openrouter: "OPENROUTER_API_KEY", gemini: "GEMINI_API_KEY",
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
  const [theme, setTheme] = useState(localStorage.getItem("xplogent_theme") || "auto");
  const [accent, setAccent] = useState(localStorage.getItem("xplogent_accent") || "#58a6ff");
  const applyTheme = (t: string) => {
    setTheme(t);
    localStorage.setItem("xplogent_theme", t);
    if (t === "auto") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", t);
  };
  const applyAccent = (c: string) => {
    setAccent(c);
    localStorage.setItem("xplogent_accent", c);
    document.documentElement.style.setProperty("--accent", c);
  };
  return (
    <div className="card">
      <h3>Appearance</h3>
      <label>Theme
        <select value={theme} onChange={(e) => applyTheme(e.target.value)}>
          {["auto", "dark", "light"].map((t) => <option key={t} value={t}>{t}</option>)}
        </select>
      </label>
      <label className="check">Accent color
        <input type="color" value={accent} onChange={(e) => applyAccent(e.target.value)} />
      </label>
    </div>
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
      <p className="dim">Full backup downloads the DB + skills + config as a .tar.gz.</p>
      <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
        <a className="primary" href="/backup" style={{ padding: "8px 14px", borderRadius: 6,
           textDecoration: "none", color: "#0d1117", background: "var(--accent)" }}>
          Download backup
        </a>
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
            <button className="x" onClick={() => onSave(models.filter((_, j) => j !== i))}>✕</button>
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
