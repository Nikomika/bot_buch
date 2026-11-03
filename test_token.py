import os
import time

import httpx
from dotenv import load_dotenv

load_dotenv()

TOKEN_URL = os.getenv(
    "TOKEN_URL",
    "https://belorusneft.by/identity/connect/token",
)

LOGIN = os.getenv("RCP_LOGIN")
PASSWORD = os.getenv("RCP_PASSWORD")


def main():
    if not LOGIN or not PASSWORD:
        print(
            "Не заданы RCP_LOGIN или RCP_PASSWORD. "
            "Добавьте их в .env или переменные окружения хостинга.",
            flush=True,
        )
        return

    # Это отдельные лимиты операций, а не общий лимит всего запроса.
    timeout = httpx.Timeout(
        connect=120.0,
        read=120.0,
        write=30.0,
        pool=30.0,
    )

    print("Запрашиваем токен. Ожидание соединения — до 120 секунд.", flush=True)
    started = time.monotonic()

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                TOKEN_URL,
                data={
                    "grant_type": "password",
                    "username": LOGIN,
                    "password": PASSWORD,
                    "client_id": "rcp.web",
                    "client_secret": "secret",
                    "scope": "rcp.api",
                },
            )

        print(f"HTTP-статус: {response.status_code}", flush=True)

        if not response.is_success:
            print(
                "Сервер ответил, но не выдал успешный ответ. "
                "Соединение установлено. Тело ответа не выводим "
                "во избежание утечки данных.",
                flush=True,
            )
            return

        try:
            data = response.json()
        except ValueError:
            print("Ответ сервера не является корректным JSON.", flush=True)
            return

        token = data.get("access_token")

        if not token:
            print("Ответ получен, но поля access_token в нём нет.", flush=True)
            return

        print("УСПЕХ: токен получен.", flush=True)
        print(f"Тип токена: {data.get('token_type', 'не указан')}", flush=True)
        print(
            f"Срок действия, секунд: {data.get('expires_in', 'не указан')}",
            flush=True,
        )
        print("Сам токен не выводится в логи.", flush=True)

    except httpx.ConnectTimeout:
        print(
            "ConnectTimeout: соединение не установилось "
            "за 120 секунд.",
            flush=True,
        )

    except httpx.ReadTimeout:
        print(
            "ReadTimeout: превышено время ожидания данных от сервера.",
            flush=True,
        )
    except httpx.RequestError as error:
        print(
            f"Ошибка сетевого запроса: {type(error).__name__}",
            flush=True,
        )
        print(f"Подробности: {error}", flush=True)

        cause = error.__cause__
        level = 1

        while cause is not None and level <= 5:
            print(
                f"Причина {level}: "
                f"{type(cause).__name__}: {cause}",
                flush=True,
            )
            cause = cause.__cause__
            level += 1

    finally:
        elapsed = time.monotonic() - started
        print(f"Прошло времени: {elapsed:.1f} сек.", flush=True)


if __name__ == "__main__":
    main()
