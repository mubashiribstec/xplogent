"""The Xplogent agent loop.

Ties together providers, tools, safety, memory, and self-improvement into a
single streaming ReAct loop: think → (maybe) call tools → observe → repeat,
until the model returns a final answer. Emits events on an :class:`EventBus` so
any interface can render the run live.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable

from xplogent.core.config import Config
from xplogent.core.context import ShortTermMemory, build_system_prompt
from xplogent.core.events import Event, EventBus, EventType
from xplogent.core.limits import context_window, estimate_cost
from xplogent.core.logging import get_logger
from xplogent.core.retry import RETRYABLE, RetryPolicy, classify_error
from xplogent.core.smalltalk import is_small_talk
from xplogent.memory.manager import MemoryManager
from xplogent.providers.base import Message, Provider, Role, StreamKind
from xplogent.safety.approval import ApprovalRequest, SafetyManager
from xplogent.skills.manager import SkillManager
from xplogent.skills.reflection import Reflector
from xplogent.tools.registry import ToolRegistry

ApproveCallback = Callable[[ApprovalRequest], Awaitable[bool]]
_log = get_logger("agent")


class CancelledByUser(Exception):
    """Raised internally when an agent is cancelled mid-run."""


class Agent:
    def __init__(
        self,
        config: Config,
        provider: Provider,
        tools: ToolRegistry,
        safety: SafetyManager,
        *,
        memory: MemoryManager | None = None,
        reflector: Reflector | None = None,
        skills: SkillManager | None = None,
        bus: EventBus | None = None,
        approve: ApproveCallback | None = None,
        gen_params: dict | None = None,
        agent_id: str | None = None,
        name: str = "agent",
        role: str = "operator",
        run_id: str | None = None,
        max_steps: int | None = None,
    ) -> None:
        self.config = config
        self.provider = provider
        self.tools = tools
        self.safety = safety
        self.memory = memory
        self.reflector = reflector
        self.skills = skills
        self.bus = bus or EventBus()
        self.approve = approve
        # identity (used to tag every event for deep monitoring)
        self.agent_id = agent_id or uuid.uuid4().hex[:8]
        self.name = name
        self.role = role
        self.run_id = run_id
        self.stm = ShortTermMemory(
            max_tokens=int(config.memory.get("short_term_max_tokens", 6000))
        )
        self._max_steps = int(max_steps or config.agent.get("max_steps", 25))
        self._temperature = float(config.agent.get("temperature", 0.7))
        # generation params: temperature / effort / thinking / max_tokens
        self.gen_params: dict = gen_params or {}
        # live control
        self._pause = asyncio.Event()
        self._pause.set()  # set == not paused
        self._cancelled = False
        self.status = "idle"
        self.current_tool: str | None = None
        self.steps_taken = 0
        self.session_tokens = {"input": 0, "output": 0}  # cumulative this agent
        self.session_cost = 0.0  # cumulative USD this agent (best-effort estimate)
        self._recalled_skills: set[str] = set()  # skills used this run (for proficiency)
        self._injections: list[str] = []  # user suggestions to fold in mid-run (steering)

    def load_history(self, limit: int = 40) -> None:
        """Seed short-term memory from the persisted session so chat continues."""
        if not self.memory or self.memory.session_id is None:
            return
        rows = self.memory.store.session_messages(self.memory.session_id)
        for row in rows[-limit:]:
            role = Role.USER if row["role"] == "user" else Role.ASSISTANT
            self.stm.add(Message(role=role, content=row["content"]))

    async def _emit(self, type_: EventType, **data) -> None:
        # tag every event with this agent's identity + run for the monitor
        data.setdefault("agent_id", self.agent_id)
        data.setdefault("agent_name", self.name)
        data.setdefault("role", self.role)
        if self.run_id:
            data.setdefault("run_id", self.run_id)
        await self.bus.publish(Event(type=type_, data=data))

    # -- live control ----------------------------------------------------------
    def pause(self) -> None:
        self._pause.clear()

    def resume(self) -> None:
        self._pause.set()

    def cancel(self) -> None:
        self._cancelled = True
        self._pause.set()  # unblock if paused so the loop can exit

    def inject(self, text: str) -> None:
        """Queue a user suggestion to fold into the run at its next step (steering)."""
        text = (text or "").strip()
        if text:
            # Queued only: a paused run stays paused until the user resumes it.
            self._injections.append(text)

    async def _set_status(self, status: str) -> None:
        self.status = status
        await self._emit(EventType.AGENT_STATUS, status=status,
                         step=self.steps_taken, current_tool=self.current_tool)

    async def _checkpoint(self) -> None:
        """Honor pause/cancel between steps."""
        if self._cancelled:
            raise CancelledByUser
        if not self._pause.is_set():
            await self._set_status("paused")
            await self._pause.wait()
            await self._set_status("running")

    async def _build_messages(self, task: str) -> list[Message]:
        facts: list[str] = []
        skills: list[tuple[str, str, str]] = []
        if self.memory:
            facts = await self.memory.recall(task, k=int(self.config.memory.get("retrieval_top_k", 5)))
            relevant = await self.memory.relevant_skills(task, k=3,
                                                         counted=self._recalled_skills)
            skills = [(s.name, s.description, s.body, s.trigger, s.tools) for s in relevant]
            self._recalled_skills.update(s.name for s in relevant)
            if facts or skills:
                await self._emit(EventType.MEMORY, facts=len(facts), skills=len(skills))
        system = build_system_prompt(
            self.config.agent.get("system_prompt"), facts, skills,
            persona=getattr(self, "_persona", ""), memory_md=getattr(self, "_memory_md", ""),
        )
        return [Message(role=Role.SYSTEM, content=system), *self.stm.render()]

    async def run(self, task: str, images: list[str] | None = None) -> str:
        """Run one task to completion and return the final answer.

        ``images`` are file paths / data-URIs attached to the first user message so a
        vision-capable model can see them (ignored by text-only models).
        """
        # Load the file-first identity once per run (SOUL.md + curated MEMORY.md).
        from xplogent.core.persona import load_memory, load_soul
        self._persona = load_soul()
        self._memory_md = load_memory()
        await self._emit(EventType.RUN_START, task=task)
        await self._set_status("running")
        self.stm.add(Message(role=Role.USER, content=task, images=images or []))
        if self.memory:
            self.memory.log("user", task)
        transcript: list[str] = [f"USER: {task}"]
        tool_steps = 0              # tool calls that worked: only those are worth reflecting on
        # "hi" / "thanks": answer without tools (a small model would otherwise go and
        # read files) and learn nothing from it.
        small_talk = is_small_talk(task) and not images
        final_answer = ""
        nudged = False
        # Callers need to know the run *failed*,
        # since run() always returns a string instead of raising.
        self.failed = False
        self.last_exception: BaseException | None = None
        self._recalled_skills.clear()  # a cancelled earlier run may have left some behind

        try:
            for step in range(self._max_steps):
                await self._checkpoint()
                self._drain_injections(transcript)  # fold in any mid-run suggestions
                if await self._enforce_budget():
                    final_answer = "(stopped: cost budget reached)"
                    break
                self.steps_taken = step + 1
                await self._emit(EventType.STEP_START, step=step)
                messages = await self._build_messages(task)
                # Local vision models (e.g. llava) reject tool specs; an image turn is
                # a pure perception turn unless the user opts into vision+tools.
                has_images = any(getattr(m, "images", None) for m in messages)
                tool_specs = ([] if (has_images and not self.config.agent.get("vision_tools", False))
                              or small_talk else self.tools.specs())

                assistant = await self._stream_assistant(messages, tool_specs)
                if assistant is None:  # provider failed; error already emitted
                    final_answer = "(failed: the model provider returned an error)"
                    self.failed = True
                    break

                self.stm.add(assistant)
                await self._emit_usage(assistant)
                if assistant.content:
                    transcript.append(f"ASSISTANT: {assistant.content}")
                    await self._emit(EventType.MESSAGE, content=assistant.content)

                if not assistant.tool_calls:
                    final_answer = (assistant.content or "").strip()
                    if not final_answer and not nudged:
                        # Empty reply with no tool calls — nudge once for a real answer.
                        nudged = True
                        self.stm.add(Message(role=Role.USER, content=(
                            "Your reply was empty. Give your final answer to the user now.")))
                        continue
                    if not final_answer:
                        final_answer = ("(the model returned an empty response — try "
                                        "rephrasing or switching to another model)")
                    break

                # Execute each requested tool through the safety gate.
                for call in assistant.tool_calls:
                    result_text = await self._run_tool(call.name, call.arguments)
                    if not result_text.startswith(("ERROR", "BLOCKED")):
                        tool_steps += 1
                    transcript.append(
                        f"TOOL[{call.name}]({_short(call.arguments)}) -> {_short(result_text)}"
                    )
                    self.stm.add(
                        Message(role=Role.TOOL, content=result_text,
                                tool_call_id=call.id, name=call.name)
                    )
                self.stm.trim()
            else:
                # Out of steps — synthesize a best-effort answer instead of nothing.
                final_answer = (await self._final_synthesis(task)
                                or "(stopped: reached the step limit without a final answer)")
        except CancelledByUser:
            final_answer = "(cancelled by user)"
            await self._set_status("cancelled")
            await self._emit(EventType.RUN_END, answer=final_answer, cancelled=True)
            return final_answer
        except Exception as exc:  # noqa: BLE001 - a run must always return, never stall the chat
            _log.exception("agent run failed")
            final_answer = f"(run error: {exc})"
            self.failed = True
            self.last_exception = exc
            await self._emit(EventType.ERROR, message=str(exc))

        # Always finish cleanly: persist, emit RUN_END, consolidate — guarded so a
        # failure here can't leave the chat hanging.
        try:
            if self.memory:
                self.memory.log("assistant", final_answer)
                self._record_skill_outcomes(final_answer)
        except Exception:  # noqa: BLE001
            _log.exception("post-run logging failed")
        await self._set_status("done")
        await self._emit(EventType.RUN_END, answer=final_answer)
        try:
            await self._post_task(task, "\n".join(transcript), tool_steps)
            await self._maybe_compact_memory()
        except Exception:  # noqa: BLE001
            _log.exception("post-task consolidation failed")
        return final_answer

    def _drain_injections(self, transcript: list[str]) -> None:
        """Fold queued user suggestions into the conversation before the next step."""
        while self._injections:
            hint = self._injections.pop(0)
            self.stm.add(Message(role=Role.USER, content=hint))
            transcript.append(f"USER (suggestion): {hint}")
            if self.memory:
                self.memory.log("user", hint)

    async def _final_synthesis(self, task: str) -> str:
        """One no-tools pass asking the model for its best answer from the work so far."""
        messages = await self._build_messages(task)
        messages.append(Message(role=Role.USER, content=(
            "You've reached the step limit. Based on everything above, give the user your "
            "best final answer now. Do not call any tools.")))
        assistant = await self._stream_assistant(messages, [])
        text = (assistant.content if assistant else "").strip()
        if text:
            await self._emit(EventType.MESSAGE, content=text)
        return text

    async def _maybe_compact_memory(self) -> None:
        """Periodically distill MEMORY.md from history (off by default)."""
        n = int(self.config.memory.get("auto_compact_every", 0) or 0)
        if n <= 0 or not self.memory:
            return
        self._run_count = getattr(self, "_run_count", 0) + 1
        if self._run_count % n != 0:
            return
        from xplogent.core.persona import compact_memory
        provider = self.reflector.provider if self.reflector else self.provider
        try:
            await compact_memory(self.memory.store, provider)
        except Exception:  # noqa: BLE001 - never break a run on compaction
            pass

    def _record_skill_outcomes(self, final_answer: str) -> None:
        """Credit/penalize the skills recalled this run by how it ended."""
        if not (self.memory and self._recalled_skills):
            return
        ok = not final_answer.startswith(("(failed", "(cancelled", "(stopped"))
        for name in self._recalled_skills:
            self.memory.record_skill_outcome(name, ok)
        self._recalled_skills.clear()

    async def _stream_assistant(self, messages, tool_specs) -> Message | None:
        """Stream one assistant turn, retrying transient provider failures.

        A retry only happens if the error arrives *before* any token was streamed,
        so the user never sees duplicated output.
        """
        params = {"temperature": self._temperature, **self.gen_params}
        policy = RetryPolicy.from_attempts(int(self.config.agent.get("provider_retries", 2)))
        for attempt in range(1, policy.max_attempts + 1):
            assistant: Message | None = None
            emitted = False
            try:
                async for ev in self.provider.stream(messages, tool_specs, **params):
                    if ev.kind == StreamKind.TOKEN:
                        emitted = True
                        await self._emit(EventType.TOKEN, text=ev.text)
                    elif ev.kind == StreamKind.DONE:
                        assistant = ev.message
                if getattr(self.provider, "used_backup", False):
                    await self._emit(EventType.THINKING, text=(
                        f"main model unavailable: answered by the backup model "
                        f"({getattr(self.provider, 'backup_spec', 'backup')})"))
                return assistant or Message(role=Role.ASSISTANT, content="")
            except Exception as exc:  # noqa: BLE001 - one agent's failure must not crash others
                kind = classify_error(exc)
                if emitted or kind not in RETRYABLE or attempt >= policy.max_attempts:
                    self.last_exception = exc
                    _log.warning("provider error for agent %s: %s", self.name, exc)
                    await self._emit(EventType.ERROR, message=f"provider error: {exc}")
                    return None
                _log.warning("provider %s for agent %s; retry %d", kind, self.name, attempt)
                await asyncio.sleep(policy.delay_for(attempt))
        return None

    async def _enforce_budget(self) -> bool:
        """Check spend against configured caps. Returns True if the run must stop."""
        budget = self.config.budget
        if not budget or not (budget.get("daily_usd") or budget.get("session_usd")):
            return False
        from xplogent.core.budget import check_budget

        store = self.memory.store if self.memory else None
        verdict = check_budget(budget, store, self.session_cost)
        if not verdict.exceeded:
            self._budget_warned = False
            return False

        if verdict.action == "downgrade" and budget.get("downgrade_model"):
            target = str(budget["downgrade_model"])
            current = f"{getattr(self.provider, 'name', '')}:{getattr(self.provider, 'model', '')}"
            if target and target != current:
                from xplogent.providers.registry import build_provider
                old = self.provider
                self.provider = build_provider(target)
                await old.aclose()
                await self._emit(EventType.BUDGET, action="downgrade", scope=verdict.scope,
                                 reason=verdict.reason, model=target,
                                 spent=verdict.spent, cap=verdict.cap)
            return False

        if verdict.action == "pause":
            await self._emit(EventType.BUDGET, action="pause", scope=verdict.scope,
                             reason=verdict.reason, spent=verdict.spent, cap=verdict.cap)
            return True

        # warn (default): notify once, keep going.
        if not getattr(self, "_budget_warned", False):
            self._budget_warned = True
            await self._emit(EventType.BUDGET, action="warn", scope=verdict.scope,
                             reason=verdict.reason, spent=verdict.spent, cap=verdict.cap)
        return False

    async def _emit_usage(self, assistant: Message) -> None:
        """Emit real token usage + context-window fill for this turn."""
        model_spec = f"{getattr(self.provider, 'name', '?')}:{getattr(self.provider, 'model', '')}"
        limit = context_window(model_spec, self.config.raw.get("model_limits"))
        used = sum(len(m.content) // 4 + 1 for m in self.stm.render())
        data = {"model": model_spec, "context_limit": limit, "context_used": used}
        if assistant.usage:
            it = assistant.usage.get("input_tokens", 0)
            ot = assistant.usage.get("output_tokens", 0)
            self.session_tokens["input"] += it
            self.session_tokens["output"] += ot
            turn_cost = estimate_cost(model_spec, it, ot)
            self.session_cost += turn_cost
            if self.memory:
                try:
                    self.memory.store.add_usage(
                        model_spec, it, ot, turn_cost, self.memory.session_id)
                except Exception:  # noqa: BLE001 - usage logging must never break a turn
                    pass
            data.update({
                "input_tokens": it, "output_tokens": ot,
                "session_input": self.session_tokens["input"],
                "session_output": self.session_tokens["output"],
                "session_cost": round(self.session_cost, 4),
            })
        await self._emit(EventType.USAGE, **data)

    def _audit(self, action: str, target: str, risk: str = "",
               allowed: bool | None = None, detail: str = "") -> None:
        """Best-effort security audit entry (never breaks a run)."""
        if not self.memory:
            return
        try:
            self.memory.store.add_audit(
                getattr(self, "name", "agent"), action, target, risk, allowed, detail)
        except Exception:  # noqa: BLE001
            pass

    async def _run_tool(self, name: str, arguments: dict) -> str:
        tool = self.tools.get(name)
        if tool is None:
            return f"ERROR: unknown tool '{name}'"
        self.current_tool = name
        await self._emit(EventType.TOOL_CALL, tool=name, arguments=arguments)

        decision = await self.safety.evaluate(tool, arguments, self.approve)
        if decision.needed_confirmation:
            await self._emit(EventType.APPROVAL_RESOLVED, tool=name, allowed=decision.allowed)
        self._audit("tool", name, decision.risk.value, decision.allowed, _short(arguments, 200))
        if not decision.allowed:
            text = f"BLOCKED by safety policy ({decision.risk.value}): {decision.reason}"
            await self._emit(EventType.TOOL_RESULT, tool=name, ok=False, output=text)
            self.current_tool = None
            return text

        try:
            result = await tool.run(**arguments)
            text = result.as_text()
            await self._emit(EventType.TOOL_RESULT, tool=name, ok=result.ok, output=text)
            return text
        except TypeError as exc:
            text = f"ERROR: bad arguments for {name}: {exc}"
            await self._emit(EventType.TOOL_RESULT, tool=name, ok=False, output=text)
            return text
        except Exception as exc:  # noqa: BLE001
            await self._emit(EventType.TOOL_RESULT, tool=name, ok=False, output=str(exc))
            return f"ERROR running {name}: {exc}"
        finally:
            self.current_tool = None

    async def _post_task(self, task: str, transcript: str, tool_steps: int) -> None:
        """Self-improvement: reflect, then consolidate memory and skills.

        Reflection is gated so routine runs don't spend tokens: it's skipped when
        fewer than ``reflect_min_steps`` tools ran (default 1 → plain chat skips),
        and only fires every ``reflect_every`` qualifying task.
        """
        if not (self.reflector and self.skills and self.config.skills.get("reflect_after_tasks", True)):
            return
        if tool_steps < int(self.config.skills.get("reflect_min_steps", 1)):
            return  # plain-chat / trivial turns: don't burn tokens reflecting
        every = max(1, int(self.config.skills.get("reflect_every", 1)))
        self._reflect_counter = getattr(self, "_reflect_counter", 0) + 1
        if self._reflect_counter % every != 0:
            return  # batch consolidation: only reflect every Nth qualifying task
        result = await self.reflector.reflect(task, transcript)
        summary = await self.skills.apply(result)
        if summary.get("facts") or summary.get("skill"):
            await self._emit(EventType.SKILL, **summary)


def _short(value, limit: int = 300) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[:limit] + "…"
