"""Request bodies for the REST API."""

from __future__ import annotations

from pydantic import BaseModel


class RunRequest(BaseModel):
    task: str


class AgentSpecModel(BaseModel):
    name: str
    role: str = "operator"
    task: str = ""
    model: str | None = None


class OrchestrateRequest(BaseModel):
    goal: str | None = None
    specs: list[AgentSpecModel] | None = None
    max_concurrent: int | None = None
    mode: str = "auto"
    auto_approve: bool = False  # explicit opt-in (the dashboard sends it)


class ConfigPatch(BaseModel):
    updates: dict = {}


class SecretsPatch(BaseModel):
    keys: dict[str, str] = {}


class RolePatch(BaseModel):
    allowed_tools: object = "*"
    policy: dict = {}
    allowed_paths: list[str] = []
    network: bool = True
    max_steps: int = 25


class RenameBody(BaseModel):
    title: str


class FactBody(BaseModel):
    content: str


class TextBody(BaseModel):
    content: str = ""


class ScheduleBody(BaseModel):
    name: str = ""
    prompt: str
    schedule: str           # e.g. "every day at 9am" or a 5-field cron
    mode: str = "agent"     # "agent" | "team"
    tz: str = ""
