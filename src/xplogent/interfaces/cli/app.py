"""Xplogent CLI/TUI.

Commands:
    xplogent chat                 interactive agent session (streaming + inline approvals)
    xplogent run "<task>"         run a single task and exit
    xplogent model <provider:model>   set the active model
    xplogent memory search <q>    search episodic memory and facts
    xplogent skills list          list learned skills
    xplogent providers            list available providers
    xplogent serve                start the REST + WebSocket API
    xplogent doctor               check config, keys, model and ports; print fixes
"""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel

from xplogent.core.config import load_config, save_env, save_user_config
from xplogent.core.events import EventBus, EventType
from xplogent.memory.store import Store
from xplogent.providers.registry import available_providers
from xplogent.runtime import build_runtime
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
    if res.get("stopped"):
        console.print("[green]stopped[/]")
    elif res.get("ok") is False:
        console.print(f"[red]{res.get('error', 'could not stop the server')}[/]")
    else:
        console.print(f"[dim]{res.get('reason', 'not running')}[/]")


@app.command()
def doctor(port: int = 8765) -> None:
    """Check config, keys, model, database and ports; print plain-English fixes."""
    from xplogent.core.doctor import run_checks

    checks = run_checks(port=port)
    marks = {"ok": "[green]✓[/]", "warn": "[yellow]⚠[/]", "fail": "[red]✗[/]"}
    failures = 0
    for check in checks:
        console.print(f"{marks[check.status]} [bold]{check.name}[/] — {escape(check.detail)}")
        if check.fix and check.status != "ok":
            console.print(f"    [dim]fix: {escape(check.fix)}[/]")
        failures += check.status == "fail"
    if failures:
        console.print(f"\n[red]{failures} problem(s) to fix before Xplogent can run.[/]")
        raise typer.Exit(1)
    console.print("\n[green]All essential checks passed.[/] "
                  "Run [bold]xplogent up[/] to launch the dashboard.")


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

    res = service.restart(port=port, host=host)
    if res.get("ok", True):
        console.print("[green]restarted[/]")
    else:
        console.print(f"[red]restart failed: {res.get('error', res)}[/]")
        raise typer.Exit(1)


@app.command()
def service(action: str) -> None:
    """Install/uninstall an OS service for boot auto-start: install | uninstall."""
    from xplogent.core import service as svc

    if action == "install":
        res = svc.install_service()
    elif action == "uninstall":
        res = svc.uninstall_service()
    else:
        console.print("[red]use: xplogent service install | uninstall[/]")
        raise typer.Exit(2)
    if isinstance(res, dict) and res.get("ok") is False:
        console.print(f"[red]{res.get('error', res)}[/]")
        raise typer.Exit(1)
    console.print(f"[green]{action} done[/]" if not isinstance(res, dict)
                  else res.get("message", f"{action} done"))
    if isinstance(res, dict) and res.get("next"):
        console.print(f"next: {res['next']}")


@app.command()
def setup() -> None:
    """Interactive first-run wizard: pick a provider/model and save settings."""
    console.print(Panel("Let's set up Xplogent.", title="⚙ setup", border_style="cyan"))
    provider = console.input(
        "Provider [bold]\\[ollama][/] / openai / anthropic / gemini / openrouter: "
    ).strip().lower() or "ollama"
    defaults = {
        "ollama": "llama3.1", "openai": "gpt-4o",
        "anthropic": "claude-sonnet-4-6", "gemini": "gemini-3.6-flash",
        "openrouter": "google/gemma-4-31b-it:free",
    }
    if provider not in defaults:
        console.print(f"[yellow]Unknown provider '{provider}'[/] — "
                      "continuing anyway; fix it later in Settings if chats fail.")
    model = console.input(f"Model [bold]\\[{defaults.get(provider, '')}][/]: ").strip() \
        or defaults.get(provider, "")
    save_user_config({"model": f"{provider}:{model}"})

    if provider != "ollama":
        key_env = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
                   "gemini": "GOOGLE_API_KEY",
                   "openrouter": "OPENROUTER_API_KEY"}.get(provider)
        if key_env:
            key = console.input(f"{key_env} (leave blank to skip): ").strip()
            if key:
                save_env({key_env: key})
            else:
                console.print(f"[yellow]No key saved[/] — chats will fail until you add "
                              f"{key_env} (Settings → Providers, or re-run setup).")
    else:
        _setup_check_ollama(model)

    console.print("[green]Saved.[/] Run [bold]xplogent doctor[/] to verify everything, "
                  "then [bold]xplogent up[/] to launch the dashboard.")


def _setup_check_ollama(model: str) -> None:
    """After picking Ollama, say right away whether it's reachable and pulled."""
    from xplogent.core.doctor import _model_present, _ollama_tags
    from xplogent.providers.ollama import ollama_settings

    host, _token = ollama_settings()
    try:
        tags = _ollama_tags(host)
    except Exception:  # noqa: BLE001 - setup must continue; doctor has the full check
        console.print(f"[yellow]Ollama isn't answering at {host} yet.[/] Install it from "
                      "ollama.com, start it, then run:\n"
                      f"  ollama pull {model} && ollama pull nomic-embed-text")
        return
    missing = [m for m in (model, "nomic-embed-text") if not _model_present(tags, m)]
    if missing:
        console.print("[yellow]Ollama is running[/] — pull the missing model(s) first:\n"
                      + "\n".join(f"  ollama pull {m}" for m in missing))
    else:
        console.print("[green]Ollama is running and your models are pulled.[/]")


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
    import tarfile

    from xplogent.core.backup import restore_backup

    try:
        res = restore_backup(path)
    except (tarfile.ReadError, OSError, EOFError) as exc:
        console.print(f"[red]could not read {path}: {exc}[/] "
                      "(is it an Xplogent backup .tar.gz?)")
        raise typer.Exit(1) from exc
    if res["ok"]:
        console.print("[green]restored[/] — restart the server to use the restored data.")
    else:
        console.print(f"[red]{res.get('error')}[/]")
        raise typer.Exit(1)




# ── sub-commands ──────────────────────────────────────────────────────────────
memory_app = typer.Typer(help="Inspect memory.")
skills_app = typer.Typer(help="Inspect learned skills.")
knowledge_app = typer.Typer(help="Export/import learned facts + skills (JSON).")
docs_app = typer.Typer(help="Ingest documents for RAG (the agent answers from them).")
app.add_typer(memory_app, name="memory")
app.add_typer(skills_app, name="skills")
app.add_typer(knowledge_app, name="knowledge")
app.add_typer(docs_app, name="docs")

from xplogent.interfaces.cli.voip_cmds import dial as _dial  # noqa: E402
from xplogent.interfaces.cli.voip_cmds import voip_app  # noqa: E402

app.add_typer(voip_app, name="voip")
app.command(name="dial")(_dial)


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

    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        console.print(f"[red]could not read {path}: {exc}[/]")
        raise typer.Exit(1) from exc
    store = Store(load_config().db_path)
    res = import_knowledge(store, data)
    store.close()
    console.print(f"[green]imported[/] +{res['facts_added']} facts, +{res['skills_added']} skills")
    for w in res.get("warnings", []):
        console.print(f"[yellow]⚠ {w}[/]")


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

    from xplogent.skills.pack import _norm_name, render_skill_md

    cfg = load_config()
    safe = _norm_name(name)  # "My Skill!" -> my_skill; "../../x" can't escape
    md = render_skill_md(safe, "one sentence: when to use this skill",
                         "1. first step\n2. second step", tools=[], trigger="when ...")
    path: Path = cfg.skills_dir / f"{safe}.md"
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
