import { useEffect, useRef, useState } from "react";
import { LayoutGrid, Rocket } from "lucide-react";
import { MonitorSocket, XplogentEvent, orchestrate } from "./api";
import { AgentCard, AgentState } from "./AgentCard";
import { TaskBoard, TaskState } from "./TaskBoard";
import { AgentMsg, MessageFeed } from "./MessageFeed";

export function MissionControl() {
  const [goal, setGoal] = useState("");
  const [maxConcurrent, setMaxConcurrent] = useState(3);
  const [autoApprove, setAutoApprove] = useState(true);
  const [running, setRunning] = useState(false);
  const [runId, setRunId] = useState<string | null>(null);
  const [agents, setAgents] = useState<Record<string, AgentState>>({});
  const [tasks, setTasks] = useState<Record<string, TaskState>>({});
  const [messages, setMessages] = useState<AgentMsg[]>([]);
  const sock = useRef<MonitorSocket | null>(null);
  const [error, setError] = useState("");

  // Stop listening when leaving the view.
  useEffect(() => () => sock.current?.close(), []);

  // ``patch`` may be a function of the current card: the socket handler is created once
  // per launch, so reading ``agents`` from its closure would always see the empty map.
  const upsertAgent = (id: string,
                       patch: Partial<AgentState> | ((cur: AgentState) => Partial<AgentState>)) =>
    setAgents((prev) => {
      const cur = prev[id] ?? {
        agent_id: id, name: id, role: "", status: "running", step: 0, tokens: 0, toolCalls: 0,
      };
      const p = typeof patch === "function" ? patch(cur) : patch;
      return { ...prev, [id]: { ...cur, ...p } };
    });

  const handle = (ev: XplogentEvent) => {
    const id = String(ev.agent_id ?? "");
    switch (ev.type) {
      case "agent_spawn":
        upsertAgent(id, { name: String(ev.agent_name), role: String(ev.role), status: "running" });
        break;
      case "agent_status":
        upsertAgent(id, { status: String(ev.status), step: Number(ev.step ?? 0),
                          current_tool: (ev.current_tool as string) ?? null });
        break;
      case "step_start":
        upsertAgent(id, { step: Number(ev.step ?? 0) + 1, name: String(ev.agent_name) });
        break;
      case "token":
        upsertAgent(id, (cur) => ({ tokens: cur.tokens + Math.ceil(String(ev.text ?? "").length / 4) }));
        break;
      case "tool_call":
        upsertAgent(id, (cur) => ({ toolCalls: cur.toolCalls + 1, current_tool: String(ev.tool) }));
        break;
      case "message":
        upsertAgent(id, { lastMessage: String(ev.content) });
        break;
      case "task_update":
        setTasks((prev) => ({
          ...prev,
          [String(ev.id)]: {
            id: String(ev.id), title: String(ev.title), role: String(ev.role),
            status: String(ev.status), assignee: (ev.assignee as string) ?? null,
          },
        }));
        break;
      case "agent_message":
        setMessages((prev) => [...prev, {
          sender: String(ev.sender), recipient: (ev.recipient as string) ?? null,
          content: String(ev.content),
        }]);
        break;
    }
  };

  const launch = async () => {
    if (!goal.trim() || running) return;
    setAgents({}); setTasks({}); setMessages([]); setError("");
    setRunning(true);
    sock.current?.close();
    try {
      const { run_id } = await orchestrate({
        goal, max_concurrent: maxConcurrent, mode: "auto", auto_approve: autoApprove,
      });
      setRunId(run_id);
      // The server closes the stream when the run finishes: re-enable Launch then.
      sock.current = new MonitorSocket(run_id, handle, () => setRunning(false));
    } catch (e) {
      setError(`could not start the team: ${e instanceof Error ? e.message : String(e)}`);
      setRunning(false);
    }
  };

  return (
    <div className="mission">
      <div className="page-head" style={{ padding: "16px 24px 0" }}>
        <h1 style={{ fontSize: 20 }}><LayoutGrid size={20} /> Mission Control</h1>
        {runId && <span className="dim">run {runId}</span>}
      </div>
      <div className="mission-bar">
        <input
          value={goal}
          placeholder="Give the team a goal…"
          onChange={(e) => setGoal(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && launch()}
        />
        <label className="slider">
          max agents: <b>{maxConcurrent}</b>
          <input type="range" min={1} max={8} value={maxConcurrent}
                 onChange={(e) => setMaxConcurrent(Number(e.target.value))} />
        </label>
        <label className="slider" title="Agents run tools without prompting; critical ops still blocked.">
          <input type="checkbox" checked={autoApprove}
                 onChange={(e) => setAutoApprove(e.target.checked)} /> auto-approve
        </label>
        <button className="btn primary" onClick={launch} disabled={running}>
          <Rocket size={16} /> {running ? "Running…" : "Launch team"}</button>
      </div>
      {error && <p className="dim" style={{ padding: "0 24px", color: "var(--red, #e5484d)" }}>{error}</p>}

      <div className="mission-grid">
        <section>
          <h3>Agents</h3>
          <div className="agent-list">
            {Object.values(agents).length === 0 && <p className="dim">no active agents</p>}
            {Object.values(agents).map((a) => <AgentCard key={a.agent_id} a={a} />)}
          </div>
        </section>
        <section>
          <h3>Tasks</h3>
          <TaskBoard tasks={tasks} />
        </section>
        <section>
          <MessageFeed messages={messages} />
        </section>
      </div>
    </div>
  );
}
