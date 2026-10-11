import { useEffect, useRef, useState } from "react";
import { BanIcon, FileUp, Trash2, Users } from "lucide-react";
import { Contact, addDnc, deleteContact, getContacts, importContactsCsv } from "../api";
import { useToast } from "../components/Toast";

export function Contacts() {
  const toast = useToast();
  const [list, setList] = useState<Contact[]>([]);
  const [total, setTotal] = useState(0);
  const [q, setQ] = useState("");
  const [paste, setPaste] = useState("");
  const [busy, setBusy] = useState(false);
  const file = useRef<HTMLInputElement>(null);

  const reload = (query = q) => getContacts(query).then((r) => { setList(r.contacts); setTotal(r.total); }).catch(() => {});
  useEffect(() => { reload(""); }, []);

  const upload = async (csv: string) => {
    setBusy(true);
    const res = await importContactsCsv(csv);
    setBusy(false);
    if (res.ok) {
      toast(`imported ${res.imported}, skipped ${res.skipped}${res.dnc ? `, ${res.dnc} on do-not-call` : ""}`, "success");
      setPaste(""); reload();
    } else toast(res.error || "import failed", "error");
  };

  const onFile = async (f?: File | null) => { if (f) await upload(await f.text()); if (file.current) file.current.value = ""; };

  return (
    <div className="pane">
      <div className="pane-wide">
        <div className="page-head"><h1><Users size={22} /> Contacts</h1>
          <span className="dim">{total} customers · caller ID matches these during calls</span></div>

        <div className="card">
          <h3><FileUp size={16} /> Import from CSV</h3>
          <p className="dim">Needs a phone column (phone / mobile / number…). Name, email, timezone and notes are
            recognised; any other columns (plan, balance, last order…) are kept and shown to the agent during the call.
            A <code>dnc</code> column set to yes puts that number on the do-not-call list.</p>
          <div className="row wrap" style={{ gap: 8 }}>
            <input ref={file} type="file" accept=".csv,text/csv" onChange={(e) => onFile(e.target.files?.[0])} />
          </div>
          <textarea className="md-edit" style={{ marginTop: 8 }} rows={4} placeholder="…or paste CSV text here"
            value={paste} onChange={(e) => setPaste(e.target.value)} />
          <button className="btn" style={{ marginTop: 6 }} disabled={busy || !paste.trim()} onClick={() => upload(paste)}>
            {busy ? "Importing…" : "Import pasted CSV"}</button>
        </div>

        <div className="card">
          <div className="row" style={{ justifyContent: "space-between", gap: 8 }}>
            <h3>Customers</h3>
            <input placeholder="search name / phone / email" value={q} style={{ width: 240 }}
              onChange={(e) => { setQ(e.target.value); reload(e.target.value); }} />
          </div>
          <table className="data-table">
            <thead><tr><th>Name</th><th>Phone</th><th>Status</th><th>Details</th><th></th></tr></thead>
            <tbody>
              {list.map((c) => (
                <tr key={c.id}>
                  <td>{c.name || <span className="dim">—</span>}</td>
                  <td className="mono">{c.phone}</td>
                  <td><span className="badge">{c.status}</span></td>
                  <td className="dim" style={{ fontSize: 12 }}>
                    {[c.email, ...Object.entries(c.fields || {}).map(([k, v]) => `${k}: ${v}`)].filter(Boolean).join(" · ")}</td>
                  <td style={{ whiteSpace: "nowrap" }}>
                    <button className="icon-btn" title="add to do-not-call"
                      onClick={() => addDnc(c.phone).then(() => toast("added to do-not-call", "success"))}><BanIcon size={14} /></button>
                    <button className="x" onClick={() => deleteContact(c.id).then(() => reload())}><Trash2 size={14} /></button>
                  </td>
                </tr>
              ))}
              {list.length === 0 && <tr><td colSpan={5} className="dim">no contacts yet — import a CSV above</td></tr>}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
