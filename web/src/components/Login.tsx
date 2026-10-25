import { useState } from "react";
import { KeyRound, ShieldCheck } from "lucide-react";
import { checkAuth, setToken, type Branding } from "../api";

// Shown when the backend requires an access token and we don't have a valid one.
// Validates the typed token against /auth/check before storing it and reloading.
export function Login({ onAuthed, brand }: { onAuthed: () => void; brand?: Branding | null }) {
  const [value, setValue] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (!value.trim()) return;
    setBusy(true); setError("");
    const res = await checkAuth(value.trim());
    setBusy(false);
    if (res.ok) { setToken(value.trim()); onAuthed(); }
    else setError("That token wasn't accepted. Check `xplogent token`.");
  };

  return (
    <div className="overlay">
      <div className="modal login" onClick={(e) => e.stopPropagation()}>
        {brand?.logo
          ? <img src={`/branding/logo?v=${brand.logo_version}`} alt={brand.company || "logo"} style={{ maxHeight: 56, maxWidth: 200, objectFit: "contain" }} />
          : <div className="login-icon"><ShieldCheck size={28} /></div>}
        <h2>{brand?.company ? `${brand.company}: secure access` : "Secure access"}</h2>
        <p className="dim">This Xplogent instance requires an access token. Generate one with
          <code> xplogent token</code> (or it was printed when the server started).</p>
        <div className="row" style={{ gap: 8, marginTop: 12 }}>
          <KeyRound size={16} className="dim" />
          <input autoFocus type="password" placeholder="paste access token…" value={value}
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && submit()} style={{ flex: 1 }} />
        </div>
        {error && <p className="err" style={{ marginTop: 8 }}>{error}</p>}
        <button className="btn primary" style={{ marginTop: 14, width: "100%" }} disabled={busy} onClick={submit}>
          {busy ? "Checking…" : "Unlock"}</button>
      </div>
    </div>
  );
}
