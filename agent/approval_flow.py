"""Исполнение подтверждённого действия после Approval Gate (P2-08).

Агент остановился перед рискованной кнопкой и завершил задачу. После «Подтвердить» мы
заново проверяем, что на экране та же кнопка по тем же координатам, и нажимаем её один раз.
"""

from __future__ import annotations

from pathlib import Path

from security.approval import ApprovalSession, Category, State
from security.audit import AuditLog
from security.payment_guard import find_risky


class ApprovalError(Exception):
    pass


async def execute_approved_tap(task_dir: Path, serial: str | None = None, driver=None) -> str:
    session = ApprovalSession.load(task_dir)
    if session.state is not State.APPROVED:
        raise ApprovalError(f"Действие не подтверждено (состояние {session.state})")
    if session.x is None or session.y is None:
        raise ApprovalError("В сессии нет координат кнопки")

    audit = AuditLog(Path(task_dir).name, root=Path(task_dir).parent)
    if driver is None:
        from mobilerun.tools.driver import AndroidDriver

        driver = AndroidDriver(serial=serial)
        await driver.connect()

    tree = await driver.get_ui_tree()
    hit = find_risky(tree, session.x, session.y, frozenset(Category))
    if hit is None or hit[1] != session.label:
        audit.log("approved_tap_aborted", expected=session.label, found=hit[1] if hit else None)
        session.finish()
        session.save(task_dir)
        raise ApprovalError("Экран изменился: нужной кнопки уже нет под тем же местом. Действие не выполнено.")

    await driver.tap(session.x, session.y)
    audit.log("approved_tap", label=session.label, x=session.x, y=session.y)
    session.finish()
    session.save(task_dir)
    return f"Нажато: «{session.label}»"
