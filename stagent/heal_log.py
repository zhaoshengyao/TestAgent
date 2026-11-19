"""stagent 自愈跟踪：把 healer.propose() 和直接触发的自愈都记下来。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class HealEvent:
    tool: str
    attempt: int
    ok: bool
    reason: str = ""
    ref: str = ""
    element: str = ""
    confidence: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "attempt": self.attempt,
            "ok": self.ok,
            "reason": self.reason,
            "ref": self.ref,
            "element": self.element,
            "confidence": self.confidence,
        }


class HealLog:
    """Agent 持有一个 HealLog，Healer 和 _run_tool 都往这里写。"""

    def __init__(self) -> None:
        self.events: list[HealEvent] = []

    def add(self, event: HealEvent) -> None:
        self.events.append(event)

    def reset(self) -> None:
        self.events = []

    def as_dicts(self) -> list[dict[str, Any]]:
        return [e.to_dict() for e in self.events]
