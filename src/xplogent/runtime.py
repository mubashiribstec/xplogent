"""Runtime factory.

One place that assembles a fully-wired :class:`Agent` from a :class:`Config`:
provider, tool registry, safety gate, memory (store + embedder), and the
self-improvement pieces (reflector + skill manager). Every interface (CLI, API,
voice) builds its agent through here so behavior stays consistent.
"""

from __future__ import annotations

from dataclasses import dataclass

from xplogent.core.agent import Agent, ApproveCallback
from xplogent.core.config import Config, load_config
from xplogent.core.events import EventBus
from xplogent.core.orchestrator import Orchestrator
from xplogent.memory.manager import MemoryManager
from xplogent.memory.store import Store
from xplogent.memory.vector import Embedder
from xplogent.plugins.loader import load_plugins
from xplogent.providers.registry import build_provider
from xplogent.safety.approval import SafetyManager
from xplogent.safety.profile import PermissionProfile
from xplogent.skills.manager import SkillManager
from xplogent.skills.reflection import Reflector
from xplogent.tools.registry import ToolRegistry


@dataclass
class Runtime:
    config: Config
    agent: Agent
    store: Store | None
    bus: EventBus

    async def aclose(self) -> None:
        await self.agent.provider.aclose()
        if self.agent.memory:
            await self.agent.memory.embedder.provider.aclose()
        if self.agent.reflector:
            await self.agent.reflector.provider.aclose()
        if self.store:
            self.store.close()


def build_runtime(
    config: Config | None = None,
    *,
    bus: EventBus | None = None,
    approve: ApproveCallback | None = None,
    with_memory: bool = True,
    role: str | None = None,
    model: str | None = None,
    gen_params: dict | None = None,
    session_id: int | None = None,
) -> Runtime:
    config = config or load_config()
    bus = bus or EventBus()

    provider = build_provider(model or config.model)
    tools = ToolRegistry.from_config(config.tools.get("enabled"))
    load_plugins(tools)  # drop-in plugins extend the same registry
    # Live Canvas needs the event bus to push HTML to the dashboard, so it's wired
    # here rather than via the zero-arg builtin tool groups.
    if config.raw.get("canvas", {}).get("enabled", True):
        from xplogent.tools.canvas import CanvasTool
        tools.register(CanvasTool(bus))
    safety = SafetyManager.from_config(config.safety)

    # Optionally scope this runtime to a role profile (used by the MCP server).
    if role:
        profile = PermissionProfile.from_role(role, config.roles)
        tools = tools.filtered(profile.tool_filter())
        safety = safety.with_profile(profile, config.safety)

    memory: MemoryManager | None = None
    reflector: Reflector | None = None
    skills: SkillManager | None = None
    store: Store | None = None

    if with_memory and config.memory.get("enabled", True):
        store = Store(config.db_path)
        # Reuse an existing session (chat continuity) or start a new one.
        sid = session_id if session_id is not None else store.create_session(title="chat")
        embed_provider = build_provider(config.embedding_model)
        embedder = Embedder(embed_provider)
        memory = MemoryManager(store, embedder, session_id=sid)

        if config.skills.get("enabled", True):
            reflector = Reflector(build_provider(config.reflection_model))
            skills = SkillManager(memory, config.skills_dir)

    agent = Agent(
        config, provider, tools, safety,
        memory=memory, reflector=reflector, skills=skills, bus=bus, approve=approve,
        gen_params=gen_params,
    )
    if session_id is not None:
        agent.load_history()
    return Runtime(config=config, agent=agent, store=store, bus=bus)


@dataclass
class OrchestratorRuntime:
    config: Config
    orchestrator: Orchestrator
    store: Store
    bus: EventBus

    async def aclose(self) -> None:
        await self.orchestrator.aclose()
        await self.orchestrator.embedder.provider.aclose()
        if self.orchestrator.reflector:
            await self.orchestrator.reflector.provider.aclose()
        self.store.close()


def build_orchestrator(
    config: Config | None = None,
    *,
    bus: EventBus | None = None,
    approve: ApproveCallback | None = None,
) -> OrchestratorRuntime:
    """Assemble a multi-agent orchestrator sharing one store, memory, and bus."""
    config = config or load_config()
    bus = bus or EventBus()

    store = Store(config.db_path)
    embedder = Embedder(build_provider(config.embedding_model))
    base_tools = ToolRegistry.from_config(config.tools.get("enabled"))
    load_plugins(base_tools)
    base_safety = SafetyManager.from_config(config.safety)

    reflector: Reflector | None = None
    skills: SkillManager | None = None
    if config.skills.get("enabled", True):
        reflector = Reflector(build_provider(config.reflection_model))
        skills = SkillManager(
            MemoryManager(store, embedder, session_id=store.create_session("skills")),
            config.skills_dir,
        )

    orchestrator = Orchestrator(
        config, bus=bus, store=store, embedder=embedder,
        base_tools=base_tools, base_safety=base_safety,
        reflector=reflector, skills=skills, approve=approve,
    )
    return OrchestratorRuntime(config=config, orchestrator=orchestrator, store=store, bus=bus)


async def run_team_goal(goal: str, *, approve: ApproveCallback | None = None,
                        config: Config | None = None) -> str:
    """Run a multi-agent goal unattended (schedules/triggers in team mode), recording
    its event trace so the Runs page can show what each agent did."""
    from xplogent.monitor.recorder import TraceRecorder

    bus = EventBus()
    runtime = build_orchestrator(config, bus=bus, approve=approve)
    recorder = TraceRecorder(bus, runtime.store, runtime.orchestrator.run_id)
    recorder.start()
    try:
        return await runtime.orchestrator.run_goal(goal)
    finally:
        await bus.close()
        await recorder.stop()
        await runtime.aclose()
