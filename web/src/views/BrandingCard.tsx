import { useEffect, useRef, useState } from "react";
import { Branding, deleteLogo, getBranding, saveBranding, uploadLogo } from "../api";
import { BRANDING_EVENT } from "../lib/brand";
import { useConfirm } from "../ui/confirm";
import { Button, Card, Field, TextField } from "../ui";

type Toaster = (m: string, kind?: any) => void;

/** White-label-lite: company name, logo and accent colour on the dashboard, login page and tour. */
export function BrandingCard({ toast }: { toast: Toaster }) {
  const [b, setB] = useState<Branding | null>(null);
  const file = useRef<HTMLInputElement>(null);
  const confirm = useConfirm();
  useEffect(() => { getBranding().then(setB).catch(() => {}); }, []);
  const changed = (next: Branding & { ok?: boolean; error?: string }) => {
    if (next.ok === false) { toast(next.error ?? "could not save", "error"); return; }
    setB(next); window.dispatchEvent(new Event(BRANDING_EVENT)); toast("branding saved", "success");
  };
  if (!b) return null;
  return (
    <Card data-tour="branding" title="Branding"
      description="Make the dashboard yours: your name, logo and colour appear in the menu, on the login page and in the tour.">
      <div className="form-grid">
        <TextField label="Company name" value={b.company} placeholder="Your company"
          onCommit={(v) => { if (v.trim() !== b.company) saveBranding({ company: v }).then(changed); }} />
        <Field label="Accent colour" hint="Adjusted automatically so text stays readable in light and dark.">
          {(id) => <div className="fld-row">
            <input id={id} type="color" value={b.accent || "#4f46e5"} onChange={(e) => saveBranding({ accent: e.target.value }).then(changed)} />
            {b.accent && <Button size="sm" variant="ghost" onClick={() => saveBranding({ accent: "" }).then(changed)}>Use the default</Button>}
          </div>}
        </Field>
        <Field label="Logo" hint="PNG, JPEG or WebP, up to 512 KB.">
          {(id) => <div className="fld-row">
            {b.logo && <img src={`/branding/logo?v=${b.logo_version}`} alt="Current logo" style={{ height: 36, maxWidth: 120, objectFit: "contain" }} />}
            <input id={id} ref={file} type="file" accept="image/png,image/jpeg,image/webp" style={{ display: "none" }}
              onChange={async (e) => { const f = e.target.files?.[0]; if (f) changed(await uploadLogo(f)); e.target.value = ""; }} />
            <Button size="sm" onClick={() => file.current?.click()}>{b.logo ? "Replace" : "Upload logo"}</Button>
            {b.logo && <Button size="sm" variant="ghost" onClick={async () => {
              if (!(await confirm({ title: "Remove the logo?", body: "The menu shows your initial instead.", confirmLabel: "Remove", danger: true }))) return;
              await deleteLogo(); changed({ ...b, logo: false });
            }}>Remove</Button>}
          </div>}
        </Field>
      </div>
    </Card>
  );
}
