import { useState } from "react";
import { CheckCircle2, XCircle } from "lucide-react";
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
      <button type="button" className="btn sm" onClick={run} disabled={busy}
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
    return <span className="small" role="status" style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
      <CheckCircle2 size={15} className="ok" aria-label="passed" />
      {((res.ms_total ?? 0) / 1000).toFixed(1)} s{res.tool_call ? ` · used ${res.tool_call.name}` : " · answered (no tool call)"}{quota}
    </span>;
  }
  return <span className="small" role="status" style={{ display: "flex", gap: 6, alignItems: "flex-start" }}>
    <XCircle size={15} className="err" style={{ flexShrink: 0, marginTop: 2 }} aria-label="failed" />
    <span>{res.reason}{res.fix ? <> <span className="dim">→ {res.fix}</span></> : null}{quota}</span>
  </span>;
}
