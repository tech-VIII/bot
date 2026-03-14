import os
import logging
import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests
from google import genai
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================
# Настройки
# =========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8079921309:AAFIzFSOOz33XBmGtJpv_54JD1moQt94EtI")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "AIzaSyDkQ6dj3GS18m_VdWASBPVVto6UEDYz33Y")
NEWS_API_KEY = os.getenv("NEWS_API_KEY", "2457ec1432514b35812eac8df0792e66")
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "8264586593")

MODEL_NAME = "gemini-2.5-flash"
MAX_HISTORY_MESSAGES = 12
MAX_TELEGRAM_MESSAGE_LEN = 4000
NEWS_LIMIT = 5

if not TELEGRAM_TOKEN:
    raise RuntimeError("Не задан TELEGRAM_TOKEN")

if not GEMINI_API_KEY:
    raise RuntimeError("Не задан GEMINI_API_KEY")

if not NEWS_API_KEY:
    raise RuntimeError("Не задан NEWS_API_KEY")

if not ADMIN_ID_RAW:
    raise RuntimeError("Не задан ADMIN_ID")

try:
    ADMIN_ID = int(ADMIN_ID_RAW)
except ValueError as exc:
    raise RuntimeError("ADMIN_ID должен быть числом") from exc

# =========================
# Логи
# =========================
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# =========================
# Gemini
# =========================
client = genai.Client(api_key=GEMINI_API_KEY)

# =========================
# Память и блокировки
# =========================
user_histories: dict[int, list[dict[str, str]]] = defaultdict(list)
user_locks: dict[int, asyncio.Lock] = {}


def get_user_lock(user_id: int) -> asyncio.Lock:
    if user_id not in user_locks:
        user_locks[user_id] = asyncio.Lock()
    return user_locks[user_id]


def build_dialog_text(history: list[dict[str, str]], user_text: str) -> str:
    system_prompt = (
        "Ты дружелюбный Telegram-бот. "
        "Всегда отвечай на русском языке. "
        "Отвечай кратко, ясно и по делу. "
        "Если не уверен, так и скажи."
    )

    parts = [f"Системная инструкция: {system_prompt}", "", "История диалога:"]

    for msg in history[-MAX_HISTORY_MESSAGES:]:
        role = "Пользователь" if msg["role"] == "user" else "Бот"
        parts.append(f"{role}: {msg['text']}")

    parts.append(f"Пользователь: {user_text}")
    parts.append("Бот:")

    return "\n".join(parts)


def trim_history(user_id: int) -> None:
    if len(user_histories[user_id]) > MAX_HISTORY_MESSAGES * 2:
        user_histories[user_id] = user_histories[user_id][-MAX_HISTORY_MESSAGES * 2:]


def split_long_message(text: str, max_len: int = MAX_TELEGRAM_MESSAGE_LEN) -> list[str]:
    if len(text) <= max_len:
        return [text]

    chunks = []
    start = 0

    while start < len(text):
        end = start + max_len

        if end >= len(text):
            chunks.append(text[start:])
            break

        split_pos = text.rfind("\n", start, end)
        if split_pos == -1:
            split_pos = text.rfind(" ", start, end)
        if split_pos == -1 or split_pos <= start:
            split_pos = end

        chunk = text[start:split_pos].strip()
        if chunk:
            chunks.append(chunk)

        start = split_pos

    return chunks


# =========================
# Новости
# =========================
def fetch_news(topic: str, page_size: int = NEWS_LIMIT) -> list[dict]:
    """
    Ищет новости по теме через NewsAPI.
    """
    url = "https://newsapi.org/v2/everything"

    # последние 3 дня, чтобы не тащить слишком старые статьи
    date_from = (datetime.now(timezone.utc) - timedelta(days=3)).date().isoformat()

    params = {
        "q": topic,
        "language": "ru",
        "sortBy": "publishedAt",
        "pageSize": page_size,
        "from": date_from,
        "apiKey": NEWS_API_KEY,
    }

    response = requests.get(url, params=params, timeout=20)
    response.raise_for_status()

    data = response.json()

    if data.get("status") != "ok":
        raise RuntimeError(data.get("message", "Не удалось получить новости"))

    return data.get("articles", [])


def format_news(topic: str, articles: list[dict]) -> str:
    if not articles:
        return (
            f"По запросу «{topic}» я не нашёл свежих новостей.\n"
            "Попробуй другую формулировку, например:\n"
            "/news политика\n"
            "/news спорт\n"
            "/news криптовалюта"
        )

    lines = [f"📰 Новости по теме: {topic}\n"]

    for i, article in enumerate(articles, start=1):
        title = article.get("title") or "Без заголовка"
        source = (article.get("source") or {}).get("name") or "Неизвестный источник"
        published_at = article.get("publishedAt") or ""
        url = article.get("url") or ""
        description = article.get("description") or ""

        # Красивее выводим дату, если пришла
        pretty_date = published_at
        try:
            pretty_date = datetime.fromisoformat(
                published_at.replace("Z", "+00:00")
            ).strftime("%d.%m.%Y %H:%M")
        except Exception:
            pass

        lines.append(
            f"{i}. {title}\n"
            f"Источник: {source}\n"
            f"Дата: {pretty_date}\n"
            f"{description}\n"
            f"{url}\n"
        )

    return "\n".join(lines).strip()


# =========================
# Команды
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return

    await update.message.reply_text(
        "Привет. Я Telegram-бот с Gemini и новостями.\n\n"
        "Команды:\n"
        "/start - запуск\n"
        "/help - помощь\n"
        "/reset - очистить историю\n"
        "/operator - вызвать оператора\n"
        "/news тема - показать свежие новости\n\n"
        "Примеры:\n"
        "/news политика\n"
        "/news технологии\n"
        "/news футбол"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return

    await update.message.reply_text(
        "Я умею:\n"
        "1) отвечать на сообщения через Gemini\n"
        "2) показывать новости по команде /news\n"
        "3) вызывать оператора по команде /operator\n\n"
        "Пример команды новостей:\n"
        "/news политика"
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None or update.effective_user is None:
        return

    user_id = update.effective_user.id
    user_histories.pop(user_id, None)
    await update.message.reply_text("История диалога очищена.")


async def call_operator(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None or update.effective_user is None:
        return

    user = update.effective_user
    chat = update.effective_chat

    username = f"@{user.username}" if user.username else "нет username"
    full_name = user.full_name
    user_id = user.id
    chat_id = chat.id if chat else "неизвестно"

    text = (
        "🚨 Пользователь вызывает оператора\n\n"
        f"Имя: {full_name}\n"
        f"Username: {username}\n"
        f"User ID: {user_id}\n"
        f"Chat ID: {chat_id}"
    )

    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=text)
        await update.message.reply_text("Оператор уведомлён. Пожалуйста, подождите.")
    except Exception:
        logger.exception("Не удалось отправить сообщение админу")
        await update.message.reply_text("Не удалось уведомить оператора.")


async def news_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return

    topic = " ".join(context.args).strip()

    if not topic:
        await update.message.reply_text(
            "После команды /news укажи тему.\n\n"
            "Пример:\n"
            "/news политика"
        )
        return

    try:
        if update.effective_chat is not None:
            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id,
                action=ChatAction.TYPING,
            )

        articles = await asyncio.to_thread(fetch_news, topic, NEWS_LIMIT)
        message = format_news(topic, articles)

        for chunk in split_long_message(message):
            await update.message.reply_text(chunk, disable_web_page_preview=True)

    except requests.HTTPError as e:
        logger.exception("Ошибка HTTP при получении новостей")
        await update.message.reply_text(
            f"Ошибка при запросе новостей: {e.response.status_code}"
        )
    except Exception:
        logger.exception("Ошибка в news_command")
        await update.message.reply_text(
            "Не удалось получить новости. Попробуй позже."
        )


# =========================
# Ответ от Gemini
# =========================
async def ai_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None or update.message.text is None or update.effective_user is None:
        return

    user_id = update.effective_user.id
    user_text = update.message.text.strip()

    if not user_text:
        await update.message.reply_text("Пустое сообщение обработать нельзя.")
        return

    lock = get_user_lock(user_id)

    async with lock:
        try:
            logger.info("User %s: %r", user_id, user_text)

            history = user_histories[user_id]
            prompt = build_dialog_text(history, user_text)

            if update.effective_chat is not None:
                await context.bot.send_chat_action(
                    chat_id=update.effective_chat.id,
                    action=ChatAction.TYPING,
                )

            response = await asyncio.to_thread(
                client.models.generate_content,
                model=MODEL_NAME,
                contents=prompt,
            )

            answer = (response.text or "").strip()

            if not answer:
                answer = "Я не смог сгенерировать ответ. Попробуй ещё раз."

            history.append({"role": "user", "text": user_text})
            history.append({"role": "assistant", "text": answer})
            trim_history(user_id)

            for chunk in split_long_message(answer):
                await update.message.reply_text(chunk)

        except Exception:
            logger.exception("Ошибка в ai_reply")
            await update.message.reply_text("Произошла внутренняя ошибка. Попробуй позже.")


# =========================
# Глобальный обработчик ошибок
# =========================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Unhandled exception", exc_info=context.error)


# =========================
# Запуск
# =========================
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("operator", call_operator))
    app.add_handler(CommandHandler("news", news_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, ai_reply))

    app.add_error_handler(error_handler)

    logger.info("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
