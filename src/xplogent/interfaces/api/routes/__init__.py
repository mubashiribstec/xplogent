"""API routers, one module per dashboard area."""

from xplogent.interfaces.api.routes import (
    automation,
    chat,
    extension,
    knowledge,
    memory,
    orchestration,
    sessions,
    settings,
    system,
    voip,
)

ALL_ROUTERS = [
    system.router, sessions.router, chat.router, voip.router, extension.router,
    orchestration.router, settings.router, memory.router, automation.router,
    knowledge.router,
]
