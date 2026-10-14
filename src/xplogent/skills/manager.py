"""Skill persistence.

Skills are stored two ways: as markdown files under ``$XPLOGENT_HOME/skills`` (human
readable / editable) and in the memory store with an embedding (for semantic
retrieval). Facts go to long-term memory. Together with the reflector this closes
the self-improvement loop: act → reflect → consolidate → reuse.
"""

from __future__ import annotations

from pathlib import Path

from xplogent.memory.manager import MemoryManager
from xplogent.skills.reflection import ReflectionResult


class SkillManager:
    def __init__(self, memory: MemoryManager, skills_dir: Path) -> None:
        self.memory = memory
        self.skills_dir = skills_dir
        self.skills_dir.mkdir(parents=True, exist_ok=True)

    async def apply(self, result: ReflectionResult) -> dict[str, int | str | None]:
        """Persist a reflection result. Returns a small summary for events."""
        for fact in result.facts:
            await self.memory.remember(fact, source="reflection")

        skill_name: str | None = None
        skill_action: str | None = None
        skill_level = ""
        skill_stars = 0
        if result.skill:
            existed = self.memory.store.skill_exists(result.skill.name)
            await self.memory.save_skill(
                result.skill.name, result.skill.description, result.skill.body
            )
            self._write_markdown(result.skill.name, result.skill.description, result.skill.body)
            skill_name = result.skill.name
            skill_action = "updated" if existed else "learned"
            row = next((s for s in self.memory.store.all_skills() if s.name == skill_name), None)
            skill_level = row.level if row else "novice"
            skill_stars = row.stars if row else 1
            self.memory.store.add_skill_event(
                skill_name, skill_action, skill_level, skill_stars,
                result.skill.description[:120])

        return {"facts": len(result.facts), "skill": skill_name,
                "skill_action": skill_action, "skill_level": skill_level,
                "skill_stars": skill_stars}

    def _write_markdown(self, name: str, description: str, body: str) -> None:
        from xplogent.skills.pack import render_skill_md

        path = self.skills_dir / f"{name}.md"
        path.write_text(render_skill_md(name, description, body), encoding="utf-8")

    def export_all(self) -> int:
        """Write every stored skill out as a SKILL.md (folder mirrors the DB)."""
        n = 0
        for s in self.memory.store.all_skills():
            from xplogent.skills.pack import render_skill_md

            path = self.skills_dir / f"{s.name}.md"
            path.write_text(
                render_skill_md(s.name, s.description, s.body, tools=s.tools, trigger=s.trigger),
                encoding="utf-8")
            n += 1
        return n
