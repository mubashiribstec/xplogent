import { useEffect, useRef, useState } from "react";
import { BanIcon, FileUp, Phone, Search, Trash2, Upload, Users } from "lucide-react";
import { Contact, addDnc, deleteContact, getContacts, importContactsCsv } from "../api";
import { useToast } from "../components/Toast";
import { Badge, Button, Card, EmptyState, IconButton, PageHeader } from "../ui";
import { useConfirm } from "../ui/confirm";
import { placeCall } from "./CallControls";

export function Contacts() {
  const toast = useToast();
  const confirm = useConfirm();
  const [list, setList] = useState<Contact[]>([]);
  const [total, setTotal] = useState(0);
  const [q, setQ] = useState("");
  const [paste, setPaste] = useState("");
  const [busy, setBusy] = useState(false);
  const file = useRef<HTMLInputElement>(null);

  const reload = (query = q) => getContacts(query).then((r) => { setList(r.contacts); setTotal(r.total); }).catch(() => {});
  useEffect(() => { reload(""); /* eslint-disable-next-line */ }, []);

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

  const call = async (c: Contact) => {
    if (await confirm({ title: `Call ${c.name || c.phone}?`, body: "The AI agent places the call now.", confirmLabel: "Call now" }))
      placeCall({ contact_id: c.id }, toast, confirm);
  };
  const dnc = async (c: Contact) => {
    if (!(await confirm({ title: "Add to do-not-call?", body: `${c.phone} will never be called by the agent.`, confirmLabel: "Add" }))) return;
    await addDnc(c.phone); toast("added to do-not-call", "success");
  };
  const remove = async (c: Contact) => {
    if (!(await confirm({ title: `Delete ${c.name || c.phone}?`, body: "The contact and its details are removed. Call history stays.",
      confirmLabel: "Delete", danger: true }))) return;
    await deleteContact(c.id); reload();
  };

  return (
    <div className="pane">
      <div className="pane-wide">
        <PageHeader icon={Users} title="Contacts" description={`${total} customers · caller ID matches these during calls.`} />

        <Card title={<><FileUp size={16} aria-hidden /> Import from CSV</>}
          description={<>Needs a phone column (phone / mobile / number…). Name, email, timezone and notes are recognised;
            any other columns (plan, balance, last order…) are kept and shown to the agent during the call. A <code>dnc</code>{" "}
            column set to yes puts that number on the do-not-call list.</>}>
          <div className="row wrap" style={{ gap: 8 }}>
            <label className="filebtn"><Upload size={15} aria-hidden /> Choose a CSV file
              <input ref={file} type="file" accept=".csv,text/csv" onChange={(e) => onFile(e.target.files?.[0])} /></label>
          </div>
          <textarea className="md-edit mt-3" rows={4} placeholder="…or paste CSV text here" aria-label="CSV text"
            value={paste} onChange={(e) => setPaste(e.target.value)} />
          <div className="mt-2"><Button loading={busy} disabled={!paste.trim()} onClick={() => upload(paste)}>Import pasted CSV</Button></div>
        </Card>

        <Card title="Customers" actions={
          <div className="side-search" style={{ minWidth: "min(280px, 100%)" }}>
            <Search size={15} aria-hidden />
            <input placeholder="Search name, phone or email" aria-label="Search contacts" value={q}
              onChange={(e) => { setQ(e.target.value); reload(e.target.value); }} />
          </div>}>
          {list.length === 0 ? (
            <EmptyState icon={Users} title={q ? "No matches" : "No contacts yet"}
              actions={!q && <Button variant="primary" icon={Upload} onClick={() => file.current?.click()}>Import contacts (CSV)</Button>}>
              {q ? "Try another name or number." : "Import your customer list so the agent knows who is calling."}
            </EmptyState>
          ) : (
            <div className="table-scroll">
              <table className="data-table">
                <thead><tr><th>Name</th><th>Phone</th><th>Status</th><th>Details</th><th><span className="sr-only">Actions</span></th></tr></thead>
                <tbody>
                  {list.map((c) => (
                    <tr key={c.id}>
                      <td data-label="Name">{c.name || <span className="dim">—</span>}</td>
                      <td data-label="Phone" className="mono">{c.phone}</td>
                      <td data-label="Status"><Badge>{c.status}</Badge></td>
                      <td data-label="Details" className="dim small">
                        {[c.email, ...Object.entries(c.fields || {}).map(([k, v]) => `${k}: ${v}`)].filter(Boolean).join(" · ") || "—"}</td>
                      <td>
                        <div className="row-actions">
                          <IconButton icon={Phone} size={15} label={`Call ${c.name || c.phone} now (the AI agent calls)`} onClick={() => call(c)} />
                          <IconButton icon={BanIcon} size={15} label={`Add ${c.phone} to do-not-call`} onClick={() => dnc(c)} />
                          <IconButton icon={Trash2} size={15} label={`Delete ${c.name || c.phone}`} onClick={() => remove(c)} />
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </div>
    </div>
  );
}
