"""Request bodies for the REST API."""

from __future__ import annotations

from pydantic import BaseModel


class RunRequest(BaseModel):
    task: str


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
