import { useState } from "react";
import { applyUpdate, checkUpdate, health } from "./api";

export function UpdatePanel() {
  const [info, setInfo] = useState<Record<string, any> | null>(null);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<string>("");

  const check = async () => {
    setBusy(true);
    setStatus("Checking…");
    setInfo(await checkUpdate());
    setStatus("");
    setBusy(false);
  };

  const doUpdate = async () => {
    setBusy(true);
    setStatus("Pulling + reinstalling…");
    const res = await applyUpdate();
    if (!res.ok) {
      setStatus(`Update failed: ${res.output ?? res.stage}`);
      setBusy(false);
      return;
    }
    setStatus("Restarting backend…");
    // Poll /health until the server is back, then reload.
    const wait = async () => {
      for (let i = 0; i < 60; i++) {
        await new Promise((r) => setTimeout(r, 1500));
        if (await health()) {
          location.reload();
          return;
        }
      }
      setStatus("Restarted — reload the page if it didn't already.");
      setBusy(false);
    };
    wait();
  };

  return (
    <div className="card">
      <h3>Update</h3>
      {!info && <p className="dim">Check whether a newer version is available.</p>}
      {info && !info.git && (info.docker
        ? <p className="warn">Running in Docker{info.version ? ` (${info.version})` : ""}. Update on the server with <code>xplogent-docker update</code>.</p>
        : <p className="warn">Not a git checkout — update via your installer.</p>)}
      {info && info.git && info.update_available && (
        <div>
          <p>{info.behind_by} new commit(s) available:</p>
          <pre className="changelog">{info.changelog}</pre>
        </div>
      )}
      {info && info.git && !info.update_available && (
        <p className="ok">Up to date ({info.current}).</p>
      )}
      {status && <p className="dim">{status}</p>}
      <div className="row">
        <button onClick={check} disabled={busy}>Check for updates</button>
        {info?.update_available && (
          <button className="primary" onClick={doUpdate} disabled={busy}>Update &amp; restart</button>
        )}
      </div>
    </div>
  );
}
