"""Multi-agent orchestrator.

Runs a pool of :class:`~xplogent.core.agent.Agent` workers bounded by a
user-set concurrency limit. Two entry points:

* :meth:`run_goal` — a Planner decomposes a goal into a dependency graph of
  subtasks; ready tasks are scheduled onto role-scoped workers as slots free up.
* :meth:`run_team` — the caller supplies named :class:`AgentSpec`s that run
  concurrently.

All workers share one :class:`MessageBus` (so they can talk) and one
:class:`TaskBoard`, and every event is tagged with the worker's identity for
deep monitoring.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

from xplogent.core.agent import Agent, ApproveCallback
from xplogent.core.config import Config
from xplogent.core.events import Event, EventBus, EventType
from xplogent.core.logging import get_logger
from xplogent.core.messaging import MessageBus
from xplogent.core.planner import Planner
from xplogent.core.retry import ErrorClass, RetryPolicy, classify_error
from xplogent.core.taskboard import Task, TaskBoard, TaskStatus
from xplogent.memory.manager import MemoryManager
from xplogent.memory.store import Store
from xplogent.memory.vector import Embedder
from xplogent.providers.base import Provider
from xplogent.providers.registry import build_provider
from xplogent.safety.approval import SafetyManager
from xplogent.safety.profile import PermissionProfile
from xplogent.skills.manager import SkillManager
from xplogent.skills.reflection import Reflector
from xplogent.tools.collab import collab_tools
from xplogent.tools.registry import ToolRegistry

_log = get_logger("orchestrator")


@dataclass
class AgentSpec:
    name: str
    role: str = "operator"
    task: str = ""
    model: str | None = None


class Orchestrator:
    def __init__(
        self,
        config: Config,
        *,
        bus: EventBus,
        store: Store,
        embedder: Embedder,
        base_tools: ToolRegistry,
        base_safety: SafetyManager,
        reflector: Reflector | None = None,
        skills: SkillManager | None = None,
        approve: ApproveCallback | None = None,
    ) -> None:
        self.config = config
        self.bus = bus
        self.store = store
        self.embedder = embedder
        self.base_tools = base_tools
        self.base_safety = base_safety
        self.reflector = reflector
        self.skills = skills
        self.approve = approve

        self.run_id = uuid.uuid4().hex[:12]
        self.message_bus = MessageBus(bus, store=store, run_id=self.run_id)
        self.board = TaskBoard(bus, run_id=self.run_id)
        # Shared registry so any worker can dispatch + collect background subagents.
        from xplogent.tools.collab import BackgroundTasks
        self.background_tasks = BackgroundTasks()
        self.default_max = int(config.orchestrator.get("max_concurrent_agents", 3))
        self.task_retries = int(config.orchestrator.get("max_task_retries", 2))
        self.max_delegation_depth = int(config.orchestrator.get("max_delegation_depth", 2))
        self.agents: dict[str, Agent] = {}
        self._providers: list[Provider] = []
        self._active = 0
        self.peak_concurrency = 0

    # -- worker construction ---------------------------------------------------
    def _make_worker(self, name: str, role: str, model: str | None = None,
                     agent_id: str | None = None, depth: int = 0) -> Agent:
        profile = PermissionProfile.from_role(role, self.config.roles)
        provider = build_provider(model or self.config.model)
        self._providers.append(provider)

        agent_id = agent_id or uuid.uuid4().hex[:8]
        tools = self.base_tools.filtered(profile.tool_filter())
        if self.config.orchestrator.get("enable_collab_tools", True):
            for tool in collab_tools(self.message_bus, agent_id, name,
                                     delegate=self.delegate, depth=depth,
                                     max_depth=self.max_delegation_depth,
                                     background_tasks=self.background_tasks):
                if profile.allows_tool(tool.name):
                    tools.register(tool)

        safety = self.base_safety.with_profile(profile, self.config.safety)
        memory = MemoryManager(
            self.store, self.embedder, session_id=self.store.create_session(name)
        )
        agent = Agent(
            self.config, provider, tools, safety,
            memory=memory, reflector=self.reflector, skills=self.skills,
            bus=self.bus, approve=self.approve,
            agent_id=agent_id, name=name, role=role, run_id=self.run_id,
            max_steps=profile.max_steps,
        )
        self.message_bus.register_agent(agent_id, name, role)
        self.agents[agent_id] = agent
        return agent

    async def _spawn(self, agent: Agent, task: str, *, nested: bool = False) -> str:
        """Run one worker, bounded by the shared concurrency semaphore.

        ``nested`` workers (spawned by ``delegate_task`` from inside another worker) run
        under their parent's slot instead of taking a new one: the parent is blocked
        waiting for them, so making them queue for the same semaphore deadlocked any run
        whose workers all delegated (e.g. max_concurrent=1). Nesting is bounded by
        ``max_delegation_depth``.
        """
        if nested:
            return await self._run_worker(agent, task)
        async with self._sem:
            return await self._run_worker(agent, task)

    async def _run_worker(self, agent: Agent, task: str) -> str:
        self._active += 1
        self.peak_concurrency = max(self.peak_concurrency, self._active)
        await self.bus.publish(Event(
            type=EventType.AGENT_SPAWN,
            data={"run_id": self.run_id, "agent_id": agent.agent_id,
                  "agent_name": agent.name, "role": agent.role},
        ))
        try:
            return await agent.run(task)
        finally:
            self._active -= 1

    async def delegate(self, task: str, role: str = "operator", depth: int = 1) -> str:
        """Spawn a fresh sub-agent mid-run (used by the ``delegate_task`` tool).

        Shares this orchestrator's run, message bus, and concurrency semaphore, so
        a worker can fan out its own helpers and they all show up in monitoring.
        """
        name = f"{role}-sub-{uuid.uuid4().hex[:6]}"
        agent = self._make_worker(name, role, depth=depth)
        return await self._spawn(agent, task, nested=True)

    # -- control ---------------------------------------------------------------
    def control(self, agent_id: str, action: str) -> bool:
        agent = self.agents.get(agent_id)
        if not agent:
            return False
        {"pause": agent.pause, "resume": agent.resume, "cancel": agent.cancel}.get(
            action, lambda: None
        )()
        return True

    def live_agents(self) -> list[dict]:
        return [
            {"id": a.agent_id, "name": a.name, "role": a.role,
             "status": a.status, "step": a.steps_taken, "current_tool": a.current_tool}
            for a in self.agents.values()
        ]

    # -- entry points ----------------------------------------------------------
    async def run_team(self, specs: list[AgentSpec], max_concurrent: int | None = None) -> dict:
        self._sem = asyncio.Semaphore(max_concurrent or self.default_max)
        self.store.create_run(self.run_id, goal=f"team:{len(specs)} agents", mode="manual")
        workers = [(s, self._make_worker(s.name, s.role, s.model)) for s in specs]
        results = await asyncio.gather(
            *[self._spawn(agent, spec.task) for spec, agent in workers]
        )
        self.store.finish_run(self.run_id)
        return {
            "run_id": self.run_id,
            "results": {spec.name: res for (spec, _), res in zip(workers, results, strict=True)},
            "messages": [m.__dict__ for m in self.message_bus.history],
            "peak_concurrency": self.peak_concurrency,
        }

    async def run_goal(self, goal: str, max_concurrent: int | None = None, mode: str = "auto") -> dict:
        count = max_concurrent or self.default_max
        self._sem = asyncio.Semaphore(count)
        self.store.create_run(self.run_id, goal=goal, mode=mode)

        planner = Planner(build_provider(self.config.reflection_model))
        self._providers.append(planner.provider)
        roles = [r for r in self.config.roles] or ["operator"]
        tasks = await planner.decompose(goal, roles, count=count)
        # Pre-assign + register every teammate so each agent sees the whole team
        # from the start (broadcast / list_agents / send_message).
        self._task_agent_ids: dict[str, str] = {}
        for t in tasks:
            self.board.add(t)
            aid = uuid.uuid4().hex[:8]
            self._task_agent_ids[t.id] = aid
            self.message_bus.register_agent(aid, f"{t.role}-{t.id}", t.role)
        await self.bus.publish(Event(
            type=EventType.RUN_PROGRESS,
            data={"run_id": self.run_id, "planned_tasks": len(tasks)},
        ))

        running: dict[str, asyncio.Task] = {}
        while not self.board.all_settled():
            for task in self.board.ready_tasks():
                if task.id not in running:
                    running[task.id] = asyncio.create_task(self._run_task(task))
            if not running:
                # nothing running and nothing ready → unmet/cyclic deps; stop.
                for t in self.board.tasks.values():
                    if t.status == TaskStatus.PENDING:
                        await self.board.fail(t.id, "unmet dependencies")
                break
            await asyncio.wait(running.values(), return_when=asyncio.FIRST_COMPLETED)
            for tid in [t for t, fut in running.items() if fut.done()]:
                running.pop(tid)

        self.store.finish_run(self.run_id)
        return {
            "run_id": self.run_id,
            "tasks": self.board.snapshot(),
            "messages": [m.__dict__ for m in self.message_bus.history],
            "peak_concurrency": self.peak_concurrency,
        }

    async def _run_task(self, task: Task) -> None:
        name = f"{task.role}-{task.id}"
        agent_id = getattr(self, "_task_agent_ids", {}).get(task.id)
        await self.board.claim(task.id, name)
        prompt = task.description
        deps = self.board.dependency_results(task)
        if deps:
            ctx = "\n\n".join(f"### {title}\n{result}" for title, result in deps.items())
            prompt += f"\n\nContext from completed subtasks:\n{ctx}"

        policy = RetryPolicy.from_attempts(self.task_retries)
        last_exc: Exception | None = None
        for attempt in range(1, policy.max_attempts + 1):
            # A fresh worker per attempt (clean session/state).
            agent = self._make_worker(name, task.role, agent_id=agent_id)
            try:
                answer = await self._spawn(agent, prompt)
                if getattr(agent, "failed", False):
                    # run() reports failure as text; surface it so the retry policy
                    # applies and dependents don't receive "(failed: ...)" as input.
                    raise agent.last_exception or RuntimeError(answer)
                await self.board.complete(task.id, answer)
                return
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                kind = classify_error(exc)
                if kind == ErrorClass.FATAL or attempt >= policy.max_attempts:
                    break
                _log.warning("task %s attempt %d failed (%s); retrying", task.id, attempt, kind)
                await self.bus.publish(Event(
                    type=EventType.RUN_PROGRESS,
                    data={"run_id": self.run_id, "task": task.id, "retry": attempt,
                          "reason": kind.value},
                ))
                await asyncio.sleep(policy.delay_for(attempt))
        _log.exception("task %s failed", task.id, exc_info=last_exc)
        await self.board.fail(task.id, str(last_exc))

    async def aclose(self) -> None:
        # Background helpers must stop before their providers' HTTP clients are closed.
        await self.background_tasks.cancel_all()
        for provider in self._providers:
            try:
                await provider.aclose()
            except Exception:  # noqa: BLE001
                pass
