"""
Mini App backend — REST API для будущего Telegram Mini App фронтенда.

Отдельное HTTP-приложение на своём порту (MINIAPP_PORT) — НЕ то же самое,
что API для скрапера (/commands, /notifications и т.д. в light_bot.py).
Там авторизация Bearer-токеном (скрапер сам его знает), здесь —
проверка Telegram initData (app/webapp_auth.py): любой запрос от
фронтенда обязан принести заголовок X-Telegram-Init-Data с initData
текущей WebApp-сессии, иначе 401.

Поднимается только если PAYWALL_ENABLED=true (см. app/light_bot.py::main)
— Mini App есть только у платного бота.

Экраны фронтенда и соответствующие эндпоинты:
  1. «Сканировать области» -> GET /scan-targets, POST /scan,
                               GET /scan-sessions/{id}
  2. «Добавить зону»        -> GET /catalog/areas, GET /catalog?area=..,
                               GET /zones, POST /zones, DELETE /zones/{id},
                               GET /zones/{id}/stations,
                               POST /zones/{id}/stations,
                               DELETE /zones/{id}/stations/{station_id}
  3. «Мои оповещения»       -> GET /subscriptions, POST /subscriptions,
                               DELETE /subscriptions
  4. «Тариф и коины»        -> GET /tariff, POST /tariff/purchase,
                               POST /tariff/topup

Здесь НЕТ доступа к объекту aiogram Bot (чтобы не тянуть циклический
импорт с light_bot.py) — таймаут-предупреждения о зависшем скане (как в
чате через /scan_city) тут не шлём, фронтенд сам решает, как показывать
затянувшийся опрос GET /scan-sessions/{id}.
"""

import asyncio
import json
import logging
from pathlib import Path

from aiohttp import web

from app.config import BOT_TOKEN, MINIAPP_HOST, MINIAPP_PORT, SUBSCRIPTION_COST_COINS, SUBSCRIPTION_DAYS
from app.storage import CATEGORY_LABELS
from app import bot_storage as storage
from app import subscription
from app import webapp_auth

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "miniapp_static"


# ---------------------------------------------------------------------
# Middlewares: CORS (на случай, если фронтенд когда-нибудь будет не на
# том же домене) и авторизация по Telegram initData.
#
# Авторизация нужна ТОЛЬКО эндпоинтам API (/api/...). Статические файлы
# фронтенда (/, /app.js, /style.css) отдаются без проверки — initData
# физически недоступен странице, пока она не загрузилась и Telegram JS
# SDK не отработал; сам API уже защищён отдельно.
# ---------------------------------------------------------------------

@web.middleware
async def cors_middleware(request: web.Request, handler):
    if request.method == "OPTIONS":
        resp = web.Response()
    else:
        try:
            resp = await handler(request)
        except web.HTTPException as exc:
            resp = exc
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Telegram-Init-Data"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    return resp


@web.middleware
async def auth_middleware(request: web.Request, handler):
    if request.method == "OPTIONS" or not request.path.startswith("/api/"):
        return await handler(request)

    init_data = request.headers.get("X-Telegram-Init-Data", "")
    try:
        parsed = webapp_auth.validate_init_data(init_data, BOT_TOKEN)
    except webapp_auth.InitDataError as e:
        return web.json_response({"error": f"unauthorized: {e}"}, status=401)

    chat_id = webapp_auth.extract_chat_id(parsed)
    if chat_id is None:
        return web.json_response({"error": "unauthorized: no user in init_data"}, status=401)

    request["miniapp_chat_id"] = chat_id
    request["miniapp_user"] = parsed.get("user")
    return await handler(request)


async def handle_options(request: web.Request):
    return web.Response()


async def handle_health(request: web.Request):
    return web.json_response({"status": "ok"})


async def handle_index(request: web.Request):
    return web.FileResponse(STATIC_DIR / "index.html")


async def handle_static_file(request: web.Request):
    """Явная раздача трёх файлов фронтенда — без web.static на весь
    каталог, чтобы не листить директорию и не тащить лишнее в образ."""
    filename = request.match_info["filename"]
    allowed = {"app.js", "style.css"}
    if filename not in allowed:
        raise web.HTTPNotFound()
    return web.FileResponse(STATIC_DIR / filename)


async def handle_me(request: web.Request):
    chat_id = request["miniapp_chat_id"]
    return web.json_response({
        "chat_id": chat_id,
        "has_access": subscription.has_access(chat_id),
        "has_paid_access": subscription.has_paid_access(chat_id),
    })


# ---------------------------------------------------------------------
# 1. «Сканировать области»
# ---------------------------------------------------------------------

async def handle_scan_targets(request: web.Request):
    chat_id = request["miniapp_chat_id"]
    return web.json_response({
        "areas": storage.get_known_areas(),    # [{'id', 'area'}]
        "zones": storage.list_zones(chat_id),  # [{'id','name','created_at','station_count'}]
    })


async def _read_json(request: web.Request) -> dict:
    try:
        return await request.json()
    except (json.JSONDecodeError, ValueError):
        return {}


async def handle_scan(request: web.Request):
    chat_id = request["miniapp_chat_id"]
    if not subscription.has_access(chat_id):
        return web.json_response({"error": "no_access"}, status=402)

    body = await _read_json(request)
    area = body.get("area")
    zone_id = body.get("zone_id")

    if area:
        areas = [area]
        zid = None
    elif zone_id is not None:
        try:
            zid = int(zone_id)
        except (TypeError, ValueError):
            return web.json_response({"error": "bad zone_id"}, status=400)
        if storage.get_zone(chat_id, zid) is None:
            return web.json_response({"error": "zone_not_found"}, status=404)
        areas = storage.get_zone_areas(chat_id, zid)
        if not areas:
            return web.json_response({"error": "zone_empty"}, status=400)
    else:
        return web.json_response({"error": "provide area or zone_id"}, status=400)

    session_id = storage.create_scan_session(chat_id, zid)
    for a in areas:
        storage.add_command(chat_id, a, session_id=session_id)
    log.info("Mini App: сессия скана id=%s chat_id=%s areas=%s", session_id, chat_id, areas)

    return web.json_response({"session_id": session_id, "areas": areas}, status=201)


async def handle_scan_session(request: web.Request):
    chat_id = request["miniapp_chat_id"]
    try:
        session_id = int(request.match_info["session_id"])
    except ValueError:
        return web.json_response({"error": "bad session_id"}, status=400)

    session = storage.get_scan_session(session_id)
    if not session or session["chat_id"] != chat_id:
        return web.json_response({"error": "not_found"}, status=404)

    zone_station_ids = None
    if session["zone_id"]:
        zone_station_ids = set(storage.get_zone_station_ids(session["zone_id"]))

    finished = True
    areas_out = []
    for cmd in storage.get_session_commands(session_id):
        if cmd["status"] != "done":
            finished = False
            areas_out.append({"area": cmd["area"], "status": cmd["status"], "stations": []})
            continue

        result = storage.get_command_result(cmd["id"])
        stations = json.loads(result["stations_json"]) if result else []
        if zone_station_ids is not None:
            stations = [s for s in stations if s.get("api_id") in zone_station_ids]
        areas_out.append({"area": cmd["area"], "status": "done", "stations": stations})

    return web.json_response({"session_id": session_id, "finished": finished, "areas": areas_out})


# ---------------------------------------------------------------------
# 2. «Добавить зону»
# ---------------------------------------------------------------------

async def handle_catalog_areas(request: web.Request):
    return web.json_response({"areas": storage.get_catalog_areas()})


async def handle_catalog_stations(request: web.Request):
    """Оставлено для обратной совместимости — станции одной конкретной
    области. Основной сценарий фронтенда теперь handle_catalog_search
    (по всему каталогу сразу, без привязки к области)."""
    area = request.query.get("area")
    if not area:
        return web.json_response({"error": "area query param required"}, status=400)
    return web.json_response({"stations": storage.get_catalog_stations(area)})


async def handle_catalog_brands(request: web.Request):
    """Бренды по всему каталогу, отсортированные по популярности — для
    панели фильтра «Фирма»."""
    return web.json_response({"brands": storage.get_catalog_brands()})


async def handle_catalog_search(request: web.Request):
    """Поиск станций ПО ВСЕЙ базе (не привязан к одной области) — текст
    ищется в адресе/названии, plus необязательный фильтр по брендам
    (?brand=Роснефть&brand=Лукойл, можно несколько раз). Список станций
    ограничен разумным лимитом — если ищут совсем без q и без брендов,
    просим уточнить, чтобы не гонять на телефон весь каталог разом."""
    query = request.query.get("q", "").strip()
    brands = request.query.getall("brand", [])
    if not query and not brands:
        return web.json_response({"stations": [], "hint": "уточните поиск или выберите фирму"})
    stations = storage.search_catalog_stations(query=query, brands=brands or None, limit=100)
    return web.json_response({"stations": stations})


async def handle_list_zones(request: web.Request):
    chat_id = request["miniapp_chat_id"]
    return web.json_response({"zones": storage.list_zones(chat_id)})



async def handle_create_zone(request: web.Request):
    chat_id = request["miniapp_chat_id"]
    body = await _read_json(request)
    name = (body.get("name") or "").strip()
    if not name:
        return web.json_response({"error": "name is required"}, status=400)
    if len(name) > 100:
        return web.json_response({"error": "name too long (max 100)"}, status=400)
    zone_id = storage.create_zone(chat_id, name)
    return web.json_response({"id": zone_id, "name": name}, status=201)


def _parse_zone_id(request: web.Request):
    try:
        return int(request.match_info["zone_id"]), None
    except ValueError:
        return 0, web.json_response({"error": "bad zone_id"}, status=400)


async def handle_delete_zone(request: web.Request):
    chat_id = request["miniapp_chat_id"]
    zone_id, err = _parse_zone_id(request)
    if err:
        return err
    if not storage.delete_zone(chat_id, zone_id):
        return web.json_response({"error": "not_found"}, status=404)
    return web.json_response({"status": "ok"})


async def handle_zone_stations(request: web.Request):
    chat_id = request["miniapp_chat_id"]
    zone_id, err = _parse_zone_id(request)
    if err:
        return err
    if storage.get_zone(chat_id, zone_id) is None:
        return web.json_response({"error": "not_found"}, status=404)
    return web.json_response({"stations": storage.get_zone_stations(chat_id, zone_id)})


async def handle_zone_add_station(request: web.Request):
    chat_id = request["miniapp_chat_id"]
    zone_id, err = _parse_zone_id(request)
    if err:
        return err
    body = await _read_json(request)
    station_id = body.get("station_id")
    if not station_id:
        return web.json_response({"error": "station_id is required"}, status=400)
    if not storage.add_station_to_zone(chat_id, zone_id, station_id):
        return web.json_response({"error": "zone not found"}, status=404)
    return web.json_response({"status": "ok"}, status=201)


async def handle_zone_add_stations_bulk(request: web.Request):
    """Массовое добавление — для кнопок «Выбрать всё»/«Добавить
    выбранные» в редакторе зоны."""
    chat_id = request["miniapp_chat_id"]
    zone_id, err = _parse_zone_id(request)
    if err:
        return err
    body = await _read_json(request)
    station_ids = body.get("station_ids")
    if not isinstance(station_ids, list) or not station_ids:
        return web.json_response({"error": "station_ids (non-empty list) is required"}, status=400)
    added = storage.add_stations_to_zone_bulk(chat_id, zone_id, station_ids)
    if not added:
        return web.json_response({"error": "zone not found"}, status=404)
    return web.json_response({"status": "ok", "added": added}, status=201)


async def handle_zone_remove_station(request: web.Request):
    chat_id = request["miniapp_chat_id"]
    zone_id, err = _parse_zone_id(request)
    if err:
        return err
    station_id = request.match_info["station_id"]
    if not storage.remove_station_from_zone(chat_id, zone_id, station_id):
        return web.json_response({"error": "not_found"}, status=404)
    return web.json_response({"status": "ok"})


# ---------------------------------------------------------------------
# 3. «Мои оповещения» (тот же механизм хранения, что и в чате)
# ---------------------------------------------------------------------

async def handle_get_subscriptions(request: web.Request):
    chat_id = request["miniapp_chat_id"]
    return web.json_response({"subscriptions": storage.get_subscriptions(chat_id)})


async def handle_add_subscription(request: web.Request):
    chat_id = request["miniapp_chat_id"]
    if not subscription.has_access(chat_id):
        return web.json_response({"error": "no_access"}, status=402)
    body = await _read_json(request)
    area = body.get("area") or "all"
    category = body.get("category") or "all"
    if category not in CATEGORY_LABELS:
        return web.json_response({"error": "bad category", "allowed": list(CATEGORY_LABELS)}, status=400)
    storage.add_subscriber(chat_id, area, category)
    return web.json_response({"status": "ok"}, status=201)


async def handle_remove_all_subscriptions(request: web.Request):
    chat_id = request["miniapp_chat_id"]
    storage.remove_subscriber(chat_id)
    return web.json_response({"status": "ok"})


# ---------------------------------------------------------------------
# 4. «Тариф и коины»
# ---------------------------------------------------------------------

async def handle_tariff(request: web.Request):
    chat_id = request["miniapp_chat_id"]
    status = subscription.get_status(chat_id)
    return web.json_response({
        "has_access": status.has_access,
        "on_trial": status.on_trial,
        "paid_active": status.paid_active,
        "trial_ends_at": status.trial_ends_at.isoformat() if status.trial_ends_at else None,
        "paid_until": status.paid_until.isoformat() if status.paid_until else None,
        "coins": status.coins,
        "subscription_days": SUBSCRIPTION_DAYS,
        "subscription_cost_coins": SUBSCRIPTION_COST_COINS,
        "coin_packages": subscription.COIN_PACKAGES,
    })


async def handle_tariff_purchase(request: web.Request):
    chat_id = request["miniapp_chat_id"]
    ok, status = subscription.purchase_subscription(chat_id)
    if not ok:
        return web.json_response(
            {"error": "not_enough_coins", "coins": status.coins, "cost": SUBSCRIPTION_COST_COINS},
            status=402,
        )
    return web.json_response({
        "status": "ok",
        "paid_until": status.paid_until.isoformat() if status.paid_until else None,
    })


async def handle_tariff_topup(request: web.Request):
    """Заглушка — как и в чат-боте (light_bot.py::cb_topup_pkg). Здесь
    нужно будет создать счёт у эквайринга и вернуть фронтенду ссылку на
    оплату; коины зачислять в вебхуке провайдера, не в этом хэндлере."""
    body = await _read_json(request)
    pkg = next((p for p in subscription.COIN_PACKAGES if p["id"] == body.get("package_id")), None)
    if pkg is None:
        return web.json_response({"error": "unknown package"}, status=404)
    return web.json_response({
        "status": "not_implemented",
        "message": "Оплата пока не подключена — коины начисляет поддержка вручную.",
    }, status=501)


def build_miniapp_app() -> web.Application:
    app = web.Application(middlewares=[cors_middleware, auth_middleware])
    app.router.add_route("OPTIONS", "/{tail:.*}", handle_options)
    app.router.add_get("/health", handle_health)

    # Статика фронтенда — без авторизации (см. auth_middleware).
    app.router.add_get("/", handle_index)
    app.router.add_get("/{filename}", handle_static_file)

    app.router.add_get("/api/me", handle_me)

    app.router.add_get("/api/scan-targets", handle_scan_targets)
    app.router.add_post("/api/scan", handle_scan)
    app.router.add_get("/api/scan-sessions/{session_id}", handle_scan_session)

    app.router.add_get("/api/catalog/areas", handle_catalog_areas)
    app.router.add_get("/api/catalog/brands", handle_catalog_brands)
    app.router.add_get("/api/catalog/search", handle_catalog_search)
    app.router.add_get("/api/catalog", handle_catalog_stations)
    app.router.add_get("/api/zones", handle_list_zones)
    app.router.add_post("/api/zones", handle_create_zone)
    app.router.add_delete("/api/zones/{zone_id}", handle_delete_zone)
    app.router.add_get("/api/zones/{zone_id}/stations", handle_zone_stations)
    app.router.add_post("/api/zones/{zone_id}/stations", handle_zone_add_station)
    app.router.add_post("/api/zones/{zone_id}/stations/bulk", handle_zone_add_stations_bulk)
    app.router.add_delete("/api/zones/{zone_id}/stations/{station_id}", handle_zone_remove_station)

    app.router.add_get("/api/subscriptions", handle_get_subscriptions)
    app.router.add_post("/api/subscriptions", handle_add_subscription)
    app.router.add_delete("/api/subscriptions", handle_remove_all_subscriptions)

    app.router.add_get("/api/tariff", handle_tariff)
    app.router.add_post("/api/tariff/purchase", handle_tariff_purchase)
    app.router.add_post("/api/tariff/topup", handle_tariff_topup)
    return app


async def run_miniapp_server():
    app = build_miniapp_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, MINIAPP_HOST, MINIAPP_PORT)
    await site.start()
    log.info("Mini App API запущено на %s:%s", MINIAPP_HOST, MINIAPP_PORT)
    while True:
        await asyncio.sleep(3600)
