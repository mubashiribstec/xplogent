"""Xplogent CLI/TUI.

Commands:
    xplogent chat                 interactive agent session (streaming + inline approvals)
    xplogent run "<task>"         run a single task and exit
    xplogent model <provider:model>   set the active model
    xplogent memory search <q>    search episodic memory and facts
    xplogent skills list          list learned skills
    xplogent providers            list available providers
    xplogent serve                start the REST + WebSocket API
    xplogent orchestrate "<goal>" run a multi-agent team on a goal
    xplogent team --agent ...     run named agents concurrently
"""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from xplogent.core.config import load_config, save_env, save_user_config
from xplogent.core.events import EventBus, EventType
from xplogent.core.orchestrator import AgentSpec
from xplogent.memory.store import Store
from xplogent.providers.registry import available_providers
from xplogent.runtime import build_orchestrator, build_runtime
from xplogent.safety.approval import ApprovalRequest

app = typer.Typer(add_completion=False, help="Xplogent — your self-improving AI agent.")
console = Console()


# ── shared helpers ────────────────────────────────────────────────────────────
def _make_approver():
    async def approve(req: ApprovalRequest) -> bool:
        console.print(
            Panel(
                # Model-chosen text must not be able to restyle/hide parts of what
                # the user is approving (e.g. "[black on black]...[/]").
                f"[bold]{escape(req.tool)}[/]  risk=[yellow]{req.risk.value}[/]\n"
                f"{escape(req.reason or '')}\n\n{escape(str(req.arguments))}",
                title="⚠ approval required",
                border_style="yellow",
            )
        )
        ans = await asyncio.to_thread(console.input, "[bold yellow]approve? \\[y/N][/] ")
        return ans.strip().lower() in {"y", "yes"}

    return approve


async def _render_events(bus: EventBus) -> None:
    async for ev in bus.stream():
        if ev.type == EventType.TOKEN:
            console.print(ev.data.get("text", ""), end="", markup=False, highlight=False)
        elif ev.type == EventType.MESSAGE:
            console.print()  # newline after streamed tokens
        elif ev.type == EventType.TOOL_CALL:
            console.print(f"\n[cyan]→ {escape(str(ev.data['tool']))}[/] "
                          f"{escape(str(ev.data.get('arguments', {})))}")
        elif ev.type == EventType.TOOL_RESULT:
            ok = ev.data.get("ok")
            mark = "[green]✓[/]" if ok else "[red]✗[/]"
            out = str(ev.data.get("output", ""))
            console.print(f"{mark} [dim]{escape(out[:500])}[/]")
        elif ev.type == EventType.MEMORY:
            console.print(
                f"[dim]🧠 recalled {ev.data.get('facts',0)} facts, "
                f"{ev.data.get('skills',0)} skills[/]"
            )
        elif ev.type == EventType.SKILL:
            skill = ev.data.get("skill")
            console.print(
                f"[magenta]✨ learned {ev.data.get('facts',0)} fact(s)"
                + (f", new skill '{skill}'" if skill else "")
                + "[/]"
            )
        elif ev.type == EventType.ERROR:
            console.print(f"[red]error:[/] {escape(str(ev.data.get('message')))}")


# ── commands ──────────────────────────────────────────────────────────────────
@app.command()
def chat() -> None:
    """Start an interactive agent session."""
    asyncio.run(_chat_loop())


async def _chat_loop() -> None:
    bus = EventBus()
    runtime = build_runtime(bus=bus, approve=_make_approver())
    console.print(
        Panel(
            f"Model: [bold]{runtime.config.model}[/]\n"
            "Type your task. Commands: /exit, /model <name>, /skills",
            title="🧠 Xplogent",
            border_style="cyan",
        )
    )
    consumer = asyncio.create_task(_render_events(bus))
    try:
        while True:
            user = await asyncio.to_thread(console.input, "\n[bold green]you›[/] ")
            user = user.strip()
            if not user:
                continue
            if user in {"/exit", "/quit", "exit", "quit"}:
                break
            if user == "/skills":
                _print_skills(runtime.store)
                continue
            if user.startswith("/model "):
                _set_model(user.split(" ", 1)[1].strip())
                console.print("[dim]restart chat to apply[/]")
                continue
            console.print("[bold blue]xplogent›[/] ", end="")
            await runtime.agent.run(user)
    finally:
        await bus.close()
        await consumer
        await runtime.aclose()
        console.print("\n[dim]session saved. bye![/]")


@app.command()
def run(task: str) -> None:
    """Run a single task non-interactively and print the answer."""

    async def _go() -> None:
        bus = EventBus()
        runtime = build_runtime(bus=bus, approve=_make_approver())
        consumer = asyncio.create_task(_render_events(bus))
        answer = await runtime.agent.run(task)
        await bus.close()
        await consumer
        await runtime.aclose()
        console.print(Panel(escape(answer or "(no answer)"), title="answer", border_style="green"))

    asyncio.run(_go())


@app.command()
def model(spec: str) -> None:
    """Set the active model, e.g. 'ollama:llama3.1' or 'openai:gpt-4o'."""
    _set_model(spec)
    console.print(f"[green]model set to[/] {spec}")


@app.command()
def providers() -> None:
    """List available providers."""
    for name in available_providers():
        console.print(f"• {name}")


@app.command(name="serve")
def serve(host: str | None = None, port: int | None = None) -> None:
    """Start the REST + WebSocket API server (needs the 'api' extra)."""
    from xplogent.interfaces.api.server import run_server

    run_server(host=host, port=port)


@app.command()
def up(port: int = 8765, host: str = "127.0.0.1", no_browser: bool = False) -> None:
    """Start the backend, serve the dashboard, and open it in your browser."""
    from xplogent.interfaces.api.server import run_server

    console.print(Panel(f"Xplogent is starting on http://{host}:{port}",
                        title="🚀 xplogent up", border_style="cyan"))
    run_server(host=host, port=port, open_browser=not no_browser)


@app.command()
def token(clear: bool = typer.Option(False, "--clear", help="Disable the access token.")) -> None:
    """Generate (or clear) the dashboard access token for remote/secure access."""
    if clear:
        save_user_config({"server": {"auth_token": ""}})
        console.print("[yellow]access token cleared[/] — the dashboard is now open on this host.")
        return
    import secrets as _pysecrets

    tok = _pysecrets.token_urlsafe(32)
    save_user_config({"server": {"auth_token": tok}})
    console.print(Panel(tok, title="🔐 dashboard access token", border_style="green"))
    console.print("[dim]Paste this when the dashboard prompts. Re-run to rotate; "
                  "`xplogent token --clear` to disable.[/]")


@app.command()
def start(port: int = 8765, host: str = "127.0.0.1") -> None:
    """Run Xplogent in the background (survives closing the terminal)."""
    import time as _time

    from xplogent.core import service

    res = service.start(port=port, host=host)
    if res.get("already_running"):
        console.print(f"[yellow]already running[/] (pid {res.get('pid')}) on :{res.get('port')}")
        return
    if not res.get("ok"):
        console.print(f"[red]failed to start:[/] {res.get('error', 'unknown error')}")
        if res.get("log"):
            console.print(Panel(res["log"], title="server.log (last lines)", border_style="red"))
        raise typer.Exit(1)

    console.print(f"[green]started[/] pid {res.get('pid')} → http://{host}:{port}")
    # Poll until the server answers /health (or give up after ~6s).
    for _ in range(12):
        if service.status().get("healthy"):
            console.print("[green]healthy[/] — dashboard ready.")
            break
        _time.sleep(0.5)
    else:
        console.print("[yellow]starting…[/] (still booting; check 'xplogent status')")
    console.print("[dim]use 'xplogent stop' to stop, 'xplogent status' to check.[/]")


@app.command()
def stop() -> None:
    """Stop the background Xplogent server."""
    from xplogent.core import service

    res = service.stop()
    console.print("[green]stopped[/]" if res.get("stopped") else f"[dim]{res.get('reason','not running')}[/]")


@app.command()
def status() -> None:
    """Show whether the background server is running."""
    from xplogent.core import service

    s = service.status()
    if s.get("running"):
        health = "[green]healthy[/]" if s.get("healthy") else "[yellow]starting[/]"
        console.print(f"running (pid {s.get('pid')}) on :{s.get('port')} — {health}")
    else:
        console.print("[dim]not running[/]")


@app.command()
def restart(port: int = 8765, host: str = "127.0.0.1") -> None:
    """Restart the background server."""
    from xplogent.core import service

    service.restart(port=port, host=host)
    console.print("[green]restarted[/]")


@app.command()
def service(action: str) -> None:
    """Install/uninstall an OS service for boot auto-start: install | uninstall."""
    from xplogent.core import service as svc

    if action == "install":
        res = svc.install_service()
        console.print(res)
    elif action == "uninstall":
        console.print(svc.uninstall_service())
    else:
        console.print("[red]use: xplogent service install | uninstall[/]")


@app.command()
def setup() -> None:
    """Interactive first-run wizard: pick a provider/model and save settings."""
    console.print(Panel("Let's set up Xplogent.", title="⚙ setup", border_style="cyan"))
    provider = console.input(
        "Provider [bold]\\[ollama][/] / openai / anthropic / openrouter: "
    ).strip() or "ollama"
    defaults = {
        "ollama": "llama3.1", "openai": "gpt-4o",
        "anthropic": "claude-sonnet-4-6", "openrouter": "meta-llama/llama-3.1-70b-instruct",
    }
    model = console.input(f"Model [bold]\\[{defaults.get(provider, '')}][/]: ").strip() \
        or defaults.get(provider, "")
    save_user_config({"model": f"{provider}:{model}"})

    if provider != "ollama":
        key_env = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
                   "openrouter": "OPENROUTER_API_KEY"}.get(provider)
        if key_env:
            key = console.input(f"{key_env} (leave blank to skip): ").strip()
            if key:
                save_env({key_env: key})
    else:
        console.print("[dim]Ollama selected. Make sure it's running:[/] "
                      f"ollama pull {model} && ollama pull nomic-embed-text")

    console.print("[green]Saved.[/] Run [bold]xplogent up[/] to launch the dashboard.")


@app.command()
def update() -> None:
    """Update Xplogent from its git repo (pull + reinstall)."""
    from xplogent.core import updater

    check = updater.check_update()
    if not check.get("git"):
        console.print(f"[red]{check.get('error', 'not a git checkout')}[/]")
        raise typer.Exit(1)
    if not check["update_available"]:
        console.print(f"[green]Already up to date[/] ({check['current']}).")
        return
    console.print(f"[cyan]{check['behind_by']} new commit(s):[/]\n{check['changelog']}")
    # Safety net: snapshot data before changing code (same as the GUI update).
    try:
        from xplogent.core.backup import create_backup
        bak = create_backup()
        console.print(f"[dim]backed up to {bak['path']}[/]")
    except Exception as exc:  # noqa: BLE001
        console.print(f"[yellow]backup skipped: {exc}[/]")
    pulled = updater.pull()
    console.print(pulled["output"])
    if pulled["ok"]:
        console.print("[cyan]reinstalling…[/]")
        updater.reinstall()
        console.print("[cyan]rebuilding dashboard…[/]")
        web = updater.rebuild_web()
        if web.get("skipped"):
            console.print(f"[dim]dashboard: {web['skipped']}[/]")
        console.print("[green]Updated.[/] Restart [bold]xplogent up[/] (or 'xplogent restart') to apply.")


@app.command()
def backup(out: str = typer.Option("", help="Output .tar.gz path (default: ~/.xplogent/backups)."),
           include_secrets: bool = typer.Option(False, help="Also back up API keys (.env).")) -> None:
    """Back up the database, skills, and config to a .tar.gz."""
    from xplogent.core.backup import create_backup

    res = create_backup(out or None, include_secrets=include_secrets)
    console.print(f"[green]backup written[/] {res['path']} ({res['size']} bytes)")


@app.command()
def restore(path: str) -> None:
    """Restore from a backup .tar.gz (overwrites current data)."""
    from xplogent.core.backup import restore_backup

    res = restore_backup(path)
    console.print(res if res["ok"] else f"[red]{res.get('error')}[/]")


@app.command()
def mcp(
    transport: str = typer.Option("stdio", help="stdio | streamable-http | sse"),
    host: str = "127.0.0.1",
    port: int = 8766,
    role: str | None = typer.Option(None, help="Role profile for MCP-driven runs."),
    auto_approve: bool = typer.Option(False, "--auto-approve",
                                      help="Allow confirm-tier actions without a human."),
) -> None:
    """Run Xplogent as an MCP server (needs the 'mcp' extra)."""
    from xplogent.interfaces.mcp_server import run_server

    run_server(transport=transport, host=host, port=port,
               role=role, auto_approve=auto_approve if auto_approve else None)


@app.command()
def voice(seconds: int = 6) -> None:
    """Talk to Xplogent (needs the 'voice' extra)."""
    from xplogent.interfaces.voice.audio import voice_loop

    async def _go() -> None:
        bus = EventBus()
        runtime = build_runtime(bus=bus, approve=_make_approver())
        try:
            await voice_loop(runtime, seconds=seconds)
        finally:
            await bus.close()
            await runtime.aclose()

    asyncio.run(_go())


# ── multi-agent ───────────────────────────────────────────────────────────────
async def _render_multi(bus: EventBus) -> None:
    """Concise per-agent rendering for multi-agent runs (token streams omitted)."""
    async for ev in bus.stream():
        d = ev.data
        who = d.get("agent_name", "?")
        if ev.type == EventType.RUN_PROGRESS:
            console.print(f"[bold cyan]🧭 planned {d.get('planned_tasks')} subtasks[/]")
        elif ev.type == EventType.AGENT_SPAWN:
            console.print(f"[green]🟢 {who}[/] ([dim]{d.get('role')}[/]) started")
        elif ev.type == EventType.TASK_UPDATE:
            console.print(f"[blue]📋 {escape(str(d.get('title')))}[/]: {d.get('status')}")
        elif ev.type == EventType.AGENT_MESSAGE:
            to = d.get("recipient") or "all"
            console.print(f"[magenta]💬 {escape(str(d.get('sender')))} → {escape(str(to))}:[/] "
                          f"{escape(str(d.get('content')))}")
        elif ev.type == EventType.TOOL_CALL:
            console.print(f"   [cyan]{who} → {d.get('tool')}[/]")
        elif ev.type == EventType.TOOL_RESULT:
            mark = "[green]✓[/]" if d.get("ok") else "[red]✗[/]"
            console.print(f"   {mark} [dim]{escape(str(d.get('output',''))[:200])}[/]")
        elif ev.type == EventType.MESSAGE:
            console.print(f"[bold]{who}:[/] {escape(str(d.get('content')))}")
        elif ev.type == EventType.SKILL:
            console.print(f"[magenta]✨ {who} learned {d.get('facts',0)} fact(s)[/]")
        elif ev.type == EventType.ERROR:
            console.print(f"[red]error ({who}):[/] {d.get('message')}")


def _print_taskboard(tasks: list[dict]) -> None:
    if not tasks:
        return
    table = Table(title="task board", show_lines=False)
    table.add_column("task")
    table.add_column("role")
    table.add_column("status")
    for t in tasks:
        color = {"done": "green", "failed": "red", "active": "yellow"}.get(t["status"], "white")
        table.add_row(t["title"], t["role"], f"[{color}]{t['status']}[/]")
    console.print(table)


@app.command()
def orchestrate(goal: str, max: int = 0, mode: str = "auto") -> None:
    """Run a multi-agent team on a goal (auto-decomposed into subtasks)."""

    async def _go() -> None:
        bus = EventBus()
        runtime = build_orchestrator(bus=bus, approve=_make_approver())
        console.print(Panel(f"Goal: [bold]{escape(goal)}[/]\nmax concurrent: "
                            f"{max or runtime.orchestrator.default_max}",
                            title="🧠 Xplogent orchestrator", border_style="cyan"))
        consumer = asyncio.create_task(_render_multi(bus))
        result = await runtime.orchestrator.run_goal(goal, max_concurrent=max or None, mode=mode)
        await bus.close()
        await consumer
        _print_taskboard(result.get("tasks", []))
        console.print(f"[dim]peak concurrency: {result.get('peak_concurrency')} · "
                      f"run {result.get('run_id')}[/]")
        await runtime.aclose()

    asyncio.run(_go())


@app.command()
def team(agent: list[str] = typer.Option(None, "--agent", "-a",
         help="Repeatable. Format: name:role:task"), max: int = 0) -> None:
    """Run named agents concurrently. Example:
    xplogent team -a "researcher:researcher:find facts about X" -a "writer:coder:write a summary"
    """
    if not agent:
        console.print("[red]provide at least one --agent name:role:task[/]")
        raise typer.Exit(1)
    specs = []
    for spec in agent:
        parts = spec.split(":", 2)
        if len(parts) != 3:
            console.print(f"[red]bad --agent '{spec}', expected name:role:task[/]")
            raise typer.Exit(1)
        specs.append(AgentSpec(name=parts[0], role=parts[1], task=parts[2]))

    async def _go() -> None:
        bus = EventBus()
        runtime = build_orchestrator(bus=bus, approve=_make_approver())
        consumer = asyncio.create_task(_render_multi(bus))
        result = await runtime.orchestrator.run_team(specs, max_concurrent=max or None)
        await bus.close()
        await consumer
        for name, answer in result.get("results", {}).items():
            console.print(Panel(answer or "(no answer)", title=name, border_style="green"))
        await runtime.aclose()

    asyncio.run(_go())


memory_app = typer.Typer(help="Inspect memory.")
skills_app = typer.Typer(help="Inspect learned skills.")
schedule_app = typer.Typer(help="Schedule recurring / timed agent jobs.")
knowledge_app = typer.Typer(help="Export/import learned facts + skills (JSON).")
docs_app = typer.Typer(help="Ingest documents for RAG (the agent answers from them).")
evals_app = typer.Typer(help="Run agent eval suites (LLM-judged quality checks).")
app.add_typer(memory_app, name="memory")
app.add_typer(skills_app, name="skills")
app.add_typer(schedule_app, name="schedule")
app.add_typer(knowledge_app, name="knowledge")
app.add_typer(docs_app, name="docs")
app.add_typer(evals_app, name="evals")


@evals_app.command("list")
def evals_list() -> None:
    """List eval suites and their latest pass-rate."""
    store = Store(load_config().db_path)
    suites = store.list_evals()
    store.close()
    if not suites:
        console.print("[dim]no eval suites — create one in the dashboard[/]")
        return
    for s in suites:
        last = s["runs"][0] if s["runs"] else None
        rate = f"{last['passed']}/{last['total']}" if last else "never run"
        console.print(f"[cyan]#{s['id']}[/] {s['name']} [dim]({len(s['cases'])} cases)[/] — {rate}")


@evals_app.command("run")
def evals_run(eval_id: int) -> None:
    """Run a suite by id and print the score."""
    from xplogent.core.evals import run_suite

    res = asyncio.run(run_suite(eval_id))
    if res.get("ok") is False:
        console.print(f"[red]{res.get('error')}[/]")
        return
    color = "green" if res["passed"] == res["total"] else "yellow"
    console.print(f"[{color}]{res['passed']}/{res['total']} passed[/] · score {res['score']}")
    for r in res["results"]:
        mark = "[green]✓[/]" if r["passed"] else "[red]✗[/]"
        console.print(f"  {mark} {r['prompt'][:60]} [dim]— {r['reason']}[/]")


@evals_app.command("gate")
def evals_gate(
    eval_id: int = typer.Option(None, "--suite", help="Gate one suite (default: all suites)."),
    min_score: float = typer.Option(0.8, "--min-score", help="Fail below this average score."),
) -> None:
    """CI gate: run suite(s) and exit non-zero if any scores below the threshold.

    Use in CI to block regressions, e.g. `xplogent evals gate --min-score 0.9`.
    """
    from xplogent.core.evals import run_suite

    store = Store(load_config().db_path)
    ids = [eval_id] if eval_id is not None else [s["id"] for s in store.list_evals()]
    names = {s["id"]: s["name"] for s in store.list_evals()}
    store.close()
    if not ids:
        console.print("[yellow]no eval suites to gate[/]")
        raise typer.Exit(0)

    failed = False
    for sid in ids:
        res = asyncio.run(run_suite(sid))
        ok = res["score"] >= min_score
        failed = failed or not ok
        tag = "[green]PASS[/]" if ok else "[red]FAIL[/]"
        console.print(f"{tag} suite '{names.get(sid, sid)}' — score {res['score']} "
                      f"({res['passed']}/{res['total']}) vs min {min_score}")
    if failed:
        console.print("[red]eval gate failed[/]")
        raise typer.Exit(1)
    console.print("[green]eval gate passed[/]")


@docs_app.command("ingest")
def docs_ingest(path: str) -> None:
    """Ingest a file or folder into the document store."""
    from xplogent.core.rag import ingest_path
    from xplogent.memory.vector import Embedder
    from xplogent.providers.registry import build_provider

    cfg = load_config()
    store = Store(cfg.db_path)

    async def _go() -> dict:
        ep = build_provider(cfg.embedding_model)
        try:
            return await ingest_path(store, Embedder(ep), path)
        finally:
            await ep.aclose()
            store.close()

    res = asyncio.run(_go())
    if res.get("ok"):
        console.print(f"[green]ingested[/] {len(res['ingested'])} file(s), "
                      f"{res['chunks']} chunks ([dim]{res['skipped']} skipped[/])")
    else:
        console.print(f"[red]{res.get('error')}[/]")


@docs_app.command("list")
def docs_list() -> None:
    """List ingested documents."""
    cfg = load_config()
    store = Store(cfg.db_path)
    docs = store.list_documents()
    store.close()
    if not docs:
        console.print("[dim]no documents ingested[/]")
        return
    for d in docs:
        console.print(f"[cyan]#{d['id']}[/] {d['title']} [dim]({d['chunks']} chunks)[/]")


@knowledge_app.command("export")
def knowledge_export(out: str) -> None:
    """Export facts + skills (with embeddings) to a JSON file."""
    import json

    from xplogent.core.backup import export_knowledge

    store = Store(load_config().db_path)
    data = export_knowledge(store)
    store.close()
    from pathlib import Path
    Path(out).write_text(json.dumps(data, indent=2), encoding="utf-8")
    console.print(f"[green]exported[/] {len(data['facts'])} facts, "
                  f"{len(data['skills'])} skills → {out}")


@knowledge_app.command("import")
def knowledge_import(path: str) -> None:
    """Import facts + skills from a JSON export (merges; skips duplicates)."""
    import json
    from pathlib import Path

    from xplogent.core.backup import import_knowledge

    store = Store(load_config().db_path)
    res = import_knowledge(store, json.loads(Path(path).read_text(encoding="utf-8")))
    store.close()
    console.print(f"[green]imported[/] +{res['facts_added']} facts, +{res['skills_added']} skills")
    for w in res.get("warnings", []):
        console.print(f"[yellow]⚠ {w}[/]")


@schedule_app.command("add")
def schedule_add(
    when: str = typer.Argument(..., help='e.g. "every day at 9am" or a 5-field cron'),
    prompt: str = typer.Argument(..., help="What the agent should do."),
    mode: str = typer.Option("agent", help="agent | team"),
    name: str = typer.Option("", help="Optional label."),
    tz: str = typer.Option("", help="IANA timezone, e.g. Europe/London (default: local)."),
) -> None:
    """Add a scheduled job. Example:
    xplogent schedule add "every day at 9am" "summarize my unread email"
    """
    import time

    from xplogent.core.scheduler import parse_schedule

    try:
        spec, next_run = parse_schedule(when, tz)
    except ValueError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc
    store = Store(load_config().db_path)
    sid = store.add_schedule(name or prompt[:40], prompt, mode, spec, tz, next_run)
    store.close()
    console.print(f"[green]scheduled[/] #{sid} ({spec}) — next run "
                  f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(next_run))}")


@schedule_app.command("list")
def schedule_list() -> None:
    """List scheduled jobs."""
    import time

    store = Store(load_config().db_path)
    jobs = store.list_schedules()
    store.close()
    if not jobs:
        console.print("[dim]no schedules[/]")
        return
    table = Table(title="schedules")
    table.add_column("id")
    table.add_column("name")
    table.add_column("spec")
    table.add_column("on")
    table.add_column("next run")
    for j in jobs:
        nxt = time.strftime("%Y-%m-%d %H:%M", time.localtime(j["next_run"])) if j["next_run"] else "—"
        on = "[green]yes[/]" if j["enabled"] else "[dim]no[/]"
        table.add_row(str(j["id"]), j["name"], j["spec"], on, nxt)
    console.print(table)


@schedule_app.command("remove")
def schedule_remove(schedule_id: int) -> None:
    """Delete a scheduled job by id."""
    store = Store(load_config().db_path)
    store.delete_schedule(schedule_id)
    store.close()
    console.print(f"[green]removed[/] #{schedule_id}")


@schedule_app.command("toggle")
def schedule_toggle(schedule_id: int) -> None:
    """Enable/disable a scheduled job by id."""
    store = Store(load_config().db_path)
    job = store.get_schedule(schedule_id)
    if not job:
        console.print("[red]no such schedule[/]")
        store.close()
        raise typer.Exit(1)
    store.set_schedule_enabled(schedule_id, not job["enabled"])
    store.close()
    console.print(f"[green]{'disabled' if job['enabled'] else 'enabled'}[/] #{schedule_id}")


@memory_app.command("search")
def memory_search(query: str) -> None:
    """Search episodic messages and stored facts."""
    cfg = load_config()
    store = Store(cfg.db_path)
    facts = [f for f in store.all_facts() if query.lower() in f.content.lower()]
    msgs = store.search_messages(query, limit=10)
    console.print(f"[bold]Facts ({len(facts)})[/]")
    for f in facts:
        console.print(f"  • {f.content}")
    console.print(f"[bold]Messages ({len(msgs)})[/]")
    for m in msgs:
        console.print(f"  [{m['role']}] {m['content'][:120]}")
    store.close()


@skills_app.command("list")
def skills_list() -> None:
    """List learned skills."""
    cfg = load_config()
    store = Store(cfg.db_path)
    _print_skills(store)
    store.close()


@skills_app.command("packs")
def skills_packs() -> None:
    """List the bundled starter skill packs you can install."""
    from xplogent.skills.hub import list_bundled

    packs = list_bundled()
    if not packs:
        console.print("[dim]no bundled packs found[/]")
        return
    for p in packs:
        console.print(f"[magenta]{p['name']}[/] — {p['description']}")


@skills_app.command("install")
def skills_install(src: str) -> None:
    """Install a skill pack from a bundled name, a path, or an http(s) URL."""
    from xplogent.memory.manager import MemoryManager
    from xplogent.memory.vector import Embedder
    from xplogent.providers.registry import build_provider
    from xplogent.skills.hub import install_pack

    cfg = load_config()
    store = Store(cfg.db_path)

    async def _go() -> dict:
        ep = build_provider(cfg.embedding_model)
        mem = MemoryManager(store, Embedder(ep))
        try:
            return await install_pack(src, mem)
        finally:
            await ep.aclose()
            store.close()

    res = asyncio.run(_go())
    if res.get("ok"):
        console.print(f"[green]installed[/] {', '.join(res['installed'])}")
    else:
        console.print(f"[red]{res.get('error')}[/]")


@skills_app.command("new")
def skills_new(name: str) -> None:
    """Scaffold a new SKILL.md you can edit."""
    from pathlib import Path

    from xplogent.skills.pack import render_skill_md

    cfg = load_config()
    md = render_skill_md(name, "one sentence: when to use this skill",
                         "1. first step\n2. second step", tools=[], trigger="when ...")
    path: Path = cfg.skills_dir / f"{name}.md"
    path.write_text(md, encoding="utf-8")
    console.print(f"[green]scaffolded[/] {path}")


# ── small utilities ───────────────────────────────────────────────────────────
def _print_skills(store: Store | None) -> None:
    if store is None:
        console.print("[dim]memory disabled[/]")
        return
    skills = store.all_skills()
    if not skills:
        console.print("[dim]no skills learned yet[/]")
        return
    for s in skills:
        stars = "★" * s.stars + "☆" * (3 - s.stars)
        console.print(f"[magenta]{s.name}[/] [yellow]{stars}[/] [dim]{s.level}[/] "
                      f"(used {s.uses}×) — {s.description}")


def _set_model(spec: str) -> None:
    save_user_config({"model": spec})


def main() -> None:
    app()


if __name__ == "__main__":
    main()
