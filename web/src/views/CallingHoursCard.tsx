import { useEffect, useState } from "react";
import { CallingHours, getCallingHours, refreshBankHolidays, saveCallingHours } from "../api";

type Toaster = (m: string, kind?: any) => void;

const REGIONS: [string, string][] = [["england-and-wales", "England and Wales"], ["scotland", "Scotland"],
  ["northern-ireland", "Northern Ireland"]];

/** When outbound calls may be placed: a window per weekday, UK bank holidays, the time zone. */
export function CallingHoursCard({ cfg, toast }: { cfg: Record<string, any>; toast: Toaster }) {
  const [h, setH] = useState<CallingHours | null>(null);
  const ch = cfg.calling_hours ?? {};
  const load = () => getCallingHours().then(setH).catch(() => {});
  useEffect(() => { load(); }, []);
  const save = async (body: Record<string, any>) => {
    const r = await saveCallingHours(body);
    if (r.ok === false) { toast(r.error ?? "could not save", "error"); return; }
    setH(r); toast("calling hours saved", "success");
  };
  if (!h) return null;
  const zone = h.timezone.includes("/") ? h.timezone : undefined;    // shown in the calling zone
  const when = (iso: string | null) => iso
    ? new Date(iso).toLocaleString(undefined, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", timeZone: zone })
    : "nothing open in the next two weeks";
  const why = h.reason.replace(/^outside calling hours:? ?/, "").replace(/^\((.*)\)$/, "$1");
  return (
    <div className="card">
      <h3>Calling hours</h3>
      <p className="explain">Outbound calls (from Call now, contacts and campaigns) are only placed inside these hours,
        in the contact's time zone when it's known. Test calls ignore them.</p>
      <p style={{ fontSize: 13 }}>
        {!h.enabled ? <span className="warn">Off: calls can be placed at any time.</span>
          : h.open_now ? <span className="ok">● Open now</span>
          : <span className="dim">● Closed now ({why}). Next allowed: <b>{when(h.next_allowed)}</b></span>}
        <span className="dim"> · {h.timezone}</span>
      </p>
      <div className="row wrap" style={{ gap: 10 }}>
        <label className="bud"><span className="lbl-sm">Enforce hours</span>
          <select value={h.enabled ? "on" : "off"} onChange={(e) => save({ enabled: e.target.value === "on" })}>
            <option value="on">on</option><option value="off">off (not recommended)</option></select></label>
        <label className="bud"><span className="lbl-sm">Default window</span>
          <span className="row" style={{ gap: 4 }}>
            <input type="time" defaultValue={ch.start ?? "09:00"} onBlur={(e) => save({ start: e.target.value })} />
            –<input type="time" defaultValue={ch.end ?? "20:00"} onBlur={(e) => save({ end: e.target.value })} /></span></label>
        <label className="bud"><span className="lbl-sm">Time zone</span>
          <input defaultValue={ch.timezone ?? "Europe/London"} placeholder="Europe/London" style={{ width: 150 }}
            onBlur={(e) => e.target.value !== (ch.timezone ?? "") && save({ timezone: e.target.value })} /></label>
      </div>
      <div className="table-scroll"><table className="data-table" style={{ marginTop: 8, maxWidth: 520 }}>
        <tbody>
          {h.week.map((d) => {
            const raw = String(ch.days?.[d.day] ?? "");
            return (
              <tr key={d.day}>
                <td style={{ width: 110 }}>{d.name}</td>
                <td style={{ width: 120 }} className={d.window === "closed" ? "dim" : ""}>{d.window}</td>
                <td><input defaultValue={raw} placeholder="closed or 10:00-16:00" style={{ width: 200 }}
                  onBlur={(e) => e.target.value.trim() !== raw && save({ days: { [d.day]: e.target.value.trim() } })} /></td>
              </tr>
            );
          })}
        </tbody>
      </table></div>
      <div className="row wrap" style={{ gap: 10, marginTop: 8 }}>
        <label className="bud"><span className="lbl-sm">UK bank holidays</span>
          <select value={h.bank_holidays} onChange={(e) => save({ bank_holidays: e.target.value })}>
            <option value="closed">no calls</option><option value="open">normal day</option></select></label>
        <label className="bud"><span className="lbl-sm">Region</span>
          <select value={h.bank_holiday_region} onChange={(e) => save({ bank_holiday_region: e.target.value })}>
            {REGIONS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></label>
        <button className="btn ghost sm" onClick={async () => {
          const r = await refreshBankHolidays(); setH(r);
          toast(r.ok ? "bank holidays updated from gov.uk" : `gov.uk not reachable: using the saved list`, r.ok ? "success" : "error");
        }}>Update from gov.uk</button>
      </div>
      {h.upcoming_bank_holidays.length > 0 && <p className="dim" style={{ fontSize: 12 }}>
        Next bank holidays: {h.upcoming_bank_holidays.map((b) => `${b.name} (${new Date(b.date).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" })})`).join(" · ")}
      </p>}
    </div>
  );
}
