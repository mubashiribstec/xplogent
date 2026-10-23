"""Automatic backup model: if the main model can't answer, a local one does.

``FallbackProvider`` streams from the primary provider; if that fails *before any
output* (server down, timeout, bad/missing key, rate limit…) the same request is sent
to the backup (by default a small model on the local Ollama in Docker). Once the
primary has started answering, its errors are not retried elsewhere: the user would
see a half answer followed by a different one.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

from xplogent.core.logging import get_logger
from xplogent.providers.base import (
    Message,
    Provider,
    Role,
    StreamEvent,
    StreamKind,
    ToolSpec,
)

_log = get_logger("fallback")


class FallbackProvider(Provider):
    def __init__(self, primary: Provider, make_backup: Callable[[], Provider],
                 backup_spec: str = "") -> None:
        super().__init__(primary.model)
        self.primary = primary
        self._make_backup = make_backup
        self._backup: Provider | None = None
        self.backup_spec = backup_spec
        self.name = primary.name
        self.used_backup = False  # set when the last request was answered by the backup

    def _backup_provider(self) -> Provider:
        if self._backup is None:
            self._backup = self._make_backup()
        return self._backup

    async def stream(self, messages: list[Message], tools: list[ToolSpec] | None = None,
                     **kwargs: Any) -> AsyncIterator[StreamEvent]:
        self.used_backup = False
        emitted = False
        saw_done = False
        parts: list[str] = []
        try:
            async for ev in self.primary.stream(messages, tools, **dict(kwargs)):
                if ev.kind == StreamKind.TOKEN:
                    emitted = True
                    parts.append(ev.text or "")
                if ev.kind == StreamKind.DONE:
                    saw_done = True
                yield ev
            if saw_done:
                return
            if emitted:
                # The primary ended mid-answer without a DONE (truncated stream,
                # gateway cut): hand the caller the partial answer as a proper
                # completion instead of no DONE at all (which read as empty).
                _log.warning("main model %s ended without DONE; keeping partial answer",
                             self.primary.model)
                yield StreamEvent(kind=StreamKind.DONE, message=Message(
                    role=Role.ASSISTANT, content="".join(parts)))
                return
            _log.warning("main model %s ended without answering; using backup %s",
                         self.primary.model, self.backup_spec)
        except Exception as exc:  # noqa: BLE001 - decide below whether the backup may answer
            if emitted:
                raise
            _log.warning("main model %s failed (%s); answering with backup %s",
                         self.primary.model, exc, self.backup_spec)
        async for ev in self._backup_provider().stream(messages, tools, **dict(kwargs)):
            self.used_backup = True  # only once the backup actually answers
            yield ev

    async def embed(self, texts: list[str]) -> list[list[float]]:
        # Never mix embedding models: vectors from another model aren't comparable.
        return await self.primary.embed(texts)

    async def list_models(self) -> list[str]:
        return await self.primary.list_models()

    async def aclose(self) -> None:
        await self.primary.aclose()
        if self._backup is not None:
            await self._backup.aclose()
