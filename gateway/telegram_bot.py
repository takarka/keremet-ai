"""Telegram-бот: текст и голос → задача на телефоне; Approval Gate кнопками (P1-16, P2-04, P2-08).

Запуск:  python -m gateway.telegram_bot
Доступ только для TELEGRAM_ALLOWED_USER_IDS (через запятую).
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from agent.approval_flow import ApprovalError, execute_approved_tap
from agent.config import ROOT, load_settings
from gateway.stt import SttError, transcribe
from security.approval import ApprovalSession, InvalidTransition, State

log = logging.getLogger("bot")
RUNS = ROOT / "runs"
# Распознанная голосом команда ждёт подтверждения: ошибка STT не должна запускать действие на телефоне.
_pending_voice: dict[str, str] = {}
_run_lock = asyncio.Lock()


def allowed_ids() -> set[int]:
    raw = os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "")
    return {int(x) for x in raw.replace(" ", "").split(",") if x}


def _kb(prefix: str, key: str, yes: str, no: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(yes, callback_data=f"{prefix}:{key}:y"), InlineKeyboardButton(no, callback_data=f"{prefix}:{key}:n")]]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Напишите или надиктуйте задачу для телефона. Оплаты, переводы и удаление "
        "я не выполняю без вашего подтверждения кнопкой."
    )


async def run_and_report(update_message, task: str) -> None:
    from agent.runner import run_task

    if _run_lock.locked():
        await update_message.reply_text("Уже выполняется другая задача, подождите.")
        return
    async with _run_lock:
        await update_message.reply_text(f"▶ Выполняю: {task}")
        report = await run_task(task, load_settings())
        footer = f"\n⏱ {report.duration_s} с · шагов {report.steps} · ~${report.cost_usd_estimate}"
        if report.approval_pending:
            run_dir = RUNS / report.task_id
            session = ApprovalSession.load(run_dir)
            caption = f"⏸ Нужно подтверждение ({session.category}): «{session.label}»{footer}"
            markup = _kb("ap", report.task_id, "✅ Подтвердить", "❌ Отменить")
            shot = run_dir / session.screenshot if session.screenshot else None
            sent = False
            if shot and shot.exists() and shot.stat().st_size > 0:
                try:
                    with shot.open("rb") as f:
                        await update_message.reply_photo(f, caption=caption, reply_markup=markup)
                    sent = True
                except Exception:  # noqa: BLE001 — любой сбой отправки фото не должен терять запрос подтверждения
                    log.exception("Не удалось отправить скриншот")
            if not sent:
                await update_message.reply_text(
                    caption + "\n⚠️ Скриншот недоступен (экран защищён от съёмки). Посмотрите экран телефона сами "
                    "и подтверждайте, только если уверены.",
                    reply_markup=markup,
                )
        else:
            icon = "✅" if report.status == "success" else "⚠️"
            await update_message.reply_text(f"{icon} {report.status}: {report.reason}{footer}")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await run_and_report(update.message, update.message.text.strip())


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    tg_file = await (msg.voice or msg.audio).get_file()
    audio = bytes(await tg_file.download_as_bytearray())
    try:
        text = await transcribe(audio)
    except SttError as exc:
        await msg.reply_text(f"Не удалось распознать голос: {exc}")
        return
    key = f"{msg.chat_id}{msg.message_id}"
    _pending_voice[key] = text
    await msg.reply_text(f"🎤 Распознано: «{text}»\nВыполнить?", reply_markup=_kb("vc", key, "▶ Выполнить", "✖ Отмена"))


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    kind, key, answer = q.data.split(":")
    yes = answer == "y"
    if kind == "vc":
        text = _pending_voice.pop(key, None)
        await q.edit_message_reply_markup(None)
        if not yes or text is None:
            await q.message.reply_text("Отменено.")
            return
        await run_and_report(q.message, text)
        return

    run_dir = RUNS / key
    try:
        session = ApprovalSession.load(run_dir)
        if session.state is not State.WAITING_FOR_APPROVAL:
            raise InvalidTransition(f"Запрос уже обработан ({session.state})")
        if yes:
            session.approve()
        else:
            session.cancel()
        session.save(run_dir)
    except (InvalidTransition, FileNotFoundError) as exc:
        await q.edit_message_reply_markup(None)
        await q.message.reply_text(f"⚠️ {exc}")
        return
    await q.edit_message_reply_markup(None)
    if not yes:
        session.finish()
        session.save(run_dir)
        await q.message.reply_text("❌ Отменено, действие не выполнено.")
        return
    try:
        await q.message.reply_text("✅ " + await execute_approved_tap(run_dir, load_settings().serial))
    except ApprovalError as exc:
        await q.message.reply_text(f"⚠️ {exc}")


def build_app() -> Application:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    ids = allowed_ids()
    if not token or not ids:
        raise SystemExit("Задайте TELEGRAM_BOT_TOKEN и TELEGRAM_ALLOWED_USER_IDS в .env")
    app = Application.builder().token(token).build()
    only = filters.User(user_id=ids)
    app.add_handler(CommandHandler("start", start, filters=only))
    app.add_handler(MessageHandler(only & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(only & (filters.VOICE | filters.AUDIO), on_voice))
    app.add_handler(CallbackQueryHandler(on_callback))  # доступ проверяется ниже
    app.add_handler(MessageHandler(~only, _deny))
    app.add_error_handler(_on_error)
    return app


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Ошибка обработчика", exc_info=context.error)
    message = getattr(update, "effective_message", None)
    if message is not None:
        await message.reply_text(f"⚠️ Внутренняя ошибка: {type(context.error).__name__}. Подробности в логе бота.")


async def _deny(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text("Доступ закрыт.")


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    build_app().run_polling()


if __name__ == "__main__":
    main()
