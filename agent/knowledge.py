"""База знаний: карточки приложений (App Cards), которые подмешиваются в промпт агента.

Карточка — markdown-файл knowledge/app_cards/android/<package>.md, связь «пакет → файл» лежит в
app_cards.json. Агент получает карточку автоматически, когда работает в этом приложении.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from agent.config import ROOT

CARDS_DIR = ROOT / "knowledge" / "app_cards" / "android"
LESSONS_HEADER = "## Уроки (добавлено командой note)"


def _mapping(cards_dir: Path) -> dict[str, str]:
    path = cards_dir / "app_cards.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def add_lesson(package: str, text: str, cards_dir: Path = CARDS_DIR, aliases_path: Path | None = None) -> Path:
    """Дописать урок в карточку приложения (создав её при необходимости)."""
    aliases_path = aliases_path or (ALIASES_PATH if cards_dir == CARDS_DIR else cards_dir / "aliases.json")
    text = text.strip()
    if not package or not text:
        raise ValueError("Нужны пакет приложения и текст урока")
    cards_dir.mkdir(parents=True, exist_ok=True)
    mapping = _mapping(cards_dir)
    filename = mapping.get(package) or f"{package}.md"
    card = cards_dir / filename
    if not card.exists():
        card.write_text(f"# {package}\n", encoding="utf-8")
    body = card.read_text(encoding="utf-8")
    if LESSONS_HEADER not in body:
        body = body.rstrip("\n") + f"\n\n{LESSONS_HEADER}\n"
    body = body.rstrip("\n") + f"\n- ({time.strftime('%Y-%m-%d')}) {text}\n"
    card.write_text(body, encoding="utf-8")
    if package not in mapping:
        mapping[package] = filename
        (cards_dir / "app_cards.json").write_text(json.dumps(mapping, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        _ensure_alias(package, aliases_path)
    return card


def _ensure_alias(package: str, aliases_path: Path) -> None:
    """Новому приложению сразу даём алиас по последнему сегменту пакета (его можно потом дополнить вручную)."""
    aliases = _aliases(aliases_path)
    if package not in aliases:
        aliases[package] = [package.split(".")[-1]]
        aliases_path.parent.mkdir(parents=True, exist_ok=True)
        aliases_path.write_text(json.dumps(aliases, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def list_cards(cards_dir: Path = CARDS_DIR) -> dict[str, int]:
    """{пакет: размер карточки в строках}."""
    out = {}
    for package, filename in _mapping(cards_dir).items():
        path = cards_dir / filename
        out[package] = len(path.read_text(encoding="utf-8").splitlines()) if path.exists() else 0
    return out


# --- подмешивание карточек в задачу ------------------------------------------------
# App Cards mobilerun читают только режимы Manager/Executor, а мы работаем в прямом режиме (FastAgent),
# поэтому карточки нужных приложений добавляем в текст цели сами.

ALIASES_PATH = ROOT / "knowledge" / "aliases.json"
MAX_CARD_CHARS = 6000


def _aliases(path: Path = ALIASES_PATH) -> dict[str, list[str]]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def packages_for_task(task: str, aliases_path: Path = ALIASES_PATH) -> list[str]:
    text = task.lower()
    found = []
    for package, names in _aliases(aliases_path).items():
        if package.lower() in text or any(n.lower() in text for n in names):
            found.append(package)
    return found


# Приложения, у которых нужное содержимое (например, адрес в 2ГИС) не попадает в дерево доступности.
VISION_PACKAGES = frozenset({"ru.dublgis.dgismobile"})


def needs_vision(task: str, aliases_path: Path = ALIASES_PATH) -> bool:
    return bool(VISION_PACKAGES & set(packages_for_task(task, aliases_path)))


def cards_for_task(task: str, cards_dir: Path = CARDS_DIR, aliases_path: Path = ALIASES_PATH) -> tuple[str, list[str]]:
    """(текст со справкой по упомянутым приложениям, список пакетов). Пустой текст, если карточек нет."""
    mapping = _mapping(cards_dir)
    parts, used = [], []
    for package in packages_for_task(task, aliases_path):
        filename = mapping.get(package)
        card = cards_dir / filename if filename else None
        if card and card.exists():
            parts.append(card.read_text(encoding="utf-8").strip()[:MAX_CARD_CHARS])
            used.append(package)
    if not parts:
        return "", []
    header = "Справка по приложениям (проверенные шаги из базы знаний; следуй им, если они подходят к задаче):"
    return header + "\n\n" + "\n\n".join(parts), used


def with_knowledge(task: str) -> tuple[str, list[str]]:
    context, used = cards_for_task(task)
    if not context:
        return task, []
    return f"{task}\n\n---\n{context}", used
