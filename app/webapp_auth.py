"""
Проверка initData Telegram Mini App (WebApp).

Когда пользователь открывает Mini App внутри Telegram, клиент передаёт
приложению строку initData, подписанную ботом. Любой запрос от фронтенда
к нашему API должен приносить эту строку — так бэкенд убеждается, что
запрос реально пришёл из Telegram-клиента ЭТОГО бота, а не подделан
снаружи (без этой проверки любой смог бы дёргать API от чужого chat_id).

Алгоритм — официальный:
https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""

import hashlib
import hmac
import json
import time
from typing import Optional
from urllib.parse import parse_qsl

# Старше суток initData не принимаем — защита от повторного использования
# перехваченной когда-то строки.
MAX_AUTH_AGE_SEC = 24 * 60 * 60


class InitDataError(Exception):
    """initData отсутствует, подпись не сходится или истекла."""


def validate_init_data(init_data: str, bot_token: str, max_age_sec: int = MAX_AUTH_AGE_SEC) -> dict:
    """Возвращает распарсенные поля initData (в т.ч. 'user' как dict),
    либо бросает InitDataError."""
    if not init_data:
        raise InitDataError("empty init_data")
    if not bot_token:
        # Не должно происходить в проде (BOT_TOKEN обязателен для ROLE=bot),
        # но лучше явно упасть, чем молча пропустить неподписанные запросы.
        raise InitDataError("BOT_TOKEN is not configured on the server")

    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError as e:
        raise InitDataError(f"malformed init_data: {e}")

    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise InitDataError("missing hash")

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        raise InitDataError("signature mismatch")

    # Без auth_date возраст не проверить — такую строку не принимаем,
    # иначе она жила бы вечно.
    try:
        age = time.time() - int(pairs["auth_date"])
    except (KeyError, ValueError):
        raise InitDataError("missing or bad auth_date")
    if age > max_age_sec:
        raise InitDataError("init_data expired")

    result = dict(pairs)
    if "user" in result:
        try:
            result["user"] = json.loads(result["user"])
        except (TypeError, ValueError):
            pass
    return result


def extract_chat_id(parsed: dict) -> Optional[int]:
    """chat_id пользователя = его Telegram user id — в личном чате с
    ботом (а Mini App у нас открывается только там) это одно и то же,
    вся остальная БД (subscribers, access, zones...) уже ключуется по
    chat_id именно в этом смысле."""
    user = parsed.get("user")
    if isinstance(user, dict) and "id" in user:
        try:
            return int(user["id"])
        except (TypeError, ValueError):
            return None
    return None
