import { useEffect, useState } from "react";
import { BookText, FileUp, Search, Trash2 } from "lucide-react";
import { DocInfo, deleteDoc, getDocs, ingestDocs, searchDocs } from "../api";
import { useToast } from "../components/Toast";

export function Knowledge() {
  const toast = useToast();
  const [docs, setDocs] = useState<DocInfo[]>([]);
  const [path, setPath] = useState("");
  const [paste, setPaste] = useState("");
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<any[]>([]);
  const [busy, setBusy] = useState(false);

  const reload = () => getDocs().then((r) => setDocs(r.documents)).catch(() => {});
  useEffect(() => { reload(); }, []);

  const ingest = async (body: { path?: string; content?: string; title?: string }) => {
    setBusy(true);
    const res = await ingestDocs(body);
    setBusy(false);
    if (res.ok) { toast(`ingested ${res.ingested?.length ?? 0} file(s), ${res.chunks} chunks`, "success"); reload(); }
    else toast(res.error || "ingest failed", "error");
  };

  const search = async () => {
    if (!q.trim()) return;
    setHits((await searchDocs(q)).hits ?? []);
  };

  return (
    <div className="pane">
      <div className="pane-wide">
        <div className="page-head"><h1><BookText size={22} /> Knowledge</h1>
          <span className="dim">the agent answers from these via <code>search_docs</code></span></div>

        <div className="card">
          <h3><FileUp size={16} /> Add documents</h3>
          <p className="dim">Ingest a file or a whole folder (text, markdown, code; PDF needs the <code>rag</code> extra).</p>
          <div className="row wrap" style={{ gap: 8 }}>
            <input placeholder="path to a file or folder…" value={path} onChange={(e) => setPath(e.target.value)} />
            <button className="btn primary" disabled={busy} onClick={() => { if (path.trim()) ingest({ path: path.trim() }); setPath(""); }}>
              {busy ? "Ingesting…" : "Ingest path"}</button>
          </div>
          <textarea className="md-edit" style={{ marginTop: 8 }} rows={5} placeholder="…or paste text to remember"
            value={paste} onChange={(e) => setPaste(e.target.value)} />
          <button className="btn" style={{ marginTop: 6 }} disabled={busy}
            onClick={() => { if (paste.trim()) ingest({ content: paste, title: "pasted" }); setPaste(""); }}>Ingest pasted text</button>
        </div>

        <div className="card">
          <h3><Search size={16} /> Search</h3>
          <div className="row" style={{ gap: 8 }}>
            <input placeholder="ask your documents…" value={q}
              onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === "Enter" && search()} />
            <button className="btn" onClick={search}>Search</button>
          </div>
          {hits.map((h, i) => (
            <div key={i} className="card" style={{ marginTop: 8, padding: 12 }}>
              <div className="badge">{h.source}</div>
              <p style={{ marginTop: 6, whiteSpace: "pre-wrap" }}>{h.content.slice(0, 600)}</p>
            </div>
          ))}
        </div>

        <div className="card">
          <h3>Sources ({docs.length})</h3>
          <ul className="list">
            {docs.map((d) => (
              <li key={d.id}>
                <span style={{ flex: 1 }}><b>{d.title}</b> <span className="dim">· {d.chunks} chunks</span><br />
                  <span className="dim" style={{ fontSize: 12 }}>{d.source}</span></span>
                <button className="x" onClick={() => deleteDoc(d.id).then(reload)}><Trash2 size={14} /></button>
              </li>
            ))}
            {docs.length === 0 && <div className="empty-state">
              <p>Nothing here yet. Add your price list, FAQ or policies: the agent answers only from what it knows.</p>
              <button className="btn primary" onClick={() => (document.querySelector(".pane textarea") as HTMLTextAreaElement | null)?.focus()}>
                Paste your first document</button>{" "}
              <span className="dim" style={{ fontSize: 12 }}>(or upload files under Call training)</span></div>}
          </ul>
        </div>
      </div>
    </div>
  );
}
