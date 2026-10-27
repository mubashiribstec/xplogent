import { useState } from "react";
import { ModelTest, testModel } from "../api";

// One request the way a call sends it; shows time, the tool call, OpenRouter's free
// requests left today, or the real reason it failed and what to do about it.
export function ModelTestButton({ spec, label = "Test" }: { spec: string; label?: string }) {
  const [busy, setBusy] = useState(false);
  const [res, setRes] = useState<ModelTest | null>(null);
  const run = async () => {
    setBusy(true); setRes(null);
    try { setRes(await testModel(spec)); }
    catch (e: any) { setRes({ spec, ok: false, reason: String(e?.message || e), fix: "Is the server running?" }); }
    finally { setBusy(false); }
  };
  return (
    <span className="model-test">
      <button className="btn" onClick={run} disabled={busy}
        title={spec.endsWith(":free") ? "Sends one request: uses 1 of your daily free OpenRouter requests" : "Sends one short request to the model"}>
        {busy ? "Testing…" : label}</button>
      {res && <ModelTestResult res={res} />}
    </span>
  );
}

export function ModelTestResult({ res }: { res: ModelTest }) {
  const q = res.quota;
  const quota = q && q.free_limit ? ` · free requests today ${q.free_used ?? 0}/${q.free_limit}` : "";
  if (res.ok) {
    return <span className="ok" style={{ fontSize: 12 }}>
      ✓ {((res.ms_total ?? 0) / 1000).toFixed(1)} s{res.tool_call ? ` · used ${res.tool_call.name} ✓` : " · answered (no tool call)"}{quota}
    </span>;
  }
  return <span className="err" style={{ fontSize: 12, display: "block" }}>
    ✗ {res.reason}{res.fix ? <> <span className="dim">→ {res.fix}</span></> : null}{quota}
  </span>;
}
