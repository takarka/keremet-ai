"""App Allowlist: агент открывает только разрешённые приложения (P1-09)."""

from __future__ import annotations

from pathlib import Path

import yaml

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "allowlist.yaml"


class AllowlistViolation(Exception):
    """Попытка открыть приложение вне белого списка или из чёрного."""


class Allowlist:
    def __init__(self, allowed: set[str], blocked: set[str] | None = None) -> None:
        self.allowed = set(allowed)
        self.blocked = set(blocked or ())

    @classmethod
    def from_yaml(cls, path: Path | str = DEFAULT_PATH) -> "Allowlist":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls(set(data.get("allowed") or ()), set(data.get("blocked") or ()))

    def check(self, package: str) -> None:
        if package in self.blocked:
            raise AllowlistViolation(f"Приложение {package} запрещено политикой безопасности")
        if package not in self.allowed:
            raise AllowlistViolation(f"Приложение {package} не входит в белый список")
