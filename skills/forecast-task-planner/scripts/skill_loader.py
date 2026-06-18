from __future__ import annotations

from pathlib import Path
from typing import Any


def load_skills(skills_dir: str | Path = "skills") -> dict[str, dict[str, Any]]:
    root = Path(skills_dir)
    skills: dict[str, dict[str, Any]] = {}
    if not root.exists():
        return skills
    for skill_file in sorted(root.glob("*/SKILL.md")):
        text = skill_file.read_text(encoding="utf-8")
        name = skill_file.parent.name
        skills[name] = {
            "name": name,
            "path": skill_file.as_posix(),
            "content": text,
        }
    return skills


def render_skills_for_prompt(skills: dict[str, dict[str, Any]]) -> str:
    parts = []
    for skill in skills.values():
        parts.append(f"## {skill['name']}\n\n{skill['content']}")
    return "\n\n---\n\n".join(parts)
