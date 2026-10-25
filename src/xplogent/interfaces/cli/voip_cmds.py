"""``xplogent voip …`` and ``xplogent dial``: the phone agent from the terminal.

The SIP trunk and live calls live in the running server, so these commands talk to its
API (``--url``, default ``http://127.0.0.1:8765`` or ``$XPLOGENT_URL``) with the local
access token. In Docker: ``xplogent-docker cli voip status``.
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx
import typer
from rich.console import Console
from rich.table import Table

console = Console()
voip_app = typer.Typer(help="Phone agent: readiness, SIP registration, live and recent calls.")

_URL = typer.Option(None, "--url", help="Server address (default $XPLOGENT_URL or http://127.0.0.1:8765).")


def _base(url: str | None) -> str:
    return (url or os.environ.get("XPLOGENT_URL") or "http://127.0.0.1:8765").rstrip("/")


def api(method: str, path: str, url: str | None = None, body: dict[str, Any] | None = None) -> Any:
    from xplogent.interfaces.api.deps import auth_token

    token = auth_token()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        with httpx.Client(timeout=30.0, trust_env=False) as client:
            resp = client.request(method, _base(url) + path, json=body, headers=headers)
    except httpx.HTTPError as exc:
        console.print(f"[red]Can't reach the Xplogent server at {_base(url)}[/] ({type(exc).__name__}). "
                      "Is it running (xplogent up)? Pass --url if it's elsewhere.")
        raise typer.Exit(2) from exc
    if resp.status_code == 401:
        console.print("[red]The server refused the access token.[/] Set XPLOGENT_AUTH_TOKEN or run "
                      "this on the server itself.")
        raise typer.Exit(2)
    try:
        data = resp.json()
    except ValueError:
        # A proxy/Caddy 502 page or a bare 500: report it, don't traceback.
        console.print(f"[red]The server answered HTTP {resp.status_code} with a non-JSON "
                      "response.[/] Check the server log.")
        raise typer.Exit(2) from None
    if resp.status_code >= 400:
        console.print(f"[red]HTTP {resp.status_code}[/]: {data.get('error') or data}")
        raise typer.Exit(2)
    return data


@voip_app.command("status")
def status(url: str | None = _URL) -> None:
    """Readiness checklist, SIP registration and calls in progress."""
    ready = api("GET", "/voip/readiness", url)
    st = api("GET", "/voip/status", url)
    sip = api("GET", "/voip/sip/status", url)
    head = "[green]ready for calls[/]" if ready.get("ready") else "[yellow]not ready yet[/]"
    console.print(f"Phone agent: {'on' if st.get('enabled') else 'off'} · {head} · "
                  f"line: {sip.get('telephony', 'telnyx')}")
    for item in ready.get("items", []):
        mark = "[green]✓[/]" if item["ok"] else ("[red]✗[/]" if item["required"] else "[yellow]○[/]")
        detail = f" [dim]{item['detail']}[/]" if item.get("detail") else ""
        console.print(f"  {mark} {item['label']}{detail}")
    if sip.get("configured"):
        reg = "[green]registered[/]" if sip.get("registered") else f"[red]not registered[/] {sip.get('error', '')}"
        console.print(f"SIP {sip.get('username', '')}@{sip.get('server', '')}: {reg}")
    live = st.get("active", [])
    console.print(f"Live calls: {len(live)}")
    for c in live:
        mins = int(time.time() - c.get("started", time.time())) // 60
        console.print(f"  #{c['call_id']} {c.get('contact_name') or c.get('number')} · {c['direction']} · "
                      f"{c['transport']} · {mins} min{' · AI paused' if c.get('paused') else ''}")


@voip_app.command("calls")
def calls(n: int = typer.Option(20, "-n", help="How many."), url: str | None = _URL) -> None:
    """Recent calls with outcome and duration."""
    rows = api("GET", f"/voip/calls?limit={max(1, n)}", url).get("calls", [])
    table = Table("#", "when", "who", "dir", "line", "length", "outcome")
    for c in rows:
        start, end = c.get("started_at") or 0, c.get("ended_at") or time.time()
        length = f"{int(end - start) // 60}:{int(end - start) % 60:02d}" if start else ""
        who = c.get("contact_name") or (c["to_number"] if c["direction"] == "outbound" else c["from_number"])
        table.add_row(str(c["id"]), time.strftime("%m-%d %H:%M", time.localtime(start)) if start else "",
                      str(who or "?"), c["direction"] + (" (test)" if c.get("test") else ""),
                      c.get("transport") or "", length, c.get("outcome") or c.get("status") or "")
    console.print(table)


def dial(number: str = typer.Argument("", help="Number (+countrycode…) or PBX extension."),
         via: str = typer.Option("", "--via", help="telnyx or sip (default: your phone line)."),
         test: bool = typer.Option(False, "--test", help="Test call: not counted, contact untouched."),
         contact: int = typer.Option(0, "--contact", help="Call this contact (by id) instead of a number."),
         anyway: bool = typer.Option(False, "--ignore-hours", help="Call even outside calling hours."),
         url: str | None = _URL) -> None:
    """Have the AI agent place a call."""
    if not number and not contact:
        console.print("[red]Give a number or --contact ID.[/]")
        raise typer.Exit(2)
    body: dict[str, Any] = {"number": number, "test": test, "ignore_hours": anyway}
    if via:
        body["via"] = via
    if contact:
        body["contact_id"] = contact
    res = api("POST", "/voip/dial", url, body)
    if res.get("ok"):
        console.print(f"[green]Calling[/] {number or f'contact {contact}'} via {res.get('via', via or 'telnyx')}"
                      f"{' (test call)' if test else ''}.")
    else:
        console.print(f"[red]Not dialed:[/] {res.get('error', 'unknown error')}")
        if res.get("hours"):
            console.print("Add --ignore-hours to call anyway.")
        raise typer.Exit(1)


@voip_app.command("debug")
def debug_cmd(call_id: int = typer.Argument(..., help="Call number (from `xplogent voip calls`)."),
              as_json: bool = typer.Option(False, "--json", help="Print the whole redacted bundle."),
              url: str | None = _URL) -> None:
    """One call's diagnostics: timeline, per-stage timings, SIP ladder, RTP or Telnyx events."""
    import json

    from xplogent.interfaces.api.deps import auth_token
    from xplogent.voip.debug import summary_lines

    token = auth_token()
    try:
        with httpx.Client(timeout=30.0, trust_env=False) as client:
            resp = client.get(f"{_base(url)}/voip/calls/{call_id}/debug/bundle",
                              headers={"Authorization": f"Bearer {token}"} if token else {})
    except httpx.HTTPError as exc:
        console.print(f"[red]Can't reach the Xplogent server at {_base(url)}[/] ({type(exc).__name__}).")
        raise typer.Exit(2) from exc
    if resp.status_code != 200:
        console.print(f"[red]HTTP {resp.status_code}[/]: {resp.text[:200]}")
        raise typer.Exit(2)
    doc = resp.json()
    if as_json:
        console.print_json(json.dumps(doc))
        return
    for line in summary_lines(doc):
        console.print(line, markup=False, highlight=False)
