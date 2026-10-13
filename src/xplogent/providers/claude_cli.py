"""Claude via the local Claude Code CLI — use your Claude subscription, no API key.

Shells out to the ``claude`` CLI you've already logged into (``claude login``), so
no API key is stored and usage goes through your Pro/Max subscription. This is a
**chat-only** provider: the CLI runs its own agent loop, so Xplogent's own tools are
not wired through it (use an API-key provider for full tool-using agents).

Spec form: ``claude-cli`` (default model) or ``claude-cli:sonnet`` / ``claude-cli:opus``.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import AsyncIterator
from typing import Any

from xplogent.providers.base import (
    Message,
    Provider,
    Role,
    StreamEvent,
    StreamKind,
    ToolSpec,
)

_MISSING = ("The 'claude' CLI isn't installed or on PATH. Install Claude Code and run "
            "`claude login` to use your subscription, or pick an API-key model instead.")


class ClaudeCLIProvider(Provider):
    name = "claude-cli"

    def __init__(self, model: str = "", cli: str = "claude", timeout: float = 600.0,
                 **kwargs: Any) -> None:
        super().__init__(model, **kwargs)
        self.cli = cli
        self.timeout = float(timeout)

    async def list_models(self) -> list[str]:
        # The CLI exposes fixed aliases through your subscription.
        return ["sonnet", "opus", "haiku"]

    def _render(self, messages: list[Message]) -> tuple[str, str]:
        """Return (system_prompt, conversation_prompt) for a one-shot CLI call."""
        system_parts: list[str] = []
        turns: list[str] = []
        for m in messages:
            if m.role == Role.SYSTEM:
                system_parts.append(m.content)
            elif m.role == Role.USER:
                turns.append(f"User: {m.content}")
            elif m.role == Role.ASSISTANT and m.content:
                turns.append(f"Assistant: {m.content}")
            elif m.role == Role.TOOL:
                turns.append(f"(tool result: {m.content})")
        return "\n\n".join(system_parts), "\n\n".join(turns)

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,  # ignored — the CLI runs its own loop
        *,
        temperature: float = 0.7,
        **kwargs: Any,
    ) -> AsyncIterator[StreamEvent]:
        if shutil.which(self.cli) is None:
            yield StreamEvent(kind=StreamKind.TOKEN, text=_MISSING)
            yield StreamEvent(kind=StreamKind.DONE, message=Message(role=Role.ASSISTANT, content=_MISSING))
            return

        system, prompt = self._render(messages)
        args = [self.cli, "-p", "--output-format", "stream-json", "--verbose"]
        if system:
            args += ["--append-system-prompt", system]
        if self.model and self.model != "claude-cli":
            args += ["--model", self.model]

        proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        assert proc.stdin and proc.stdout and proc.stderr
        # Drain stderr concurrently: a chatty CLI filling the stderr pipe would block
        # forever while we wait on stdout.
        err_task = asyncio.create_task(proc.stderr.read())
        content_parts: list[str] = []
        usage: dict[str, int] | None = None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout
        timed_out = False
        try:
            proc.stdin.write(prompt.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    raw = await asyncio.wait_for(proc.stdout.readline(), remaining)
                except TimeoutError:
                    timed_out = True
                    break
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                etype = ev.get("type")
                if etype == "assistant":
                    text = _assistant_text(ev)
                    if text:
                        content_parts.append(text)
                        yield StreamEvent(kind=StreamKind.TOKEN, text=text)
                elif etype == "result":
                    u = ev.get("usage") or {}
                    if u:
                        usage = {"input_tokens": int(u.get("input_tokens", 0)),
                                 "output_tokens": int(u.get("output_tokens", 0))}
                    if not content_parts and ev.get("result"):
                        content_parts.append(str(ev["result"]))
                        yield StreamEvent(kind=StreamKind.TOKEN, text=str(ev["result"]))
            if timed_out:
                _kill(proc)
            await proc.wait()
            err = (await err_task).decode("utf-8", "replace").strip()
        finally:
            # Cancelled (client went away) or failed: never leave the CLI running.
            if proc.returncode is None:
                _kill(proc)
                try:
                    await asyncio.wait_for(proc.wait(), 5)
                except (TimeoutError, ProcessLookupError):
                    pass
            if not err_task.done():
                err_task.cancel()

        if timed_out:
            raise TimeoutError(f"claude CLI produced no result within {self.timeout:.0f}s")
        if proc.returncode and not content_parts:
            msg = f"claude CLI failed (exit {proc.returncode}): {err[-2000:] or 'no output'}"
            yield StreamEvent(kind=StreamKind.TOKEN, text=msg)
            content_parts.append(msg)

        yield StreamEvent(kind=StreamKind.DONE,
                          message=Message(role=Role.ASSISTANT,
                                          content="".join(content_parts), usage=usage))


def _kill(proc: asyncio.subprocess.Process) -> None:
    try:
        proc.kill()
    except ProcessLookupError:
        pass


def _assistant_text(ev: dict) -> str:
    blocks = (ev.get("message") or {}).get("content") or []
    return "".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
