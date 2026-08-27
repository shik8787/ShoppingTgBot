from __future__ import annotations

import asyncio
from contextlib import suppress
import logging
from pathlib import Path
import re

from telegram import (
    BotCommand,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.config import Settings, get_settings
from app.repository import (
    AddItemResult,
    ListLimitReachedError,
    ShoppingItem,
    ShoppingRepository,
)


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

HEARTBEAT_PATH = Path("/tmp/shopping-heartbeat")
ADD_PROMPT_KEY = "shopping_add_prompt"
SHOPPING_LIST_HEADER = re.compile(
    r"^\s*список\s+покупок\s*:?\s*(?:\r?\n|$)",
    re.IGNORECASE,
)
LIST_ITEM_PREFIX = re.compile(
    r"^\s*(?:(?:[-*•–—]|\d+[.)]|[☐☑✅⬜])\s*)"
)


def build_list_view(
    items: list[ShoppingItem],
) -> tuple[str, InlineKeyboardMarkup]:
    remaining = sum(not item.is_checked for item in items)
    lines = ["🛒 Список покупок", ""]
    if items:
        for index, item in enumerate(items, start=1):
            marker = "✅" if item.is_checked else "⬜"
            lines.append(f"{index}. {marker} {item.name}")
        lines.extend(["", f"Осталось: {remaining} из {len(items)}"])
    else:
        lines.append("Список пуст.")

    keyboard = [
        [
            InlineKeyboardButton(
                f"{'✅' if item.is_checked else '⬜'} {_button_name(item.name)}",
                callback_data=f"toggle:{item.id}",
            )
        ]
        for item in items
    ]
    keyboard.extend(
        [
            [
                InlineKeyboardButton("➕ Добавить", callback_data="add"),
                InlineKeyboardButton("🔄 Обновить", callback_data="refresh"),
            ],
            [
                InlineKeyboardButton(
                    "🧹 Удалить купленное",
                    callback_data="remove_checked",
                )
            ],
        ]
    )
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not _is_allowed(update, context):
        return
    await update.effective_message.reply_text(
        "Я веду общий список покупок для этого чата.\n"
        "Нажимайте на покупки, чтобы переключать галочки.\n\n"
        "/add молоко — добавить покупку\n"
        "/list — показать список\n"
        "/clear — удалить отмеченные покупки\n"
        "/cancel — отменить добавление"
    )
    await _send_list(update, context)


async def list_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not _is_allowed(update, context):
        return
    await _send_list(update, context)


async def add_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not _is_allowed(update, context):
        return
    name = " ".join(context.args)
    if not name.strip():
        await _prompt_for_item(update, context)
        return
    await _add_item(update, context, name)


async def clear_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not _is_allowed(update, context):
        return
    repository = _repository(context)
    removed = await repository.remove_checked(update.effective_chat.id)
    await update.effective_message.reply_text(
        f"Удалено отмеченных покупок: {removed}."
    )
    await _send_list(update, context)


async def cancel_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    context.user_data.pop(ADD_PROMPT_KEY, None)
    if _is_allowed(update, context):
        await update.effective_message.reply_text("Добавление отменено.")


async def text_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not _is_allowed(update, context):
        return

    message = update.effective_message
    shopping_list = parse_shopping_list_message(message.text)
    if shopping_list is not None:
        context.user_data.pop(ADD_PROMPT_KEY, None)
        await _add_shopping_list(update, context, shopping_list)
        return

    prompt = context.user_data.get(ADD_PROMPT_KEY)
    is_prompt_reply = (
        prompt is not None
        and prompt["chat_id"] == update.effective_chat.id
        and message.reply_to_message is not None
        and message.reply_to_message.message_id == prompt["message_id"]
    )
    if is_prompt_reply:
        context.user_data.pop(ADD_PROMPT_KEY, None)
        await _add_item(update, context, message.text)
        return

    if update.effective_chat.type == ChatType.PRIVATE:
        await _add_item(update, context, message.text)


async def callback_query(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    if query is None or query.message is None:
        return
    if not _is_allowed(update, context):
        await query.answer("Этот чат не разрешён.", show_alert=True)
        return

    repository = _repository(context)
    chat_id = query.message.chat.id
    data = query.data or ""

    if data.startswith("toggle:"):
        try:
            item_id = int(data.removeprefix("toggle:"))
        except ValueError:
            await query.answer("Некорректная покупка.", show_alert=True)
            return
        item = await repository.toggle_item(chat_id, item_id)
        if item is None:
            await query.answer("Покупка уже удалена.", show_alert=True)
            await _edit_list(query, repository, chat_id)
            return
        await query.answer("Куплено" if item.is_checked else "Вернул в список")
        await _edit_list(query, repository, chat_id)
        return

    if data == "add":
        await query.answer()
        user = query.from_user
        prompt = await query.message.reply_text(
            f"{user.mention_html()}, что добавить в список?",
            parse_mode=ParseMode.HTML,
            reply_markup=ForceReply(
                selective=True,
                input_field_placeholder="Например: молоко",
            ),
        )
        context.user_data[ADD_PROMPT_KEY] = {
            "chat_id": chat_id,
            "message_id": prompt.message_id,
        }
        return

    if data == "remove_checked":
        removed = await repository.remove_checked(chat_id)
        await query.answer(f"Удалено: {removed}")
        await _edit_list(query, repository, chat_id)
        return

    if data == "refresh":
        await query.answer("Список обновлён")
        await _edit_list(query, repository, chat_id)
        return

    await query.answer("Неизвестное действие.", show_alert=True)


async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    logger.error("Unhandled Telegram update error", exc_info=context.error)


async def post_init(application: Application) -> None:
    await application.bot_data["repository"].initialize()
    await application.bot.set_my_commands(
        [
            BotCommand("list", "Показать список покупок"),
            BotCommand("add", "Добавить покупку"),
            BotCommand("clear", "Удалить отмеченные покупки"),
            BotCommand("cancel", "Отменить добавление"),
            BotCommand("start", "Помощь"),
        ]
    )
    application.bot_data["heartbeat_task"] = asyncio.create_task(_heartbeat_loop())


async def post_shutdown(application: Application) -> None:
    task = application.bot_data.pop("heartbeat_task", None)
    if task is None:
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def _send_list(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    repository = _repository(context)
    items = await repository.list_items(update.effective_chat.id)
    text, keyboard = build_list_view(items)
    await update.effective_message.reply_text(text, reply_markup=keyboard)


async def _edit_list(
    query,
    repository: ShoppingRepository,
    chat_id: int,
) -> None:
    items = await repository.list_items(chat_id)
    text, keyboard = build_list_view(items)
    try:
        await query.edit_message_text(text, reply_markup=keyboard)
    except BadRequest as error:
        if "Message is not modified" not in str(error):
            raise


async def _prompt_for_item(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    prompt = await update.effective_message.reply_text(
        "Что добавить в список?",
        reply_markup=ForceReply(
            selective=True,
            input_field_placeholder="Например: молоко",
        ),
    )
    context.user_data[ADD_PROMPT_KEY] = {
        "chat_id": update.effective_chat.id,
        "message_id": prompt.message_id,
    }


async def _add_item(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    name: str,
) -> None:
    repository = _repository(context)
    try:
        result = await repository.add_item(
            update.effective_chat.id,
            name,
            update.effective_user.id if update.effective_user else None,
        )
    except ValueError:
        await update.effective_message.reply_text(
            "Название должно содержать от 1 до 60 символов."
        )
        return
    except ListLimitReachedError:
        await update.effective_message.reply_text(
            "Список заполнен. Отметьте покупки и удалите купленное."
        )
        return

    await update.effective_message.reply_text(_add_result_text(result))
    await _send_list(update, context)


async def _add_shopping_list(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    names: list[str],
) -> None:
    if not names:
        await update.effective_message.reply_text(
            "После строки «Список покупок» добавьте покупки, каждую с новой строки."
        )
        return

    repository = _repository(context)
    created = 0
    reactivated = 0
    duplicates = 0
    skipped = 0
    limit_reached = False

    for name in names:
        try:
            result = await repository.add_item(
                update.effective_chat.id,
                name,
                update.effective_user.id if update.effective_user else None,
            )
        except ValueError:
            skipped += 1
            continue
        except ListLimitReachedError:
            limit_reached = True
            break

        if result.created:
            created += 1
        elif result.reactivated:
            reactivated += 1
        else:
            duplicates += 1

    summary = [f"Добавлено: {created}"]
    if reactivated:
        summary.append(f"возвращено: {reactivated}")
    if duplicates:
        summary.append(f"уже было: {duplicates}")
    if skipped:
        summary.append(f"пропущено: {skipped}")
    if limit_reached:
        summary.append("достигнут лимит списка")
    await update.effective_message.reply_text(", ".join(summary) + ".")
    await _send_list(update, context)


def _add_result_text(result: AddItemResult) -> str:
    if result.created:
        return f"Добавлено: {result.item.name}"
    if result.reactivated:
        return f"Вернул в список: {result.item.name}"
    return f"Уже есть в списке: {result.item.name}"


def _repository(context: ContextTypes.DEFAULT_TYPE) -> ShoppingRepository:
    return context.application.bot_data["repository"]


def _settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.application.bot_data["settings"]


def _is_allowed(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    chat = update.effective_chat
    if chat is None:
        return False
    allowed_chat_ids = _settings(context).allowed_chat_ids
    return not allowed_chat_ids or chat.id in allowed_chat_ids


def _button_name(name: str) -> str:
    return name if len(name) <= 45 else name[:44] + "…"


def parse_shopping_list_message(text: str) -> list[str] | None:
    match = SHOPPING_LIST_HEADER.match(text)
    if match is None:
        return None

    items = []
    for line in text[match.end():].splitlines():
        item = LIST_ITEM_PREFIX.sub("", line, count=1).strip()
        if item:
            items.append(item)
    return items


async def _heartbeat_loop() -> None:
    while True:
        HEARTBEAT_PATH.touch()
        await asyncio.sleep(15)


def main() -> None:
    settings = get_settings()
    repository = ShoppingRepository(
        settings.database_path,
        settings.max_items_per_list,
    )
    application = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .concurrent_updates(False)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.bot_data["settings"] = settings
    application.bot_data["repository"] = repository
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("list", list_command))
    application.add_handler(CommandHandler("add", add_command))
    application.add_handler(CommandHandler("clear", clear_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CallbackQueryHandler(callback_query))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_message)
    )
    application.add_error_handler(error_handler)

    logger.info("Shopping Telegram bot started")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
