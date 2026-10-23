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
    safety = SafetyManager.from_config(config.safety)

    # Optionally scope this runtime to a role profile (safety PermissionProfile).
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
