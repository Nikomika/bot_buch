import asyncio
import logging
import os
import ssl
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv


# =========================
# Настройки и журнал
# =========================

PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_DIR / ".env", encoding="utf-8-sig")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


def required_env(name: str) -> str:
    value = os.getenv(name)

    if value is None or not value.strip():
        raise RuntimeError(
            f"Не задана переменная {name}. "
            f"Проверьте файл {PROJECT_DIR / '.env'}."
        )

    return value


def int_setting(name: str, default: str) -> int:
    try:
        return int(os.getenv(name, default).strip())
    except ValueError as error:
        raise RuntimeError(
            f"Переменная {name} должна содержать целое число."
        ) from error


BOT_TOKEN = required_env("BOT_TOKEN").strip()
RCP_LOGIN = required_env("RCP_LOGIN").strip()
RCP_PASSWORD = required_env("RCP_PASSWORD")

# Полные адреса берутся из .env без добавления частей пути.
TOKEN_URL = required_env("TOKEN_URL").strip()
BALANCE_URL = required_env("BALANCE_URL").strip()

try:
    ALLOWED_IDS = {
        int(value.strip())
        for value in required_env("ALLOWED_IDS").split(",")
        if value.strip()
    }
except ValueError as error:
    raise RuntimeError(
        "ALLOWED_IDS должен содержать числовые Telegram ID "
        "через запятую."
    ) from error

if not ALLOWED_IDS:
    raise RuntimeError("ALLOWED_IDS не должен быть пустым.")

group_id_text = os.getenv("GROUP_CHAT_ID", "").strip()

try:
    GROUP_CHAT_ID = int(group_id_text) if group_id_text else None
except ValueError as error:
    raise RuntimeError(
        "GROUP_CHAT_ID должен содержать числовой ID группы."
    ) from error

if GROUP_CHAT_ID is not None and GROUP_CHAT_ID >= 0:
    raise RuntimeError(
        "Ожидался отрицательный Telegram ID группы. "
        "Получите его командой /chatid в нужной группе."
    )

REPORT_HOUR = int_setting("REPORT_HOUR", "9")
REPORT_MINUTE = int_setting("REPORT_MINUTE", "5")
REPORT_DAYS = os.getenv("REPORT_DAYS", "mon-fri").strip()
REPORT_TIMEZONE = os.getenv(
    "REPORT_TIMEZONE",
    "Europe/Minsk",
).strip()

if not 0 <= REPORT_HOUR <= 23:
    raise RuntimeError("REPORT_HOUR должен быть от 0 до 23.")

if not 0 <= REPORT_MINUTE <= 59:
    raise RuntimeError("REPORT_MINUTE должен быть от 0 до 59.")

try:
    REPORT_TZ = ZoneInfo(REPORT_TIMEZONE)
except ZoneInfoNotFoundError as error:
    raise RuntimeError(
        "Не найден часовой пояс. Проверьте REPORT_TIMEZONE "
        "и установите пакет tzdata."
    ) from error


dp = Dispatcher()

cached_token: str | None = None
token_expires_at = 0.0
token_lock = asyncio.Lock()


# =========================
# TLS
# =========================

def make_ssl_context() -> ssl.SSLContext:
    context = ssl.create_default_context()

    # Сохраняем совместимость из работающего варианта.
    # Проверка сертификата и имени сервера остаётся включённой.
    legacy_option = getattr(ssl, "OP_LEGACY_SERVER_CONNECT", None)

    if legacy_option is None:
        raise RuntimeError(
            "В этой сборке Python отсутствует "
            "ssl.OP_LEGACY_SERVER_CONNECT."
        )

    context.options |= legacy_option
    return context


# =========================
# Авторизация API
# =========================

async def get_token(client: httpx.AsyncClient) -> str:
    global cached_token, token_expires_at

    async with token_lock:
        if cached_token and time.monotonic() < token_expires_at:
            return cached_token

        response = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "password",
                "username": RCP_LOGIN,
                "password": RCP_PASSWORD,
                "client_id": "rcp.web",
                "client_secret": "secret",
                "scope": "rcp.api",
            },
        )

        if response.is_error:
            logger.warning(
                "Авторизация API: HTTP %s.",
                response.status_code,
            )

        response.raise_for_status()
        data = response.json()

        if not isinstance(data, dict):
            raise ValueError("Некорректный ответ авторизации.")

        token = data.get("access_token")

        if not isinstance(token, str) or not token.strip():
            raise ValueError("В ответе отсутствует access_token.")

        expires_in = float(data.get("expires_in", 3600))

        if not 0 < expires_in < float("inf"):
            raise ValueError("Некорректное время действия токена.")

        safety_margin = min(60.0, expires_in * 0.1)

        cached_token = token
        token_expires_at = (
            time.monotonic() + expires_in - safety_margin
        )

        logger.info("Токен API получен; значение токена скрыто.")
        return token


# =========================
# Получение баланса
# =========================

async def get_balance() -> dict:
    global cached_token, token_expires_at

    timeout = httpx.Timeout(
        connect=30.0,
        read=30.0,
        write=30.0,
        pool=30.0,
    )

    async with httpx.AsyncClient(
        timeout=timeout,
        verify=make_ssl_context(),
    ) as client:
        for attempt in range(2):
            token = await get_token(client)

            response = await client.post(
                BALANCE_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )

            if response.status_code == 401 and attempt == 0:
                logger.warning(
                    "API вернул HTTP 401. Обновляю токен."
                )

                async with token_lock:
                    if cached_token == token:
                        cached_token = None
                        token_expires_at = 0.0

                continue

            if response.is_error:
                logger.warning(
                    "Получение баланса: HTTP %s; путь: %s",
                    response.status_code,
                    response.request.url.path,
                )

            response.raise_for_status()

            if not response.content:
                raise ValueError("API вернул пустой ответ.")

            data = response.json()

            if not isinstance(data, dict):
                raise ValueError("Ожидался JSON-объект.")

            logger.info("Ответ с балансом получен.")
            return data

    raise RuntimeError("Не удалось получить баланс.")


# =========================
# Форматирование сообщения
# =========================

def format_summa(value) -> str:
    if value is None:
        return "нет данных"

    try:
        amount = Decimal(str(value))

        if not amount.is_finite():
            return "нет данных"

        return f"{amount:.2f}"

    except (InvalidOperation, ValueError, TypeError):
        return "не удалось прочитать сумму"


def balance_message(data: dict) -> str:
    client_name = str(data.get("clientName") or "нет данных")
    summa = format_summa(data.get("summa"))

    return (
        f"Клиент: {client_name}\n"
        f"Остаток по договору: {summa}"
    )


def api_error_message(error: Exception) -> str:
    if isinstance(error, httpx.TimeoutException):
        logger.warning("Превышено время ожидания API.")
        return "Не удалось дождаться ответа API. Попробуйте позже."

    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code

        logger.warning(
            "Ошибка API: HTTP %s; путь: %s",
            status,
            error.request.url.path,
        )

        if status in (400, 401, 403):
            return (
                f"API вернул HTTP {status}. "
                "Проверьте авторизацию и права доступа."
            )

        if status == 404:
            return (
                "API вернул HTTP 404. "
                "Проверьте BALANCE_URL в .env."
            )

        return f"API вернул HTTP {status}. Попробуйте позже."

    if isinstance(error, httpx.RequestError):
        logger.error(
            "Ошибка соединения с API: %s",
            type(error).__name__,
        )
        return "Не удалось подключиться к API."

    if isinstance(
        error,
        (ValueError, TypeError, KeyError, OverflowError),
    ):
        logger.warning(
            "Некорректный ответ API: %s",
            type(error).__name__,
        )
        return "API вернул пустой или некорректный ответ."

    logger.error(
        "Непредвиденная ошибка: %s",
        type(error).__name__,
    )
    return "Внутренняя ошибка. Проверьте журнал программы."


# =========================
# Проверка доступа
# =========================

def authorized_user(message: Message) -> bool:
    return (
        message.from_user is not None
        and message.from_user.id in ALLOWED_IDS
    )


def allowed_private(message: Message) -> bool:
    return (
        authorized_user(message)
        and message.chat.type == "private"
    )


# =========================
# Команды Telegram
# =========================

@dp.message(Command("start"))
async def start_handler(message: Message):
    if not allowed_private(message):
        return

    await message.answer(
        "Бот подключён.\n"
        "/balance — получить остаток по договору.\n"
        "/chatid — узнать ID текущего чата."
    )


@dp.message(Command("chatid"))
async def chatid_handler(message: Message):
    # Команда доступна разрешённому пользователю
    # как в личном чате, так и в группе.
    if not authorized_user(message):
        return

    await message.answer(
        f"ID этого чата: {message.chat.id}\n"
        f"Тип чата: {message.chat.type}"
    )


@dp.message(Command("balance"))
async def balance_handler(message: Message):
    # Ручной запрос баланса разрешён только в личном чате.
    if not allowed_private(message):
        return

    await message.answer("Запрашиваю данные по договору…")

    try:
        data = await get_balance()
        text = balance_message(data)
    except Exception as error:
        text = api_error_message(error)

    await message.answer(text)


# =========================
# Автоматическая рассылка
# =========================

async def scheduled_report(bot: Bot):
    if GROUP_CHAT_ID is None:
        return

    logger.info("Запущено получение баланса по расписанию.")

    try:
        data = await get_balance()
        text = balance_message(data)
    except Exception as error:
        details = api_error_message(error)
        text = (
            "Не удалось получить баланс по расписанию.\n"
            f"{details}"
        )

    try:
        await bot.send_message(
            chat_id=GROUP_CHAT_ID,
            text=text,
        )
    except Exception as error:
        # Не выводим полный текст исключения,
        # чтобы не раскрывать детали запросов.
        logger.error(
            "Не удалось отправить сообщение в группу: %s. "
            "Проверьте GROUP_CHAT_ID, наличие бота в группе "
            "и разрешение отправлять сообщения.",
            type(error).__name__,
        )
        return

    logger.info("Сообщение по расписанию отправлено в группу.")


# =========================
# Запуск
# =========================

async def main():
    make_ssl_context()

    scheduler = AsyncIOScheduler(timezone=REPORT_TZ)

    async with Bot(token=BOT_TOKEN) as bot:
        try:
            if GROUP_CHAT_ID is not None:
                trigger = CronTrigger(
                    day_of_week=REPORT_DAYS,
                    hour=REPORT_HOUR,
                    minute=REPORT_MINUTE,
                    second=0,
                    timezone=REPORT_TZ,
                )

                scheduler.add_job(
                    scheduled_report,
                    trigger=trigger,
                    kwargs={"bot": bot},
                    id="weekday_balance_report",
                    replace_existing=True,
                    max_instances=1,
                    coalesce=True,
                    misfire_grace_time=60,
                )

                scheduler.start()

                job = scheduler.get_job(
                    "weekday_balance_report"
                )

                logger.info(
                    "Расписание: %s в %02d:%02d; часовой пояс: %s.",
                    REPORT_DAYS,
                    REPORT_HOUR,
                    REPORT_MINUTE,
                    REPORT_TIMEZONE,
                )

                if job is not None:
                    logger.info(
                        "Следующая отправка: %s",
                        job.next_run_time,
                    )
            else:
                logger.warning(
                    "GROUP_CHAT_ID не задан. "
                    "Автоматическая рассылка отключена. "
                    "Получите ID группы командой /chatid."
                )

            logger.info("Бот запускается. Ожидание сообщений.")

            await dp.start_polling(
                bot,
                close_bot_session=False,
            )

        finally:
            if scheduler.running:
                scheduler.shutdown(wait=False)
                await asyncio.sleep(0)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем.")
