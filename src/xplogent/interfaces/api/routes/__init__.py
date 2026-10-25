"""API routers, one module per dashboard area."""

from xplogent.interfaces.api.routes import (
    branding,
    chat,
    knowledge,
    memory,
    sessions,
    settings,
    system,
    voip,
)

ALL_ROUTERS = [
    system.router, sessions.router, chat.router, voip.router,
    settings.router, memory.router,
    knowledge.router, branding.router,
]
