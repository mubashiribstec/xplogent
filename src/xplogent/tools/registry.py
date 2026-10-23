"""Tool registry — one unified catalog of built-in and plugin tools."""

from __future__ import annotations

from xplogent.providers.base import ToolSpec
from xplogent.tools.base import Tool
from xplogent.tools.filesystem import filesystem_tools
from xplogent.tools.memory import memory_tools
from xplogent.tools.shell import ShellTool

# Maps a config group name to a factory producing its tools.
_BUILTIN_GROUPS = {
    "shell": lambda: [ShellTool()],
    "filesystem": filesystem_tools,
    "memory": memory_tools,
}


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def register_many(self, tools: list[Tool]) -> None:
        for tool in tools:
            self.register(tool)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def specs(self) -> list[ToolSpec]:
        return [t.spec() for t in self._tools.values()]

    def filtered(self, allowed: set[str] | None) -> ToolRegistry:
        """Return a new registry containing only the allowed tools.

        ``None`` means 'all tools' and returns a registry sharing the same tools.
        """
        clone = ToolRegistry()
        for tool in self._tools.values():
            if allowed is None or tool.name in allowed:
                clone.register(tool)
        return clone

    @classmethod
    def from_config(cls, enabled: list[str] | None = None) -> ToolRegistry:
        registry = cls()
        groups = enabled if enabled is not None else list(_BUILTIN_GROUPS)
        for group in groups:
            factory = _BUILTIN_GROUPS.get(group)
            if factory:
                registry.register_many(factory())
        return registry
