import { useEffect, useRef, useState } from "react";
import { Branding, deleteLogo, getBranding, saveBranding, uploadLogo } from "../api";
import { BRANDING_EVENT } from "../lib/brand";

type Toaster = (m: string, kind?: any) => void;

/** White-label-lite: company name, logo and accent colour on the dashboard, login page and tour. */
export function BrandingCard({ toast }: { toast: Toaster }) {
  const [b, setB] = useState<Branding | null>(null);
  const file = useRef<HTMLInputElement>(null);
  useEffect(() => { getBranding().then(setB).catch(() => {}); }, []);
  const changed = (next: Branding & { ok?: boolean; error?: string }) => {
    if (next.ok === false) { toast(next.error ?? "could not save", "error"); return; }
    setB(next); window.dispatchEvent(new Event(BRANDING_EVENT)); toast("branding saved", "success");
  };
  if (!b) return null;
  return (
    <div className="card" data-tour="branding">
      <h3>Branding</h3>
      <p className="explain">Make the dashboard yours: your name, logo and colour appear in the menu, on the login page and in the tour.</p>
      <div className="row wrap" style={{ gap: 12, alignItems: "flex-end" }}>
        <label className="bud"><span className="lbl-sm">Company name</span>
          <input defaultValue={b.company} key={b.company} placeholder="Your company"
            onBlur={(e) => e.target.value.trim() !== b.company && saveBranding({ company: e.target.value }).then(changed)} /></label>
        <label className="bud"><span className="lbl-sm">Accent colour</span>
          <span className="row" style={{ gap: 6 }}>
            <input type="color" value={b.accent || "#4c8dff"} style={{ width: 44, padding: 2 }}
              onChange={(e) => saveBranding({ accent: e.target.value }).then(changed)} />
            {b.accent && <button className="btn ghost sm" onClick={() => saveBranding({ accent: "" }).then(changed)}>default</button>}
          </span></label>
        <div>
          <span className="lbl-sm dim">Logo (PNG, JPEG or WebP, up to 512 KB)</span>
          <div className="row" style={{ gap: 8, marginTop: 4 }}>
            {b.logo && <img src={`/branding/logo?v=${b.logo_version}`} alt="logo" style={{ height: 36, maxWidth: 120, objectFit: "contain" }} />}
            <input ref={file} type="file" accept="image/png,image/jpeg,image/webp" style={{ display: "none" }}
              onChange={async (e) => { const f = e.target.files?.[0]; if (f) changed(await uploadLogo(f)); e.target.value = ""; }} />
            <button className="btn sm" onClick={() => file.current?.click()}>{b.logo ? "Replace" : "Upload logo"}</button>
            {b.logo && <button className="btn ghost sm" onClick={async () => { await deleteLogo(); changed({ ...b, logo: false }); }}>Remove</button>}
          </div>
        </div>
      </div>
    </div>
  );
}
