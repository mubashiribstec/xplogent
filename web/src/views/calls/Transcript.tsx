// One transcript view for saved calls, live calls and test calls.
export function Transcript({ turns, empty = "Nothing said yet.", live }: {
  turns: { speaker: string; text: string }[]; empty?: string; live?: boolean;
}) {
  if (!turns.length) return <p className="dim small mb-0">{empty}</p>;
  return (
    <div className="transcript" aria-live={live ? "polite" : undefined}>
      {turns.map((t, i) => {
        const agent = t.speaker === "agent";
        return <div key={i} className={`turn ${agent ? "agent" : "caller"}`}><b>{agent ? "Agent" : "Caller"}:</b> {t.text}</div>;
      })}
    </div>
  );
}
