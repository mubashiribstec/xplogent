"""Self-improvement via reflection.

After a task finishes, the reflector asks a (usually cheaper/local) model to
review the transcript and emit structured JSON: durable facts worth remembering
and, when a repeatable procedure was discovered, a reusable skill. This is the
engine behind "the agent that grows with you".
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from xplogent.providers.base import Message, Provider, Role

_REFLECTION_PROMPT = """You are the reflection module of a self-improving AI agent.
Review the task and transcript below. Extract durable learnings.

Return ONLY a JSON object with this shape:
{
  "success": true/false,
  "facts": ["short durable facts about the user, environment, or preferences"],
  "skill": {
     "name": "snake_case_name",
     "description": "one sentence: when to use this skill",
     "body": "step-by-step reusable procedure in markdown"
  }
}
Rules:
- "facts" may be empty. Only include genuinely durable, reusable facts.
- Include "skill" ONLY if a clearly repeatable, non-trivial procedure was used; otherwise set it to null.
- Do not wrap the JSON in markdown fences."""


@dataclass
class SkillDraft:
    name: str
    description: str
    body: str


@dataclass
class ReflectionResult:
    success: bool = True
    facts: list[str] = field(default_factory=list)
    skill: SkillDraft | None = None


def _extract_json(text: str) -> dict:
    text = text.strip()
    # strip code fences if present
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return {}
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}


class Reflector:
    def __init__(self, provider: Provider) -> None:
        self.provider = provider

    async def reflect(self, task: str, transcript: str) -> ReflectionResult:
        messages = [
            Message(role=Role.SYSTEM, content=_REFLECTION_PROMPT),
            Message(role=Role.USER, content=f"TASK:\n{task}\n\nTRANSCRIPT:\n{transcript[:12000]}"),
        ]
        try:
            reply = await self.provider.complete(messages, temperature=0.2)
        except Exception:  # noqa: BLE001 - reflection must never break a run
            return ReflectionResult()

        data = _extract_json(reply.content)
        if not data:
            return ReflectionResult()

        skill = None
        raw_skill = data.get("skill")
        if isinstance(raw_skill, dict) and raw_skill.get("name") and raw_skill.get("body"):
            skill = SkillDraft(
                # Same fallback as pack._norm_name: a name like "!!!" normalises
                # to "" and would otherwise create an empty-named skill (".md").
                name=re.sub(r"[^a-z0-9_]+", "_", str(raw_skill["name"]).lower()).strip("_")
                or "skill",
                description=str(raw_skill.get("description", "")),
                body=str(raw_skill["body"]),
            )
        facts = [str(f) for f in data.get("facts", []) if str(f).strip()]
        raw_success = data.get("success", True)
        success = raw_success if isinstance(raw_success, bool) else \
            str(raw_success).strip().lower() not in ("false", "no", "0", "")
        return ReflectionResult(success=success, facts=facts, skill=skill)
