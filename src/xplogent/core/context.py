"""Prompt construction and the short-term conversation window."""

from __future__ import annotations

from xplogent.providers.base import Message, Role

DEFAULT_SYSTEM_PROMPT = """You are Xplogent, a capable, self-improving personal AI agent \
running on the user's own machine. You work through tools: run shell commands, read, \
write and edit files, list folders, and use your persistent memory. Plugins can add \
more tools — use the ones you are actually given, and never claim an ability no \
available tool provides.

Operate as an autonomous agent:
- Think step by step. Break tasks into concrete actions.
- Prefer using tools to gather real information instead of guessing.
- After a tool returns, read its output and decide the next step.
- When the task is complete, give a clear final answer WITHOUT calling more tools.
- Be careful with destructive actions; the user must approve risky operations.

You have persistent memory across sessions. Relevant facts and learned skills are \
injected below when available — use them."""


def _approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def build_system_prompt(
    base: str | None,
    facts: list[str],
    skills: list[tuple],  # (name, description, body[, trigger, tools])
    persona: str = "",
    memory_md: str = "",
) -> str:
    parts: list[str] = []
    if persona.strip():
        parts.append(persona.strip())
    parts.append(base or DEFAULT_SYSTEM_PROMPT)
    if memory_md.strip():
        parts.append("## Curated memory (MEMORY.md)\n" + memory_md.strip())
    if facts:
        parts.append("## What you remember\n" + "\n".join(f"- {f}" for f in facts))
    if skills:
        skill_block = ["## Learned skills (apply when relevant)"]
        for s in skills:
            name, desc, body = s[0], s[1], s[2]
            trigger = s[3] if len(s) > 3 else ""
            tools = s[4] if len(s) > 4 else []
            head = f"### {name}\n{desc}"
            if trigger:
                head += f"\n_When:_ {trigger}"
            if tools:
                head += f"\n_Uses tools:_ {', '.join(tools)}"
            skill_block.append(f"{head}\n{body}")
        parts.append("\n".join(skill_block))
    return "\n\n".join(parts)


class ShortTermMemory:
    """The live conversation window with a soft token budget.

    When the running message history exceeds the budget, the oldest exchanges are
    folded into a compact summary placeholder so the context stays bounded.
    """

    def __init__(self, max_tokens: int = 6000) -> None:
        self.max_tokens = max_tokens
        self.messages: list[Message] = []
        self._summary: str = ""

    def add(self, message: Message) -> None:
        # One huge tool output (e.g. read_file of a 200 KB file) must not blow the whole
        # window: keep its head and tail, which is what the model usually needs.
        limit = max(2000, self.max_tokens * 2)  # chars ≈ half the token budget
        if message.role == Role.TOOL and len(message.content or "") > limit:
            c = message.content
            message.content = (c[: limit * 3 // 4] + f"\n…[{len(c) - limit} chars truncated]…\n"
                               + c[-limit // 4:])
        self.messages.append(message)

    def _total_tokens(self) -> int:
        # The summary counts too: it is rendered into every request, and
        # leaving it out let the "bounded" window grow without bound.
        return sum(_approx_tokens(m.content) for m in self.messages) \
            + _approx_tokens(self._summary)

    def _fold(self, msg: Message) -> None:
        snippet = (msg.content or "")[:200].replace("\n", " ")
        if msg.tool_calls:
            snippet = (snippet + " " + ", ".join(tc.name for tc in msg.tool_calls)).strip()
        self._summary += f"\n[{msg.role.value}] {snippet}"
        # Cap the summary at ~half the budget (in chars): past that, the OLDEST
        # folded lines drop — recent context is worth more than ancient history.
        cap = max(1000, self.max_tokens * 2)
        if len(self._summary) > cap:
            kept: list[str] = []
            total = 0
            for line in reversed(self._summary.split("\n")):
                if kept and total + len(line) + 1 > cap:
                    break
                kept.append(line)
                total += len(line) + 1
            self._summary = "\n".join(reversed(kept))

    def _pop_turn(self, at: int) -> None:
        """Remove the message at ``at`` *with* the tool results that belong to it.

        Providers reject a tool result whose assistant tool-call message is gone (400),
        so an assistant tool-call message and its TOOL replies are dropped together, and
        a stray TOOL message is never left at the front.
        """
        first = self.messages.pop(at)
        self._fold(first)
        if first.role == Role.ASSISTANT and first.tool_calls:
            while at < len(self.messages) and self.messages[at].role == Role.TOOL:
                self._fold(self.messages.pop(at))

    def trim(self) -> None:
        """Fold oldest turns into a summary placeholder when over budget.

        The opening user message (the task) stays pinned: the model always sees what it
        was asked, and the window keeps starting with a user turn.
        """
        while self._total_tokens() > self.max_tokens and len(self.messages) > 4:
            at = 1 if self.messages[0].role == Role.USER else 0
            self._pop_turn(at)

    def render(self) -> list[Message]:
        msgs: list[Message] = []
        if self._summary:
            msgs.append(
                Message(role=Role.SYSTEM, content="Earlier conversation summary:" + self._summary)
            )
        # Defensive: never hand a provider a tool result without its tool call.
        start = 0
        while start < len(self.messages) and self.messages[start].role == Role.TOOL:
            start += 1
        msgs.extend(self.messages[start:])
        return msgs
