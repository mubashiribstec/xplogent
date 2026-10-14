import { useEffect, useRef, useState } from "react";
import { FileAudio, FileQuestion, FileText, GraduationCap, RefreshCw, RotateCcw, Trash2, Upload, Wand2 } from "lucide-react";
import {
  buildPlaybook, deleteTrainingItem, getPlaybook, getTraining, getTrainingItem, PlaybookJob, retryTrainingItem,
  savePlaybook, TrainingItem, uploadTraining,
} from "../api";
import { useToast } from "../components/Toast";

const KIND_ICON = { recording: FileAudio, document: FileText, qa: FileQuestion } as const;
const KIND_LABEL = { recording: "Call recording", document: "Script / document", qa: "Q&A sheet" } as const;
const ACCEPT = ".wav,.mp3,.m4a,.ogg,.oga,.opus,.flac,.webm,.aac,.mp4,.txt,.md,.pdf,.html,.json,.csv,.tsv";
const fmtSize = (n: number) => (n > 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1e3))} KB`);

export function Training() {
  const toast = useToast();
  const [items, setItems] = useState<TrainingItem[]>([]);
  const [layout, setLayout] = useState("agent-left");
  const [uploading, setUploading] = useState<number | null>(null);
  const [view, setView] = useState<TrainingItem | null>(null);
  const [playbook, setPlaybook] = useState("");
  const [job, setJob] = useState<PlaybookJob | null>(null);
  const [dirty, setDirty] = useState(false);
  const [drag, setDrag] = useState(false);
  const file = useRef<HTMLInputElement>(null);

  const reload = () => { getTraining().then((r) => setItems(r.items)).catch(() => {}); };
  const reloadPlaybook = (takeContent: boolean) => getPlaybook().then((r) => {
    setJob(r.job);
    if (takeContent) setPlaybook(r.content);
  }).catch(() => {});

  useEffect(() => { reload(); reloadPlaybook(true); }, []);
  const busy = items.some((i) => i.status === "queued" || i.status === "processing") || job?.status === "running";
  useEffect(() => {
    if (!busy) return;
    const id = setInterval(() => { reload(); reloadPlaybook(false); }, 2000);
    return () => clearInterval(id);
  }, [busy]);

  const upload = async (files: File[]) => {
    if (!files.length) return;
    setUploading(0);
    try {
      const res = await uploadTraining(files, layout, setUploading);
      if (res.added?.length) toast(`added ${res.added.length} file(s); processing in the background`, "success");
      for (const r of res.rejected ?? []) toast(`${r.file}: ${r.error}`, "error");
    } catch (e) {
      toast(String((e as Error).message), "error");
    } finally {
      setUploading(null);
      if (file.current) file.current.value = "";
      reload();
    }
  };

  const remove = async (it: TrainingItem) => {
    if (!confirm(`Delete ${it.filename}? Its knowledge is removed from the agent too.`)) return;
    const r = await deleteTrainingItem(it.id);
    if (!r.ok) toast(r.error ?? "could not delete", "error");
    if (view?.id === it.id) setView(null);
    reload();
  };

  const open = async (it: TrainingItem) => {
    if (view?.id === it.id) { setView(null); return; }
    const r = await getTrainingItem(it.id);
    setView(r.item);
  };

  const build = async () => {
    const r = await buildPlaybook();
    setJob(r.job);
  };
  const useDraft = () => { if (job?.draft) { setPlaybook(job.draft); setDirty(true); } };
  const save = async () => {
    const r = await savePlaybook(playbook);
    if (r.ok) { setDirty(false); toast("playbook saved; the next call uses it", "success"); }
    else toast(r.error ?? "could not save", "error");
  };

  const done = items.filter((i) => i.status === "done");
  const transcripts = done.filter((i) => i.kind !== "document").length;

  return (
    <div className="pane">
      <div className="pane-wide">
        <div className="page-head">
          <h1><GraduationCap size={22} /> Call training</h1>
          <div className="row" style={{ gap: 8 }}>
            {busy && <span className="badge warn">working…</span>}
            <button className="icon-btn" onClick={() => { reload(); reloadPlaybook(false); }} aria-label="refresh">
              <RefreshCw size={16} /></button>
          </div>
        </div>
        <p className="dim">Teach the phone agent from your real material. Everything here is added to the knowledge
          the agent searches during calls; the playbook below is read before every call.</p>

        <div className={`card${drag ? " drop-active" : ""}`}
          onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
          onDragLeave={() => setDrag(false)}
          onDrop={(e) => { e.preventDefault(); setDrag(false); upload(Array.from(e.dataTransfer.files)); }}>
          <h3><Upload size={16} /> Upload</h3>
          <ul className="dim" style={{ marginTop: 0 }}>
            <li><b>Call recordings</b> (wav, mp3, m4a, ogg…): transcribed on this server. A stereo recording is
              split per channel so the transcript says who spoke.</li>
            <li><b>Scripts, FAQs, price lists</b> (txt, md, pdf).</li>
            <li><b>Q&amp;A sheet</b> (csv with <code>question</code> and <code>answer</code> columns): each row
              becomes one answer the agent can give.</li>
          </ul>
          <div className="row wrap" style={{ gap: 10, alignItems: "center" }}>
            <input ref={file} type="file" multiple accept={ACCEPT} disabled={uploading !== null}
              onChange={(e) => upload(Array.from(e.target.files ?? []))} />
            <label className="row" style={{ gap: 6 }}>Stereo recordings:
              <select value={layout} onChange={(e) => setLayout(e.target.value)}>
                <option value="agent-left">agent on the left channel</option>
                <option value="agent-right">agent on the right channel</option>
              </select>
            </label>
          </div>
          {uploading !== null && <div className="dim" style={{ marginTop: 8 }}>
            Uploading… {Math.round(uploading * 100)}%</div>}
          <p className="dim" style={{ marginBottom: 0 }}>Up to 200 MB per file. You can also drop files on this card.</p>
        </div>

        <div className="card">
          <h3>Material ({items.length})</h3>
          {items.length === 0 ? <p className="dim">Nothing uploaded yet.</p> : (
            <table className="data-table">
              <thead><tr><th>File</th><th>Type</th><th>Status</th><th>Knowledge</th><th></th></tr></thead>
              <tbody>
                {items.map((it) => {
                  const Icon = KIND_ICON[it.kind] ?? FileText;
                  return (
                    <tr key={it.id}>
                      <td><button className="linkish" onClick={() => open(it)} disabled={it.status !== "done"}>
                        <Icon size={14} /> {it.filename}</button>
                        <div className="dim" style={{ fontSize: 12 }}>{fmtSize(it.size)}
                          {it.kind === "recording" && it.options?.layout === "agent-right" ? " · agent on the right" : ""}</div>
                      </td>
                      <td>{KIND_LABEL[it.kind] ?? it.kind}</td>
                      <td>
                        {it.status === "processing"
                          ? <span className="badge warn">{it.kind === "recording" ? "transcribing" : "reading"} {Math.round(it.progress * 100)}%</span>
                          : <span className={`badge ${it.status === "done" ? "ok" : it.status === "error" ? "bad" : ""}`}>{it.status}</span>}
                        {it.error && <div className={it.status === "error" ? "err" : "dim"} style={{ fontSize: 12 }}>{it.error}</div>}
                      </td>
                      <td>{it.status === "done" ? `${it.chunks} piece${it.chunks === 1 ? "" : "s"}` : "—"}</td>
                      <td style={{ whiteSpace: "nowrap" }}>
                        {(it.status === "error" || it.status === "done") && it.kind === "recording" && (
                          <button className="icon-btn" title="Transcribe again (switch agent channel)" aria-label="transcribe again"
                            onClick={() => retryTrainingItem(it.id, it.options?.layout === "agent-right" ? "agent-left" : "agent-right").then(reload)}>
                            <RotateCcw size={15} /></button>)}
                        {it.status === "error" && it.kind !== "recording" && (
                          <button className="icon-btn" title="Try again" aria-label="try again"
                            onClick={() => retryTrainingItem(it.id).then(reload)}><RotateCcw size={15} /></button>)}
                        <button className="icon-btn" title="Delete" aria-label="delete" onClick={() => remove(it)}>
                          <Trash2 size={15} /></button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
          {view && (
            <div style={{ marginTop: 12 }}>
              <div className="row" style={{ justifyContent: "space-between" }}>
                <b>{view.filename}</b>
                <button className="btn ghost" onClick={() => setView(null)}>Close</button>
              </div>
              <pre className="md-edit" style={{ whiteSpace: "pre-wrap", maxHeight: 420, overflow: "auto" }}>
                {view.transcript || "(no text)"}</pre>
            </div>
          )}
        </div>

        <div className="card">
          <h3><Wand2 size={16} /> Playbook</h3>
          <p className="dim">How the agent should run a call: the flow, common questions, objections and the tone that
            works. <b>Build from calls</b> asks the call model to write a draft from your {transcripts} transcript
            {transcripts === 1 ? "" : "s"} and Q&amp;A sheets; review it, edit, then save.</p>
          <div className="row wrap" style={{ gap: 8, marginBottom: 8 }}>
            <button className="btn" disabled={job?.status === "running" || transcripts === 0} onClick={build}>
              {job?.status === "running" ? `Building… ${Math.round((job.progress ?? 0) * 100)}%` : "Build from calls"}</button>
            {job?.status === "done" && job.draft && <button className="btn" onClick={useDraft}>Use the new draft</button>}
            <button className="btn primary" disabled={!dirty} onClick={save}>Save playbook</button>
          </div>
          {job?.status === "error" && <p className="err">{job.error}</p>}
          {job?.status === "done" && job.draft && !dirty && playbook !== job.draft && (
            <details style={{ marginBottom: 8 }}><summary>New draft ready ({job.draft.length} characters)</summary>
              <pre className="md-edit" style={{ whiteSpace: "pre-wrap", maxHeight: 300, overflow: "auto" }}>{job.draft}</pre>
            </details>)}
          <textarea className="md-edit" rows={16} value={playbook}
            placeholder="# Playbook&#10;## Call flow&#10;- Greet, say who you are and why you're calling…"
            onChange={(e) => { setPlaybook(e.target.value); setDirty(true); }} />
        </div>
      </div>
    </div>
  );
}
