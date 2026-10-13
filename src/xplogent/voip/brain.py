"""CallBrain — the lean per-call agent behind a phone conversation.

Heavier than a chatbot turn is exactly what we avoid here: no memory manager, no skill
recall, no embeddings per turn beyond one knowledge-base lookup. Each caller utterance:

1. retrieves the top knowledge-base chunks (scripts, FAQs, distilled playbook),
2. streams a short spoken-style reply from a fast LLM,
3. yields it sentence by sentence so TTS can start before the full reply exists,
4. executes a small, safe tool set (log outcome, callback, transfer, end call).

The model is told to answer only from the knowledge base, script, and customer record,
and otherwise offer a callback or a human — the main protection against made-up answers.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from xplogent.providers.base import Message, Provider, Role, StreamKind, ToolSpec

Retriever = Callable[[str], Awaitable[list[str]]]

GUARDRAILS = """You are a phone agent speaking with a customer on a live call.
Rules:
- Speak naturally in short sentences (1-3 sentences per turn). No lists, no markdown, no emojis.
- Answer ONLY from the KNOWLEDGE, SCRIPT and CUSTOMER sections below. If the answer is not
  there, say you will arrange a callback or transfer to a colleague. Never guess prices,
  dates, policies or promises.
- If asked, say plainly that you are an AI assistant. Never claim to be human.
- If the customer asks to stop being called, or says they are not interested in further
  calls, apologise, call log_outcome with outcome "do_not_call", then end the call.
- If the customer is upset, asks for a person, or the request is outside your scope, call
  transfer_to_human.
- Confirm important details (name, number, date) by repeating them back.
- When the conversation is finished, call log_outcome then end_call."""

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+|(?<=[.!?])$")

TOOLS = [
    ToolSpec("log_outcome", "Record the result of the call.", {
        "type": "object",
        "properties": {
            "outcome": {"type": "string",
                        "description": "resolved | callback | transferred | interested | "
                                       "not_interested | do_not_call | other"},
            "summary": {"type": "string", "description": "One or two sentence summary."},
        }, "required": ["outcome"]}),
    ToolSpec("book_callback", "Schedule a call back to the customer.", {
        "type": "object",
        "properties": {"when": {"type": "string", "description": "When the customer prefers."},
                       "note": {"type": "string"}}, "required": ["when"]}),
    ToolSpec("transfer_to_human", "Hand the call to a human agent.", {
        "type": "object", "properties": {"reason": {"type": "string"}}}),
    ToolSpec("lookup_customer", "Look up a customer record by name or phone number.", {
        "type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}),
    ToolSpec("end_call", "Politely end the call after saying goodbye.", {
        "type": "object", "properties": {"reason": {"type": "string"}}}),
]


@dataclass
class CallState:
    """Mutable facts about the call that the engine reads after each turn."""
    outcome: str = ""
    summary: str = ""
    callback: str = ""
    transfer: bool = False
    transfer_reason: str = ""
    end: bool = False
    do_not_call: bool = False
    events: list[str] = field(default_factory=list)


def split_sentences(buffer: str, final: bool = False) -> tuple[list[str], str]:
    """Split ``buffer`` into complete sentences + the unfinished remainder."""
    out: list[str] = []
    start = 0
    for m in _SENTENCE_END.finditer(buffer):
        piece = buffer[start:m.end()].strip()
        # Avoid speaking tiny fragments like "Mr." or "OK." on their own.
        if len(piece) >= 12 or final:
            out.append(piece)
            start = m.end()
    rest = buffer[start:]
    # A long clause without punctuation: flush at a comma so speech starts sooner.
    if not final and len(rest) > 140 and "," in rest:
        cut = rest.rfind(",") + 1
        out.append(rest[:cut].strip())
        rest = rest[cut:]
    if final and rest.strip():
        out.append(rest.strip())
        rest = ""
    return out, rest


def trim_history(history: list[Message], limit: int) -> list[Message]:
    """Keep the last ``limit`` messages, starting on a caller (user) turn.

    A plain slice could start on a TOOL result whose tool call was cut off, which
    providers reject — every later turn of a long call would then fail.
    """
    if len(history) <= limit:
        return history
    out = history[-limit:]
    while out and out[0].role != Role.USER:
        out = out[1:]
    return out or history[-1:]


class CallBrain:
    def __init__(self, provider: Provider, *, persona: str = "", playbook: str = "",
                 contact: dict[str, Any] | None = None, retrieve: Retriever | None = None,
                 store: Any = None, max_turns: int = 16, temperature: float = 0.3) -> None:
        self.provider = provider
        self.persona = persona or "You are a friendly, professional customer-service agent."
        self.playbook = playbook
        self.contact = contact
        self.retrieve = retrieve
        self.store = store
        self.max_turns = max_turns
        self.temperature = temperature
        self.state = CallState()
        self.history: list[Message] = []

    # -- prompt -----------------------------------------------------------------
    def _system(self, knowledge: list[str]) -> str:
        parts = [self.persona, GUARDRAILS]
        if self.playbook:
            parts.append("SCRIPT / PLAYBOOK:\n" + self.playbook[:4000])
        if self.contact:
            c = self.contact
            extra = ", ".join(f"{k}: {v}" for k, v in (c.get("fields") or {}).items())
            parts.append(f"CUSTOMER: name {c.get('name') or 'unknown'}, phone {c.get('phone', '')}"
                         + (f", status {c['status']}" if c.get("status") else "")
                         + (f", notes: {c['notes']}" if c.get("notes") else "")
                         + (f", {extra}" if extra else ""))
        else:
            parts.append("CUSTOMER: unknown caller (ask politely for their name if needed).")
        parts.append("KNOWLEDGE (most relevant first):\n"
                     + ("\n---\n".join(knowledge) if knowledge else "(nothing relevant found)"))
        return "\n\n".join(parts)

    # -- tools ------------------------------------------------------------------
    def _run_tool(self, name: str, args: dict[str, Any]) -> str:
        st = self.state
        if name == "log_outcome":
            st.outcome = str(args.get("outcome", "other"))
            st.summary = str(args.get("summary", ""))
            if st.outcome == "do_not_call":
                st.do_not_call = True
            return "outcome recorded"
        if name == "book_callback":
            st.callback = f"{args.get('when', '')} {args.get('note', '')}".strip()
            st.outcome = st.outcome or "callback"
            return "callback booked"
        if name == "transfer_to_human":
            st.transfer = True
            st.transfer_reason = str(args.get("reason", ""))
            st.outcome = st.outcome or "transferred"
            return "transferring now; tell the customer you are connecting them"
        if name == "end_call":
            st.end = True
            return "call will end after your goodbye"
        if name == "lookup_customer":
            if self.store is None:
                return "customer lookup unavailable"
            q = str(args.get("query", ""))
            found = self.store.find_contact_by_phone(q) or next(iter(self.store.list_contacts(1, q)), None)
            if not found:
                return "no matching customer"
            return (f"{found.get('name')}, phone {found.get('phone')}, status {found.get('status')}, "
                    f"notes {found.get('notes') or 'none'}")
        return f"unknown tool {name}"

    # -- one conversational turn ---------------------------------------------------
    async def respond(self, caller_text: str) -> AsyncIterator[str]:
        """Yield the agent's reply sentence by sentence."""
        self.history.append(Message(role=Role.USER, content=caller_text))
        self.history = trim_history(self.history, self.max_turns * 3)
        knowledge: list[str] = []
        if self.retrieve is not None:
            try:
                knowledge = await self.retrieve(caller_text)
            except Exception:  # noqa: BLE001 - retrieval must never break a live call
                knowledge = []
        system = Message(role=Role.SYSTEM, content=self._system(knowledge))

        for _round in range(3):  # tool round-trips within one turn
            buffer = ""
            final_msg: Message | None = None
            async for ev in self.provider.stream([system, *self.history], TOOLS,
                                                 temperature=self.temperature):
                if ev.kind == StreamKind.TOKEN and ev.text:
                    buffer += ev.text
                    sentences, buffer = split_sentences(buffer)
                    for s in sentences:
                        yield s
                elif ev.kind == StreamKind.DONE:
                    final_msg = ev.message
            tail, _ = split_sentences(buffer, final=True)
            for s in tail:
                yield s

            if final_msg is None or not final_msg.tool_calls:
                spoken = (final_msg.content if final_msg else "") or ""
                if spoken:
                    self.history.append(Message(role=Role.ASSISTANT, content=spoken))
                return
            self.history.append(final_msg)
            for call in final_msg.tool_calls:
                result = self._run_tool(call.name, call.arguments or {})
                self.state.events.append(f"{call.name}: {result}")
                self.history.append(Message(role=Role.TOOL, content=result,
                                            tool_call_id=call.id, name=call.name))
