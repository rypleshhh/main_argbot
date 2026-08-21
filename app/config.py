import os

# Роль процесса: "scraper" (мощный сервер за NAT, парсит и стучится
# наружу), "bot" (лёгкий сервер с белым IP, слушает + Telegram),
# "monolith" (всё в одном процессе, для теста на одной машине).
ROLE = os.environ.get("ROLE", "monolith")

# Общий секрет между сервером-скрапером и сервером-ботом.
API_TOKEN = os.environ.get("API_TOKEN", "")

# --- нужны только на сервере-скрапере (ROLE=scraper или monolith) ---
SCAN_INTERVAL_MINUTES = int(os.environ.get("SCAN_INTERVAL_MINUTES", "12"))
MAX_PAYMENT_AGE_MINUTES = int(os.environ.get("MAX_PAYMENT_AGE_MINUTES", "20"))
STEP_LON = float(os.environ.get("STEP_LON", "0.3"))
STEP_LAT = float(os.environ.get("STEP_LAT", "0.2"))
REQUEST_DELAY_SEC = float(os.environ.get("REQUEST_DELAY_SEC", "0.4"))
DB_PATH = os.environ.get("DB_PATH", "/app/data/stations.db")

# Публичные адреса лёгких серверов (белый IP) — сюда скрапер стучится
# сам, чтобы отправить уведомления и забрать команды пользователя.
# Один скрапер может обслуживать НЕСКОЛЬКО ботов (например, основной
# бесплатный/триальный бот и платный клон) — тогда заводите пары
# LIGHT_SERVER_URL_2 / API_TOKEN_2, LIGHT_SERVER_URL_3 / API_TOKEN_3 и
# т.д. в .env.scraper. Первая пара — по обратной совместимости через
# LIGHT_SERVER_URL/API_TOKEN (см. ниже), без суффикса.
LIGHT_SERVER_URL = os.environ.get("LIGHT_SERVER_URL", "")


def _load_light_servers() -> list[dict]:
    servers = []
    if LIGHT_SERVER_URL:
        servers.append({
            "name": "bot1",
            "url": LIGHT_SERVER_URL.rstrip("/"),
            "token": API_TOKEN,
        })
    i = 2
    while True:
        url = os.environ.get(f"LIGHT_SERVER_URL_{i}")
        if not url:
            break
        servers.append({
            "name": f"bot{i}",
            "url": url.rstrip("/"),
            "token": os.environ.get(f"API_TOKEN_{i}", ""),
        })
        i += 1
    return servers


# Список всех лёгких серверов, с которыми работает скрапер. Используется
# в app/scheduler.py (пуш уведомлений/списка городов) и app/main_scraper.py
# (опрос команд) — вместо единственного LIGHT_SERVER_URL.
LIGHT_SERVERS = _load_light_servers()

# Станции с меньшим числом недавних оплат считаем шумом/погрешностью —
# по ним недостаточно данных, чтобы доверять статусу топлива.
MIN_OPERATIONS_COUNT = int(os.environ.get("MIN_OPERATIONS_COUNT", "3"))

# Как часто скрапер спрашивает лёгкий сервер про новые команды, сек.
COMMAND_POLL_INTERVAL_SEC = int(os.environ.get("COMMAND_POLL_INTERVAL_SEC", "5"))

# Targeted-режим: после первого полного прохода по GEOBOXES (нужен, чтобы
# наполнить каталог станций — например, при GEOBOXES на всю Россию)
# ФОНОВЫЙ цикл (каждые SCAN_INTERVAL_MINUTES) сканирует не всю область
# целиком, а только маленькие боксы вокруг станций, которые реально
# сохранены хоть у кого-то в зонах (см. /watched-stations на лёгком
# сервере и app/scheduler.py::_targeted_cycle). Полный проход по GEOBOXES
# всё равно повторяется, но РЕДКО (CATALOG_SWEEP_INTERVAL_HOURS) — только
# чтобы обновить каталог/поймать новые станции, не для оповещений.
#
# По умолчанию выключено (полный скан каждый цикл, как раньше) — включайте
# явно, когда GEOBOXES стал слишком большим (например, вся Россия) и
# полный скан каждые SCAN_INTERVAL_MINUTES стал нереалистичным.
TARGETED_SCAN_ENABLED = os.environ.get("TARGETED_SCAN_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
CATALOG_SWEEP_INTERVAL_HOURS = int(os.environ.get("CATALOG_SWEEP_INTERVAL_HOURS", "24"))
# Половина стороны мини-бокса вокруг отслеживаемой станции, в градусах.
# 0.01° ~ 1.1 км по широте — с запасом покрывает саму станцию, не
# разворачиваясь в скан всего города.
WATCH_BOX_MARGIN_DEG = float(os.environ.get("WATCH_BOX_MARGIN_DEG", "0.01"))

# --- нужны только на сервере-боте (ROLE=bot или monolith) ---
if ROLE in ("bot", "monolith"):
    BOT_TOKEN = os.environ["BOT_TOKEN"]
else:
    BOT_TOKEN = ""

# На чём слушает HTTP API лёгкого сервера (сюда стучится скрапер).
API_HOST = os.environ.get("API_HOST", "0.0.0.0")
API_PORT = int(os.environ.get("API_PORT", "8080"))

# Путь к локальной базе подписчиков/команд на сервере-боте.
BOT_DB_PATH = os.environ.get("BOT_DB_PATH", "/app/data/bot.db")

# Доступ: пробный период при первом /start, дальше — платная подписка
# за коины. Эквайринг подключается отдельно (см. app/subscription.py) —
# пока коины начисляются вручную через app/grant_coins.py.
#
# PAYWALL_ENABLED=false полностью отключает эту систему на конкретном
# инстансе бота — все функции становятся бесплатными и безлимитными,
# кнопка «💳 Тариф и коины» не показывается (меню возвращается к 2
# кнопкам: «Действия» / «Подписки»). Так можно держать один бот
# бесплатным, а другой (клон, см. docker-compose.bot2.yml) — платным,
# на одном и том же скрапере.
PAYWALL_ENABLED = os.environ.get("PAYWALL_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
TRIAL_HOURS = int(os.environ.get("TRIAL_HOURS", "12"))
SUBSCRIPTION_DAYS = int(os.environ.get("SUBSCRIPTION_DAYS", "14"))
SUBSCRIPTION_COST_COINS = int(os.environ.get("SUBSCRIPTION_COST_COINS", "100"))

# Telegram Mini App (WebApp) — отдельный HTTP API (не тот, что для
# скрапера, у него своя авторизация через Telegram initData, а не
# Bearer-токен). Поднимается только если PAYWALL_ENABLED=true — Mini App
# делаем только для платного бота (см. app/light_bot.py::run_miniapp_server).
# MINIAPP_URL — адрес, где будет отдаваться сам фронтенд (когда появится);
# пока не задан — кнопка запуска приложения в чате не показывается.
MINIAPP_HOST = os.environ.get("MINIAPP_HOST", "0.0.0.0")
MINIAPP_PORT = int(os.environ.get("MINIAPP_PORT", "8090"))
MINIAPP_URL = os.environ.get("MINIAPP_URL", "")

# ---------------------------------------------------------------------
# GEOBOXES: ваши области (актуально только на сервере-скрапере).
# Реальные координаты вынесены в app/geoboxes_local.py — этот файл в
# .gitignore и никогда не попадёт в публичный репозиторий.
# ---------------------------------------------------------------------
try:
    from app.geoboxes_local import GEOBOXES  # noqa: F401
except ImportError:
    GEOBOXES: dict[str, tuple[float, float, float, float]] = {}
