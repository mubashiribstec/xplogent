import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import {
  ChevronDown, ChevronLeft, ChevronRight, Download, FileSpreadsheet, PhoneIncoming, PhoneOutgoing, RefreshCw,
} from "lucide-react";
import { type CallTurn, type CdrFilters, type CdrRow, type CdrTotals, downloadCdrCsv, getCall, getCdr } from "../api";
import { useToast } from "../components/Toast";
import { Badge, Button, EmptyState, Field, SelectField, Switch } from "../ui";
import { CRM_TONE, CallDetail } from "./calls/Overview";
import { fmtTime, visibleInterval } from "./calls/shared";

type Period = "today" | "7d" | "30d" | "all" | "custom";
const PERIODS: { value: Period; label: string }[] = [
  { value: "today", label: "Today" }, { value: "7d", label: "Last 7 days" }, { value: "30d", label: "Last 30 days" },
  { value: "all", label: "All time" }, { value: "custom", label: "Custom dates" },
];
const SORTS = [
  { value: "newest", label: "Newest first" }, { value: "oldest", label: "Oldest first" },
  { value: "longest", label: "Longest first" }, { value: "cost", label: "Most expensive first" },
];
const LINES = [
  { value: "", label: "All lines" }, { value: "sip", label: "SIP trunk" }, { value: "extension", label: "PBX extension" },
  { value: "telnyx", label: "Telnyx" }, { value: "browser", label: "Browser test" },
];
const SAVED = "xplogent_cdr_filters";

const midnight = (daysAgo = 0) => {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  d.setDate(d.getDate() - daysAgo);
  return d.getTime() / 1000;
};
const dayStart = (iso: string) => (iso ? new Date(`${iso}T00:00`).getTime() / 1000 : undefined);
const hms = (s: number) => {
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = Math.round(s % 60);
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}` : `${m}:${String(sec).padStart(2, "0")}`;
};

interface Saved { period: Period; from: string; to: string; direction: string; answered: string; outcome: string;
  line: string; test: boolean; transcripts: boolean; sort: string; perPage: number }
const DEFAULTS: Saved = { period: "7d", from: "", to: "", direction: "", answered: "", outcome: "", line: "",
  test: false, transcripts: false, sort: "newest", perPage: 50 };

function loadSaved(): Saved {
  try { return { ...DEFAULTS, ...JSON.parse(localStorage.getItem(SAVED) || "{}") }; } catch { return DEFAULTS; }
}

/** Every AI call, inbound and outbound: search, filter, totals and a CSV export. */
export function Cdr() {
  const toast = useToast();
  const [f, setF] = useState<Saved>(loadSaved);
  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");                   // the search, once typing pauses
  const [page, setPage] = useState(1);
  const [data, setData] = useState<{ rows: CdrRow[]; total: number; totals: CdrTotals; outcomes: string[] } | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState<number | null>(null);
  const [turns, setTurns] = useState<CallTurn[]>([]);

  const set = (u: Partial<Saved>) => { setF((old) => ({ ...old, ...u })); setPage(1); };
  useEffect(() => { try { localStorage.setItem(SAVED, JSON.stringify(f)); } catch { /* storage blocked */ } }, [f]);
  useEffect(() => { const t = setTimeout(() => { setQuery(q.trim()); setPage(1); }, 300); return () => clearTimeout(t); }, [q]);

  const filters: CdrFilters = useMemo(() => {
    const range = f.period === "today" ? { from: midnight(0) } : f.period === "7d" ? { from: midnight(6) }
      : f.period === "30d" ? { from: midnight(29) }
      : f.period === "custom" ? { from: dayStart(f.from), to: f.to ? (dayStart(f.to) ?? 0) + 86400 : undefined } : {};
    return { q: query, direction: f.direction, answered: f.answered, outcome: f.outcome, line: f.line, test: f.test,
      transcripts: f.transcripts, sort: f.sort, ...range };
  }, [f, query]);

  const load = useCallback(() => {
    getCdr(filters, page, f.perPage)
      .then((r) => { if (r.ok) { setData(r); setError(""); } else setError("the report could not be loaded"); })
      .catch(() => setError("the server is not answering"));
  }, [filters, page, f.perPage]);
  useEffect(() => { load(); return visibleInterval(load, 30000); }, [load]);
  useEffect(() => {
    if (open === null) return;
    setTurns([]);
    getCall(open).then((r) => setTurns(r.call?.turns ?? [])).catch(() => {});
  }, [open]);

  const exportCsv = async () => {
    setBusy(true);
    try { await downloadCdrCsv(filters); } catch { toast("the export failed", "error"); } finally { setBusy(false); }
  };

  const t = data?.totals;
  const pages = data ? Math.max(1, Math.ceil(data.total / f.perPage)) : 1;
  const first = data && data.total ? (page - 1) * f.perPage + 1 : 0;
  const last = data ? Math.min(data.total, page * f.perPage) : 0;

  return (
    <div className="pane">
      <div className="pane-wide">
        <div className="page-head">
          <h1><FileSpreadsheet size={22} aria-hidden /> CDR Report</h1>
          <div className="row wrap" style={{ gap: 8 }}>
            <Button icon={RefreshCw} onClick={load}>Refresh</Button>
            <Button variant="primary" icon={Download} loading={busy} disabled={!data?.total} onClick={exportCsv}>Export CSV</Button>
          </div>
        </div>
        <p className="dim mt-0">Every call the AI made or answered, from Xplogent's own records (not the CRM's).</p>

        <div className="card">
          <div className="form-grid">
            <Field label="Search" hint="Number (any format), name, job, outcome, summary or call ID.">
              {(id) => <input id={id} type="search" value={q} placeholder="e.g. 07700 900123 or Night Care"
                onChange={(e) => setQ(e.target.value)} />}
            </Field>
            <SelectField label="Period" value={f.period} options={PERIODS} onChange={(v) => set({ period: v as Period })} />
            {f.period === "custom" && <>
              <Field label="From">{(id) => <input id={id} type="date" value={f.from} onChange={(e) => set({ from: e.target.value })} />}</Field>
              <Field label="To">{(id) => <input id={id} type="date" value={f.to} onChange={(e) => set({ to: e.target.value })} />}</Field>
            </>}
            <SelectField label="Direction" value={f.direction} onChange={(v) => set({ direction: v })}
              options={[{ value: "", label: "Inbound and outbound" }, { value: "inbound", label: "Inbound" }, { value: "outbound", label: "Outbound" }]} />
            <SelectField label="Answered" value={f.answered} onChange={(v) => set({ answered: v })}
              options={[{ value: "", label: "All calls" }, { value: "yes", label: "Answered by a person" },
                { value: "no", label: "Not answered (no answer, busy, voicemail…)" }]} />
            <SelectField label="Outcome" value={f.outcome} onChange={(v) => set({ outcome: v })}
              options={[{ value: "", label: "Any outcome" }, ...(data?.outcomes ?? []).map((o) => ({ value: o, label: o.replace(/_/g, " ") }))]} />
            <SelectField label="Line" value={f.line} options={LINES} onChange={(v) => set({ line: v })} />
            <SelectField label="Sort" value={f.sort} options={SORTS} onChange={(v) => set({ sort: v })} />
          </div>
          <div className="row wrap mt-3" style={{ gap: 16 }}>
            <Switch checked={f.transcripts} label="Also search what was said" onChange={(on) => set({ transcripts: on })} />
            <Switch checked={f.test} label="Include test calls" onChange={(on) => set({ test: on })} />
          </div>
        </div>

        {t && (
          <div className="grid-stats" aria-label="Totals for these filters">
            <Stat label="Calls" value={t.calls.toLocaleString()} sub={`${t.inbound} in · ${t.outbound} out`} />
            <Stat label="Answered" value={t.calls ? `${Math.round((t.answered / t.calls) * 100)}%` : "—"}
              sub={`${t.answered.toLocaleString()} of ${t.calls.toLocaleString()}`} />
            <Stat label="Talk time" value={hms(t.talk_s)} sub={t.answered ? `average ${hms(t.talk_s / t.answered)}` : "—"} />
            <Stat label="Est. cost" value={`$${t.cost.toFixed(2)}`} sub={t.calls ? `$${(t.cost / t.calls).toFixed(3)} a call` : "—"} />
          </div>
        )}

        {error && <p className="warn">{error}</p>}
        {!data ? <p className="dim">loading…</p> : data.total === 0 ? (
          <div className="card"><EmptyState icon={FileSpreadsheet} title="No calls match">
            Try a longer period, or clear the search and filters.</EmptyState></div>
        ) : (
          <div className="card">
            <div className="table-scroll">
              <table className="data-table">
                <thead><tr>
                  <th aria-label="Direction" /><th>When</th><th>Number</th><th>Name</th><th>Job</th><th>Line</th>
                  <th>Duration</th><th>Outcome</th><th>Ended by</th><th>Est. cost</th>
                </tr></thead>
                <tbody>
                  {data.rows.map((c) => (
                    <Fragment key={c.id}>
                      <tr className="clickable" onClick={() => setOpen(open === c.id ? null : c.id)}>
                        <td>
                          <button type="button" className="linkish" aria-expanded={open === c.id}
                            aria-label={`${open === c.id ? "Hide" : "Show"} call ${c.id}`}
                            onClick={(e) => { e.stopPropagation(); setOpen(open === c.id ? null : c.id); }}>
                            {open === c.id ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                            {c.direction === "inbound" ? <PhoneIncoming size={14} aria-label="inbound" />
                              : <PhoneOutgoing size={14} aria-label="outbound" />}
                          </button>
                        </td>
                        <td className="dim" data-label="When">{fmtTime(c.started_at)}</td>
                        <td data-label="Number" className="mono">{c.number || "—"}</td>
                        <td data-label="Name">
                          <span className="row wrap" style={{ gap: 6 }}>
                            {c.name || <span className="dim">unknown</span>}
                            {!!c.test && <Badge>test</Badge>}
                            {!!c.via_queue && <Badge tone="warn">via queue</Badge>}
                          </span>
                        </td>
                        <td data-label="Job">{c.job || <span className="dim">—</span>}
                          {c.campaign_id ? <span className="dim xsmall"> · campaign {c.campaign_id}</span> : null}</td>
                        <td data-label="Line" className="dim">{c.line_label || "—"}</td>
                        <td data-label="Duration">{c.status === "active" ? <Badge tone="warn">live</Badge> : hms(c.duration_s)}</td>
                        <td data-label="Outcome">
                          <span className="row wrap" style={{ gap: 6 }}>
                            {c.outcome ? <Badge tone={c.answered ? undefined : "warn"}>{c.outcome.replace(/_/g, " ")}</Badge>
                              : <span className="dim">—</span>}
                            {c.crm_status && <Badge tone={CRM_TONE[c.crm_status]} title={c.crm_detail || ""}>CRM {c.crm_status}</Badge>}
                          </span>
                        </td>
                        <td data-label="Ended by" className="dim">{c.end_reason || "—"}</td>
                        <td data-label="Est. cost" className="dim">${(c.cost ?? 0).toFixed(3)}</td>
                      </tr>
                      {open === c.id && (
                        <tr><td colSpan={10}><CallDetail call={c} turns={turns} toast={toast} reload={load} /></td></tr>
                      )}
                    </Fragment>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="row wrap mt-3" style={{ gap: 12, justifyContent: "space-between" }}>
              <span className="dim small">{first.toLocaleString()}–{last.toLocaleString()} of {data.total.toLocaleString()}</span>
              <div className="row wrap" style={{ gap: 8 }}>
                <select aria-label="Rows per page" value={f.perPage} onChange={(e) => set({ perPage: Number(e.target.value) })}>
                  {[25, 50, 100, 200].map((n) => <option key={n} value={n}>{n} a page</option>)}
                </select>
                <Button size="sm" icon={ChevronLeft} disabled={page <= 1} onClick={() => setPage(page - 1)}>Previous</Button>
                <span className="small">Page {page} of {pages}</span>
                <Button size="sm" disabled={page >= pages} onClick={() => setPage(page + 1)}>Next <ChevronRight size={14} aria-hidden /></Button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function Stat({ label, value, sub }: { label: string; value: string; sub: string }) {
  return (
    <div className="card stat">
      <span className="dim small">{label}</span>
      <span className="stat-value">{value}</span>
      <span className="dim xsmall">{sub}</span>
    </div>
  );
}
