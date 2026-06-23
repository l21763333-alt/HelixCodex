from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ModelRepoState:
    branch: str
    head: str
    model_dirty: bool
    model_changes: list[str] = field(default_factory=list)
    project_changes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
