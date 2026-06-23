from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal


Decision = Literal["keep", "rollback", "reverse", "revise", "branch", "stop", "status", "supplement"]


@dataclass
class HumanFeedback:
    trial_id: str
    decision: Decision
    supplement: str | None = None
    reviewer: str | None = None
    source: str = "message"
    received_at: float | None = None
    raw: Any = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
