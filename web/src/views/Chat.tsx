import { useEffect, useRef, useState } from "react";
import {
  Copy, Download, FileJson, ImagePlus, Mic, MicOff, RotateCcw, Search, Send, Share2, ShieldCheck,
  Square, Trash2, Undo2, Volume2, X,
} from "lucide-react";
import {
  ApprovalRequest, ConnStatus, XplogentEvent, XplogentSocket,
  consolidateMemory, deleteSession, getSessionMessages, getSessions, getSkills, newSession,
  renameSession, searchMemory, undoTurns,
} from "../api";
import { GenChoice, ModelBar } from "../ModelBar";
import { Markdown } from "../Markdown";
import { useToast } from "../components/Toast";
import { downloadJSON, downloadMarkdown, shareHTML } from "../lib/exportChat";
import { useVoice } from "../hooks/useVoice";

// Browsers only allow the microphone on https:// pages (or localhost).
const MIC_NEEDS_HTTPS = "Microphone needs HTTPS (or localhost): set up a domain with xplogent-docker https <domain>";
const micAllowed = typeof window !== "undefined" && window.isSecureContext;

interface LogLine { kind: "assistant" | "tool" | "result" | "note" | "user"; text: string; ok?: boolean; }
interface Usage {
  input_tokens?: number; output_tokens?: number; session_input?: number;
  session_output?: number; session_cost?: number; context_used?: number; context_limit?: number;
}

const readImage = (file: File) => new Promise<string>((res, rej) => {
  const r = new FileReader();
  r.onload = () => res(String(r.result));
  r.onerror = rej;
  r.readAsDataURL(file);
});

export function Chat({ sidebarOpen }: { sidebarOpen?: boolean }) {
  const toast = useToast();
  const [skills, setSkills] = useState<any[]>([]);
  const [sessions, setSessions] = useState<any[]>([]);
  const [search, setSearch] = useState("");
  const [hits, setHits] = useState<any[]>([]);
  const [log, setLog] = useState<LogLine[]>([]);
  const [input, setInput] = useState("");
  const [images, setImages] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [drag, setDrag] = useState(false);
  const [usage, setUsage] = useState<Usage | null>(null);
  const [approval, setApproval] = useState<ApprovalRequest | null>(null);
  const [, setConn] = useState<ConnStatus>("connecting");
  const [gen, setGen] = useState<GenChoice>({ model: "", effort: "off", thinking: false });
  // Approval scope: auto-approve up to high risk for "this chat" or "this session".
  const autoChat = useRef(false);
  const autoSession = useRef(false);
  const [autoScope, setAutoScope] = useState<"" | "chat" | "session">("");
  const lastTask = useRef<string>("");
  const sock = useRef<XplogentSocket | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const bottom = useRef<HTMLDivElement | null>(null);
  const sessionId = useRef<number | null>(Number(localStorage.getItem("xplogent_session")) || null);
  const [activeId, setActiveId] = useState<number | null>(sessionId.current);
  const voice = useVoice();
  const [handsFree, setHandsFree] = useState(false);
  const handsFreeRef = useRef(false);
  const busyRef = useRef(false);
  const loadSeq = useRef(0);  // ignore session loads that finish after a newer one started
  const toMsgs = (r: { messages: any[] }): LogLine[] => r.messages.map((m: any) => ({
    kind: m.role === "user" ? "user" : "assistant", text: m.content,
  }));
  const fail = (what: string) => (e: unknown) =>
    toast(`${what}: ${e instanceof Error ? e.message : String(e)}`, "error");

  const pushAssistantToken = (text: string) =>
    setLog((l) => {
      const last = l[l.length - 1];
      if (last && last.kind === "assistant") return [...l.slice(0, -1), { ...last, text: last.text + text }];
      return [...l, { kind: "assistant", text }];
    });

  const handleEvent = (ev: XplogentEvent) => {
    switch (ev.type) {
      case "token":
        pushAssistantToken(String(ev.text ?? ""));
        if (handsFreeRef.current) voice.feed(String(ev.text ?? ""));
        break;
      case "tool_call":
        setLog((l) => [...l, { kind: "tool", text: `${ev.tool} ${JSON.stringify(ev.arguments ?? {})}` }]); break;
      case "tool_result":
        setLog((l) => [...l, { kind: "result", text: String(ev.output ?? ""), ok: Boolean(ev.ok) }]); break;
      case "memory":
        setLog((l) => [...l, { kind: "note", text: `recalled ${ev.facts} facts, ${ev.skills} skills` }]); break;
      case "skill":
        setLog((l) => [...l, { kind: "note", text: `learned ${ev.facts} fact(s)${ev.skill ? `, skill '${ev.skill}'` : ""}` }]);
        if (ev.skill) {
          const stars = "★".repeat(Number(ev.skill_stars) || 1);
          toast(`Skill ${ev.skill_action === "updated" ? "improved" : "learned"}: ${ev.skill} ${stars}`, "success");
        }
        refreshSkills(); break;
      case "usage": setUsage(ev as unknown as Usage); break;
      case "thinking":
        if (ev.text) setLog((l) => [...l, { kind: "note", text: `⚠ ${ev.text}` }]);
        break;
      case "budget":
        setLog((l) => [...l, { kind: "note", text: `💰 budget (${ev.scope}): ${ev.reason}${ev.action === "downgrade" ? ` → switched to ${ev.model}` : ev.action === "pause" ? " → run paused" : ""}` }]);
        toast(String(ev.reason ?? "budget cap reached"), ev.action === "pause" ? "error" : "info");
        break;
      case "session":
        sessionId.current = Number(ev.id); localStorage.setItem("xplogent_session", String(ev.id)); break;
      case "approval_required": {
        const req = ev as unknown as ApprovalRequest;
        // Critical always prompts; otherwise honor a chosen chat/session scope.
        if (req.risk !== "critical" && (autoChat.current || autoSession.current)) {
          sock.current?.resolveApproval(req.id, true);
        } else setApproval(req);
        break;
      }
      case "error": toast(String(ev.message ?? "error"), "error"); break;
      case "done":
        busyRef.current = false; setBusy(false); refreshSkills(); refreshSessions();
        if (handsFreeRef.current) voice.flush();
        break;
    }
  };

  const refreshSkills = () => getSkills().then((s) => setSkills(s.skills)).catch(() => {});
  const refreshSessions = () => getSessions().then((s) => setSessions(s.sessions)).catch(() => {});
  const runSearch = (q: string) => {
    setSearch(q);
    if (!q.trim()) { setHits([]); return; }
    searchMemory(q).then((r) => setHits(r.messages ?? [])).catch(() => setHits([]));
  };

  // Recover if the socket drops mid-run: the in-flight run can't stream to the new
  // connection, so clear the stuck "busy" and reload the persisted conversation.
  const onStatus = (s: ConnStatus) => {
    setConn(s);
    if (s === "online" && busyRef.current) {
      busyRef.current = false; setBusy(false);
      if (sessionId.current) {
        const seq = ++loadSeq.current;
        getSessionMessages(sessionId.current)
          .then((r) => { if (seq === loadSeq.current) setLog(toMsgs(r)); })
          .catch(() => {});
      }
    }
  };

  const connect = () => {
    sock.current?.close();
    sock.current = new XplogentSocket(handleEvent, sessionId.current, onStatus);
  };

  const loadSession = (id: number | null) => {
    sessionId.current = id; setActiveId(id); setUsage(null);
    autoChat.current = false;  // per-chat approval doesn't carry across chats
    if (!autoSession.current) setAutoScope("");
    // The old chat's run ends with its socket: don't leave this chat locked as "busy"
    // or showing that run's approval prompt.
    busyRef.current = false; setBusy(false); setApproval(null);
    const seq = ++loadSeq.current;
    if (id) {
      localStorage.setItem("xplogent_session", String(id));
      getSessionMessages(id)
        .then((r) => { if (seq === loadSeq.current) setLog(toMsgs(r)); })
        .catch(() => { if (seq === loadSeq.current) setLog([]); });
    } else setLog([]);
    connect();
  };

  useEffect(() => {
    refreshSkills(); refreshSessions(); loadSession(sessionId.current);
    return () => { sock.current?.close(); voice.stopListening(); voice.cancelSpeak(); };
  }, []);
  // Braces matter: newer Chrome returns a Promise from scrollIntoView, and an effect's
  // return value is treated as its cleanup; calling that Promise crashed the whole app.
  useEffect(() => { bottom.current?.scrollIntoView({ behavior: "smooth" }); }, [log]);
  useEffect(() => { busyRef.current = busy; }, [busy]);

  const newChat = async () => {
    // End-of-conversation save: flush skills to the folder + distill MEMORY.md.
    if (log.length > 0) consolidateMemory().catch(() => {});
    try {
      const { id } = await newSession(); loadSession(id); refreshSessions();
    } catch (e) { fail("could not start a new chat")(e); }
  };
  const removeSession = async (id: number) => {
    try {
      await deleteSession(id);
      if (id === activeId) { localStorage.removeItem("xplogent_session"); loadSession(null); }
      refreshSessions(); toast("chat deleted", "success");
    } catch (e) { fail("could not delete the chat")(e); }
  };
  const rename = async (id: number, current: string) => {
    const title = window.prompt("Rename chat:", current);
    if (!title?.trim()) return;
    try { await renameSession(id, title.trim()); refreshSessions(); }
    catch (e) { fail("could not rename the chat")(e); }
  };

  const addFiles = async (files: FileList | File[]) => {
    const imgs = [...files].filter((f) => f.type.startsWith("image/"));
    const uris = await Promise.all(imgs.map(readImage));
    if (uris.length) setImages((x) => [...x, ...uris]);
  };

  const sendText = (text: string) => {
    // busyRef, not busy: hands-free calls this from a listener created earlier, and two
    // quick utterances must not start two runs before React re-renders.
    if ((!text.trim() && !images.length) || busyRef.current) return;
    busyRef.current = true;
    lastTask.current = text;
    setLog((l) => [...l, { kind: "user", text: text + (images.length ? `  ·  📎 ${images.length} image(s)` : "") }]);
    sock.current?.sendTask(text, {
      model: gen.model || undefined, effort: gen.effort, thinking: gen.thinking,
      images: images.length ? images : undefined,
    });
    setInput(""); setImages([]); setBusy(true);
  };
  // Voice callbacks outlive the render they were created in: always call the latest
  // sendText so they use the current model / options instead of the ones at toggle time.
  const sendTextRef = useRef(sendText);
  sendTextRef.current = sendText;

  const send = () => {
    const cmd = input.trim().match(/^\/undo(?:\s+(\d+))?$/i);
    if (cmd) { undo(Number(cmd[1] || 1)); setInput(""); return; }
    if (busy) { steer(input); return; }   // mid-run: send a suggestion, don't start a new run
    sendText(input);
  };

  // Nudge the running agent with a suggestion (folded into its next step).
  const steer = (text: string) => {
    if (!text.trim()) return;
    sock.current?.sendSteer(text.trim());
    setLog((l) => [...l, { kind: "user", text: `💡 ${text.trim()}` }]);
    setInput("");
  };

  // Roll back the last N exchanges (also typed as "/undo [N]").
  const undo = async (n = 1) => {
    if (!activeId) { toast("nothing to undo", "info"); return; }
    try {
      const res = await undoTurns(activeId, n);
      if (res.removed > 0) { loadSession(activeId); toast(`undid ${n} turn${n > 1 ? "s" : ""}`, "success"); }
      else toast("nothing to undo", "info");
    } catch (e) { fail("undo failed")(e); }
  };

  // Hands-free conversation: speak replies, auto-send finished utterances, barge-in.
  const toggleHandsFree = () => {
    const next = !handsFree;
    setHandsFree(next); handsFreeRef.current = next;
    if (next) {
      if (!voice.supported) { toast("voice isn't supported in this browser", "error"); setHandsFree(false); handsFreeRef.current = false; return; }
      voice.startListening({ onFinal: (t) => sendTextRef.current(t), onSpeechStart: () => voice.cancelSpeak() });
      toast("hands-free on — just talk", "success");
    } else { voice.stopListening(); voice.cancelSpeak(); }
  };
  // One-shot dictation into the input box.
  const toggleDictate = () => {
    if (!voice.supported) { toast("voice isn't supported in this browser", "error"); return; }
    if (voice.listening) voice.stopListening();
    else voice.startListening({ onFinal: (t) => setInput((v) => (v ? v + " " : "") + t) });
  };
  const stop = () => { sock.current?.cancel(); busyRef.current = false; setBusy(false); };
  // Re-send the last task directly (the old setInput + send() read the stale input).
  const regenerate = () => { if (lastTask.current && !busyRef.current) sendText(lastTask.current); };
  const resolve = (allowed: boolean, scope: "" | "chat" | "session" = "") => {
    if (allowed && scope === "chat") { autoChat.current = true; setAutoScope("chat"); }
    if (allowed && scope === "session") { autoSession.current = true; setAutoScope("session"); }
    if (approval) sock.current?.resolveApproval(approval.id, allowed);
    setApproval(null);
  };
  const clearAuto = () => { autoChat.current = false; autoSession.current = false; setAutoScope(""); };

  const transcript = () => log.filter((l) => l.kind === "user" || l.kind === "assistant")
    .map((l) => ({ role: l.kind, text: l.text }));
  const chatTitle = () => sessions.find((s) => s.id === activeId)?.title || "Xplogent chat";

  return (
    <div className="view">
      <aside className={`sidebar ${sidebarOpen ? "open" : ""}`}>
        <div className="side-head">
          <button className="btn primary" onClick={newChat}><span>+ New chat</span></button>
          <div className="side-search">
            <Search size={15} className="dim" />
            <input value={search} placeholder="Search chats…" onChange={(e) => runSearch(e.target.value)} />
          </div>
        </div>
        <div className="side-scroll">
          {search && (
            <ul className="chatlist">
              {hits.length === 0 && <p className="dim" style={{ padding: "8px" }}>no matches</p>}
              {hits.map((h, i) => (
                <li key={i}><button className="open" title={h.content}
                  onClick={() => { loadSession(h.session_id); runSearch(""); }}>
                  <span className="dim">[{h.role}]</span> {String(h.content).slice(0, 48)}
                </button></li>
              ))}
            </ul>
          )}
          <h2>Chats</h2>
          {sessions.length === 0 && <p className="dim" style={{ padding: "0 8px" }}>no chats yet</p>}
          <ul className="chatlist">
            {sessions.map((s) => (
              <li key={s.id} className={s.id === activeId ? "active" : ""}>
                <button className="open" onClick={() => loadSession(s.id)}
                  onDoubleClick={() => rename(s.id, s.title)} title="double-click to rename">
                  {s.title || "chat"} <span className="dim">· {s.message_count ?? 0}</span>
                </button>
                <button className="x" aria-label="delete" onClick={() => removeSession(s.id)}><Trash2 size={14} /></button>
              </li>
            ))}
          </ul>
          <h2>Learned skills</h2>
          {skills.length === 0 && <p className="dim" style={{ padding: "0 8px" }}>none yet</p>}
          {skills.map((s) => (
            <div className="skill-row" key={s.name} title={`${s.description} — ${s.level ?? ""}`}>
              <b style={{ flex: 1 }}>{s.name}</b>
              <span className="stars">{"★".repeat(s.stars ?? 1)}{"☆".repeat(3 - (s.stars ?? 1))}</span>
            </div>
          ))}
        </div>
      </aside>

      <main className="chat">
        <ModelBar value={gen} onChange={setGen} />
        <div className="chat-toolbar">
          {autoScope && (
            <span className="badge warn">
              <ShieldCheck size={13} /> auto-approving ({autoScope})
              <button className="linkx" onClick={clearAuto}>turn off</button>
            </span>
          )}
          <div style={{ flex: 1 }} />
          {voice.supported && (
            <button className={`btn ghost sm ${handsFree ? "active" : ""}`}
              disabled={!micAllowed} style={micAllowed ? undefined : { opacity: 0.5 }}
              title={micAllowed ? "Hands-free voice conversation" : MIC_NEEDS_HTTPS}
              onClick={toggleHandsFree}><Volume2 size={15} /> {handsFree ? "Hands-free on" : "Hands-free"}</button>
          )}
          <button className="btn ghost sm" title="Undo the last exchange (/undo [N])" disabled={!log.length || busy}
            onClick={() => undo(1)}><Undo2 size={15} /> Undo</button>
          <button className="btn ghost sm" title="Download as Markdown" disabled={!log.length}
            onClick={() => downloadMarkdown(chatTitle(), transcript())}><Download size={15} /> .md</button>
          <button className="btn ghost sm" title="Download as JSON" disabled={!log.length}
            onClick={() => downloadJSON(chatTitle(), transcript())}><FileJson size={15} /> .json</button>
          <button className="btn ghost sm" title="Export a shareable HTML file" disabled={!log.length}
            onClick={() => { shareHTML(chatTitle(), transcript()); toast("shareable HTML exported", "success"); }}>
            <Share2 size={15} /> Share</button>
        </div>
        <div className="log">
          {log.length === 0 && <div className="empty"><ImagePlus size={28} />
            <div>Start a conversation. Attach an image to use vision.</div></div>}
          {log.map((line, i) => (
            line.kind === "tool" ? <div key={i} className="msg"><div className="avatar" style={{ visibility: "hidden" }} />
              <div className="body"><span className="chip tool">⚙ {line.text.slice(0, 240)}</span></div></div>
            : line.kind === "result" ? <div key={i} className="msg"><div className="avatar" style={{ visibility: "hidden" }} />
              <div className="body"><span className={`chip result ${line.ok ? "" : "bad"}`}>{line.ok ? "✓" : "✗"} {line.text.slice(0, 400)}</span></div></div>
            : line.kind === "note" ? <div key={i} className="msg"><div className="avatar" style={{ visibility: "hidden" }} />
              <div className="body"><span className="chip note">✨ {line.text}</span></div></div>
            : (
              <div key={i} className={`msg ${line.kind}`}>
                <div className="avatar">{line.kind === "user" ? "U" : "X"}</div>
                <div className="body">
                  <div className="who"><span>{line.kind === "user" ? "You" : "Xplogent"}</span>
                    <span className="actions">
                      <button className="icon-btn" style={{ width: 26, height: 26 }} aria-label="copy"
                        onClick={() => { navigator.clipboard?.writeText(line.text); toast("copied", "success"); }}>
                        <Copy size={13} /></button>
                      {line.kind === "assistant" && i === log.length - 1 && (
                        <button className="icon-btn" style={{ width: 26, height: 26 }} aria-label="regenerate"
                          onClick={regenerate}><RotateCcw size={13} /></button>)}
                    </span>
                  </div>
                  <div className="bubble">
                    {line.kind === "assistant" ? <Markdown text={line.text} /> : line.text}
                    {busy && line.kind === "assistant" && i === log.length - 1 && <span className="caret" />}
                  </div>
                </div>
              </div>
            )
          ))}
          <div ref={bottom} />
        </div>

        {usage && <UsageBar u={usage} />}

        <div className="composer" data-tour="chat">
          {images.length > 0 && (
            <div className="thumbs">
              {images.map((src, i) => (
                <div className="thumb" key={i}>
                  <img src={src} alt="" />
                  <button onClick={() => setImages((x) => x.filter((_, j) => j !== i))}><X size={11} /></button>
                </div>
              ))}
            </div>
          )}
          <div className="row">
            <div className={`field ${drag ? "drag" : ""}`}
              onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
              onDragLeave={() => setDrag(false)}
              onDrop={(e) => { e.preventDefault(); setDrag(false); addFiles(e.dataTransfer.files); }}>
              <button className="icon-btn" style={{ width: 34, height: 34 }} aria-label="attach image"
                onClick={() => fileRef.current?.click()}><ImagePlus size={18} /></button>
              {voice.supported && !handsFree && (
                <button className={`icon-btn ${voice.listening ? "rec" : ""}`}
                  style={{ width: 34, height: 34, opacity: micAllowed ? 1 : 0.5 }} disabled={!micAllowed}
                  aria-label="dictate" title={micAllowed ? "Dictate with your voice" : MIC_NEEDS_HTTPS} onClick={toggleDictate}>
                  {voice.listening ? <MicOff size={18} /> : <Mic size={18} />}</button>
              )}
              <input ref={fileRef} type="file" accept="image/*" multiple style={{ display: "none" }}
                onChange={(e) => e.target.files && addFiles(e.target.files)} />
              <textarea rows={1} value={input + (voice.listening && voice.interim ? ` ${voice.interim}` : "")}
                placeholder={handsFree ? "Listening… just talk" : busy ? "Add a suggestion… (Enter to nudge the agent)" : "Message Xplogent…  (Enter to send, Shift+Enter for newline)"}
                onChange={(e) => setInput(e.target.value)}
                onPaste={(e) => { const imgs = [...e.clipboardData.files]; if (imgs.length) addFiles(imgs); }}
                onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }} />
            </div>
            <button className={`send ${busy ? "stop" : ""}`} onClick={busy ? stop : send}
              aria-label={busy ? "stop" : "send"}>
              {busy ? <Square size={16} /> : <Send size={16} />}
            </button>
          </div>
        </div>
      </main>

      {approval && (
        <div className="overlay center" onClick={() => resolve(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-body">
              <h3>Approval required</h3>
              <p><b>{approval.tool}</b> — risk{" "}
                <span className={`badge ${approval.risk === "critical" ? "bad" : "warn"}`}>{approval.risk}</span></p>
              {approval.reason && <p className="dim">{approval.reason}</p>}
              <pre className="snippet">{JSON.stringify(approval.arguments, null, 2)}</pre>
              {approval.risk !== "critical" && (
                <p className="dim" style={{ fontSize: 12 }}>
                  <ShieldCheck size={12} /> "this chat" / "this session" auto-approve non-critical
                  tools so you aren't asked every time. Critical actions always prompt.
                </p>
              )}
            </div>
            <div className="modal-actions" style={{ flexWrap: "wrap" }}>
              <button className="btn danger" onClick={() => resolve(false)}>Deny</button>
              <button className="btn" onClick={() => resolve(true)}>Allow once</button>
              {approval.risk !== "critical" && <>
                <button className="btn" onClick={() => resolve(true, "chat")}>Allow this chat</button>
                <button className="btn primary" onClick={() => resolve(true, "session")}>Allow this session</button>
              </>}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function UsageBar({ u }: { u: Usage }) {
  const used = u.context_used ?? 0, limit = u.context_limit ?? 0;
  const pct = limit ? Math.min(100, Math.round((used / limit) * 100)) : 0;
  const sess = (u.session_input ?? 0) + (u.session_output ?? 0);
  return (
    <div className="usagebar">
      {u.input_tokens != null && <span>turn: <b>{u.input_tokens}</b> in / <b>{u.output_tokens}</b> out</span>}
      {sess > 0 && <span>session: <b>{sess.toLocaleString()}</b> tok</span>}
      {!!u.session_cost && <span>~<b>${u.session_cost.toFixed(4)}</b></span>}
      <span className="ctx">context
        <span className="meter"><span className="fill" style={{ width: `${pct}%` }} /></span>
        {used.toLocaleString()} / {limit.toLocaleString()}</span>
    </div>
  );
}
