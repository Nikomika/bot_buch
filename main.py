import os
import time
import asyncio
import logging
from datetime import datetime

import httpx
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message

load_dotenv()
logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ALLOWED = {int(x) for x in os.getenv("ALLOWED_IDS", "").split(",") if x.strip()}
TOKEN_URL = os.getenv("TOKEN_URL")
BASE_URL = os.getenv("BASE_URL")

_tok = {"v": None, "exp": 0.0}


async def get_token(c: httpx.AsyncClient) -> str:
    if _tok["v"] and time.time() < _tok["exp"] - 60:
        return _tok["v"]
    r = await c.post(TOKEN_URL, data={
        "grant_type": "password",
        "username": os.getenv("RCP_LOGIN"),
        "password": os.getenv("RCP_PASSWORD"),
        "client_id": "rcp.web",
        "client_secret": "secret",
        "scope": "rcp.api",
    })
    r.raise_for_status()
    j = r.json()
    _tok["v"] = j["access_token"]
    _tok["exp"] = time.time() + j.get("expires_in", 3600)
    return _tok["v"]


async def call_api(method: str, path: str, body=None) -> httpx.Response:
    async with httpx.AsyncClient(timeout=20) as c:
        headers = {
            "Authorization": f"Bearer {await get_token(c)}",
            "Content-Type": "application/json",
        }
        return await c.request(method, f"{BASE_URL}/{path}",
                               headers=headers, json=body)


def fmt_date(s):
    if not s or str(s).startswith("0001"):
        return "нет данных"
    try:
        return datetime.fromisoformat(s).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return str(s)


def allowed(m: Message) -> bool:
    return bool(m.from_user) and m.from_user.id in ALLOWED


dp = Dispatcher()


@dp.message(Command("start"))
async def start(m: Message):
    if not allowed(m):
        return await m.answer("Доступ закрыт.")
    await m.answer("Привет! Команда /balance покажет остаток по договору.")


@dp.message(Command("balance"))
async def balance(m: Message):
    if not allowed(m):
        return await m.answer("Доступ закрыт.")
    try:
        r = await call_api("POST", "balanceLight")
    except httpx.HTTPError as e:
        logging.exception(e)
        return await m.answer("Сервис недоступен, попробуйте позже.")
    if r.status_code != 200:
        return await m.answer(f"Ошибка API: {r.status_code}")
    try:
        d = r.json()
    except ValueError:
        return await m.answer("API вернул пустой или неверный ответ.")

    summa = d.get("summa")
    summa_txt = f"{summa:.2f}" if summa is not None else "нет данных"
    await m.answer(
        f"Клиент: {d.get('clientName') or 'нет данных'}\n"
        f"Остаток: {summa_txt}\n"
        f"Дата пересчёта: {fmt_date(d.get('date'))}"
    )


async def main():
    bot = Bot(BOT_TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())