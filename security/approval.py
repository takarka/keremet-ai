"""Approval Gate: статус-машина сессии и категории рискованных действий (P2-07, P2-09).

RUNNING → WAITING_FOR_APPROVAL → APPROVED | CANCELLED | TIMEOUT → DONE
Состояние пишется в runs/<task_id>/session.json, поэтому переживает перезапуск бота.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path


class State(StrEnum):
    RUNNING = "RUNNING"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    APPROVED = "APPROVED"
    CANCELLED = "CANCELLED"
    TIMEOUT = "TIMEOUT"
    DONE = "DONE"


class Category(StrEnum):
    FINANCE = "finance"          # оплата, перевод, покупка
    EXTERNAL_SEND = "external"   # отправка сообщения внешнему адресату
    DELETE = "delete"            # удаление данных


_ALLOWED: dict[State, set[State]] = {
    State.RUNNING: {State.WAITING_FOR_APPROVAL, State.DONE},
    State.WAITING_FOR_APPROVAL: {State.APPROVED, State.CANCELLED, State.TIMEOUT},
    State.APPROVED: {State.DONE},
    State.CANCELLED: {State.DONE},
    State.TIMEOUT: {State.DONE},
    State.DONE: set(),
}

DEFAULT_TIMEOUT_S = 15 * 60


class InvalidTransition(Exception):
    pass


@dataclass
class ApprovalSession:
    task_id: str
    state: State = State.RUNNING
    category: str | None = None
    label: str | None = None
    x: int | None = None
    y: int | None = None
    screenshot: str | None = None
    requested_at: float | None = None
    timeout_s: float = DEFAULT_TIMEOUT_S
    history: list[tuple[float, str]] = field(default_factory=list)

    def _go(self, new: State) -> None:
        if new not in _ALLOWED[self.state]:
            raise InvalidTransition(f"{self.state} → {new} недопустим")
        self.state = new
        self.history.append((time.time(), new.value))

    def request(self, category: Category, label: str, x: int, y: int, screenshot: str | None = None) -> None:
        self._go(State.WAITING_FOR_APPROVAL)
        self.category, self.label, self.x, self.y, self.screenshot = category.value, label, x, y, screenshot
        self.requested_at = time.time()

    def approve(self) -> None:
        self._ensure_not_expired()
        self._go(State.APPROVED)

    def cancel(self) -> None:
        self._go(State.CANCELLED)

    def finish(self) -> None:
        self._go(State.DONE)

    def expired(self, now: float | None = None) -> bool:
        return (
            self.state is State.WAITING_FOR_APPROVAL
            and self.requested_at is not None
            and (now or time.time()) - self.requested_at > self.timeout_s
        )

    def expire_if_needed(self, now: float | None = None) -> bool:
        if self.expired(now):
            self._go(State.TIMEOUT)
            return True
        return False

    def _ensure_not_expired(self) -> None:
        if self.expire_if_needed():
            raise InvalidTransition("Время подтверждения истекло, действие отменено")

    # -- хранение -------------------------------------------------------------

    def save(self, directory: Path) -> Path:
        path = Path(directory) / "session.json"
        data = asdict(self)
        data["state"] = self.state.value
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, directory: Path) -> "ApprovalSession":
        data = json.loads((Path(directory) / "session.json").read_text(encoding="utf-8"))
        data["state"] = State(data["state"])
        data["history"] = [tuple(h) for h in data.get("history", [])]
        return cls(**data)
