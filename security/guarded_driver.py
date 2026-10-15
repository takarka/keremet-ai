"""GuardedDriver: прокси над DeviceDriver, объединяющий Allowlist, Audit Log и Payment Guard.

Стоит между агентом (LLM) и устройством, поэтому обойти защиту промптом нельзя.
Оборачивается так же, как RecordingDriver из mobilerun.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from devices.adb import find_adb
from security.allowlist import Allowlist
from security.approval import Category
from security.audit import AuditLog
from security.payment_guard import PaymentApprovalRequired, find_risky, screen_texts, texts_at


class TaskAborted(Exception):
    """Задача принудительно прервана защитным слоем."""


class LoopDetected(TaskAborted):
    """Агент ходит по кругу: повторяет одни и те же действия."""


class BudgetExceeded(TaskAborted):
    """Превышен лимит стоимости задачи."""


class TaskTimeout(TaskAborted):
    """Превышен лимит времени задачи."""


MAX_REPEATS = 5     # одно и то же действие подряд
MAX_SAME_TAP = 6    # тапов в одну и ту же точку (сетка TAP_GRID px) за задачу
MAX_SAME_TEXT = 3   # раз введён один и тот же текст за задачу
TAP_GRID = 40
MAX_REVIVES = 3     # попыток оживить Portal за задачу
MAX_APP_STARTS = 3  # запусков одного приложения за задачу (каждый перезапуск сбрасывает прогресс)
PORTAL_PACKAGE = "com.mobilerun.portal"
PORTAL_SERVICE = "com.mobilerun.portal/com.mobilerun.portal.service.MobilerunAccessibilityService"
# Категории Approval Gate по умолчанию; «внешняя отправка» включается явно (для новых/деловых чатов).
DEFAULT_POLICY = frozenset({Category.FINANCE, Category.DELETE})


class GuardedDriver:
    def __init__(
        self,
        inner: Any,
        allowlist: Allowlist,
        audit: AuditLog,
        fail_closed: bool = True,
        policy: frozenset[Category] = DEFAULT_POLICY,
        secrets: Any = None,
        decider: Any = None,
    ) -> None:
        self.decider = decider  # DeciderGuard | None: второй слой, только добавляет остановки
        self.inner = inner
        self.policy = policy
        self.secrets = secrets  # SecretStore | None: подставляет {{secret:name}} только здесь, в LLM не уходит
        self.allowlist = allowlist
        self.audit = audit
        self.fail_closed = fail_closed
        # После первой остановки все действия блокируются: LLM не должен искать обходной путь.
        self.halted: PaymentApprovalRequired | None = None
        self.stuck: TaskAborted | None = None  # зацикливание или превышение бюджета; после этого всё блокируется
        self._last_sig: tuple | None = None
        self._repeats = 0
        self._tap_counts: dict[tuple[int, int], int] = {}
        self._text_counts: dict[int, int] = {}
        self._revives = 0
        self._app_starts: dict[str, int] = {}

    def abort(self, exc: TaskAborted) -> None:
        """Принудительно прервать задачу (например, из раннера при превышении бюджета)."""
        if self.stuck is None:
            self.audit.log("task_aborted", reason=str(exc), kind=type(exc).__name__)
            self.stuck = exc

    def _loop(self, message: str, **data: Any) -> None:
        self.audit.log("loop_detected", **data)
        self.abort(LoopDetected(message))
        raise self.stuck  # type: ignore[misc]

    def _track(self, *sig: Any) -> None:
        """Обрыв при зацикливании: одно действие подряд или одна и та же точка/текст много раз за задачу."""
        if self.stuck is not None:
            raise self.stuck
        self._repeats = self._repeats + 1 if sig == self._last_sig else 1
        self._last_sig = sig
        if self._repeats > MAX_REPEATS:
            self._loop(f"Действие «{sig[0]}» повторено {self._repeats} раз подряд — задача прервана",
                       action=sig[0], repeats=self._repeats)
        if sig[0] == "tap":
            cell = (sig[1] // TAP_GRID, sig[2] // TAP_GRID)
            self._tap_counts[cell] = self._tap_counts.get(cell, 0) + 1
            if self._tap_counts[cell] > MAX_SAME_TAP:
                self._loop(f"Одна и та же область экрана нажата {self._tap_counts[cell]} раз — задача прервана",
                           action="tap", x=sig[1], y=sig[2], count=self._tap_counts[cell])
        elif sig[0] == "input_text":
            h = sig[1]
            self._text_counts[h] = self._text_counts.get(h, 0) + 1
            if self._text_counts[h] > MAX_SAME_TEXT:
                self._loop(f"Один и тот же текст введён {self._text_counts[h]} раз — задача прервана",
                           action="input_text", count=self._text_counts[h])

    @property
    def platform(self) -> str:
        return self.inner.platform

    @property
    def supported(self) -> set[str]:
        return self.inner.supported

    @property
    def supported_buttons(self) -> set[str]:
        return self.inner.supported_buttons

    def __getattr__(self, name: str):
        return getattr(self.inner, name)

    def _ensure_not_halted(self) -> None:
        if self.halted is not None:
            raise self.halted
        if self.stuck is not None:
            raise self.stuck

    # -- действия ------------------------------------------------------------

    async def tap(self, x: int, y: int) -> None:
        self._ensure_not_halted()
        self._track("tap", x, y)
        tree = None
        try:
            tree = await self.get_ui_tree()
            risky = find_risky(tree, x, y, self.policy)
        except Exception as exc:
            self.audit.log("guard_error", error=repr(exc), x=x, y=y)
            if self.fail_closed:
                raise PaymentApprovalRequired("не удалось проверить элемент под тапом") from exc
            risky = None
        if risky:
            await self._halt(risky[0], risky[1], x, y)
        if self.decider is not None and tree is not None:
            await self._decider_check(tree, x, y)
        self.audit.log("tap", x=x, y=y)
        await self.inner.tap(x, y)

    async def _decider_check(self, tree: Any, x: int, y: int) -> None:
        """Вторая линия: сбой модели не блокирует (решает regex), срабатывание — останавливает."""
        try:
            hit = await self.decider.check(texts_at(tree, x, y), screen_texts(tree), self.policy)
        except Exception as exc:  # noqa: BLE001
            self.audit.log("decider_error", error=repr(exc), x=x, y=y)
            return
        if hit:
            category, label, prob = hit
            self.audit.log("decider_stop", category=category.value, label=label, probability=round(prob, 3))
            await self._halt(category, label, x, y)

    async def get_ui_tree(self) -> Any:
        """Дерево экрана; если сервис доступности Portal убит системой (MIUI), оживляем его и повторяем."""
        try:
            return await self.inner.get_ui_tree()
        except Exception as exc:
            text = str(exc).lower()
            if "accessibility" not in text and "xml" not in text:
                raise
            if not await self._revive_portal():
                raise
            return await self.inner.get_ui_tree()

    async def _adb(self, *args: str) -> str:
        serial = getattr(self.inner, "_serial", None)
        cmd = [find_adb()] + (["-s", serial] if serial else []) + list(args)
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
        return out.decode("utf-8", "replace")

    async def _revive_portal(self) -> bool:
        """Выключить/включить сервис доступности Portal. Не больше MAX_REVIVES раз за задачу."""
        if self._revives >= MAX_REVIVES:
            return False
        self._revives += 1
        self.audit.log("portal_revive", attempt=self._revives)
        try:
            await self._adb("shell", "am", "force-stop", PORTAL_PACKAGE)
            await self._adb("shell", "settings", "put", "secure", "accessibility_enabled", "0")
            await asyncio.sleep(1.5)
            await self._adb("shell", "settings", "put", "secure", "enabled_accessibility_services", PORTAL_SERVICE)
            await self._adb("shell", "settings", "put", "secure", "accessibility_enabled", "1")
            await asyncio.sleep(3)
            ok = "MobilerunAccessibilityService" in await self._adb("shell", "dumpsys", "accessibility")
        except Exception as exc:  # noqa: BLE001
            self.audit.log("portal_revive_failed", error=repr(exc))
            return False
        self.audit.log("portal_revived", ok=ok)
        return ok

    async def _grab_screenshot(self) -> bytes | None:
        """Скриншот через Portal; если пусто (защищённый экран) — через `adb screencap`."""
        try:
            shot = await self.inner.screenshot()
            if shot:
                return shot
        except Exception as exc:
            self.audit.log("screenshot_failed", error=repr(exc))
        serial = getattr(self.inner, "_serial", None)
        cmd = [find_adb()] + (["-s", serial] if serial else []) + ["exec-out", "screencap", "-p"]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
            return out or None
        except Exception as exc:
            self.audit.log("adb_screencap_failed", error=repr(exc))
            return None

    async def _halt(self, category: Category, label: str, x: int, y: int) -> None:
        shot = await self._grab_screenshot()
        if shot:
            self.audit.save_screenshot("payment-stop", shot)
        else:
            self.audit.log("screenshot_unavailable")
        self.audit.log("payment_stop", category=category.value, label=label, x=x, y=y)
        self.halted = PaymentApprovalRequired(label, shot, category, x, y)
        raise self.halted

    async def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: float = 1000) -> None:
        self._ensure_not_halted()
        self._track("swipe", x1, y1, x2, y2)
        self.audit.log("swipe", x1=x1, y1=y1, x2=x2, y2=y2)
        await self.inner.swipe(x1, y1, x2, y2, duration_ms)

    async def drag(self, *args: Any, **kwargs: Any) -> None:
        self._ensure_not_halted()
        self.audit.log("drag", args=args, kwargs=kwargs)
        await self.inner.drag(*args, **kwargs)

    async def input_text(self, text: str, clear: bool = False, **kwargs: Any) -> bool:
        self._ensure_not_halted()
        self._track("input_text", hash(text))  # хэш только в памяти, в журнал не пишется
        self.audit.log("input_text", length=len(text), clear=clear)  # сам текст не пишем: может быть секретом
        if self.secrets is not None:
            text = self.secrets.resolve(text)
        return await self.inner.input_text(text, clear, **kwargs)

    async def press_button(self, button: str) -> None:
        self._ensure_not_halted()
        self.audit.log("press_button", button=button)
        await self.inner.press_button(button)

    async def press_key_code(self, key_code: int) -> None:
        self._ensure_not_halted()
        self.audit.log("press_key_code", key_code=key_code)
        await self.inner.press_key_code(key_code)

    async def start_app(self, package: str, activity: Optional[str] = None) -> str:
        self._ensure_not_halted()
        try:
            self.allowlist.check(package)
        except Exception as exc:
            self.audit.log("allowlist_block", package=package)
            raise exc
        self._app_starts[package] = self._app_starts.get(package, 0) + 1
        if self._app_starts[package] > MAX_APP_STARTS:
            self._loop(f"Приложение {package} запущено {self._app_starts[package]} раз — агент теряет прогресс, задача прервана",
                       action="start_app", package=package, count=self._app_starts[package])
        self.audit.log("start_app", package=package)
        return await self.inner.start_app(package, activity)

    async def stop_app(self, package: str) -> str:
        self._ensure_not_halted()
        self.allowlist.check(package)
        self.audit.log("stop_app", package=package)
        return await self.inner.stop_app(package)

    async def install_app(self, *args: Any, **kwargs: Any) -> str:
        self.audit.log("install_blocked")
        raise PermissionError("Установка приложений агентом запрещена")

    async def uninstall_app(self, package: str) -> str:
        self.audit.log("uninstall_blocked", package=package)
        raise PermissionError("Удаление приложений агентом запрещено")
