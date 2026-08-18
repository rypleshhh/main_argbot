import asyncio
import logging

import aiohttp

from app.config import (
    GEOBOXES,
    MAX_PAYMENT_AGE_MINUTES,
    SCAN_INTERVAL_MINUTES,
    STEP_LON,
    STEP_LAT,
    LIGHT_SERVERS,
)
from app import storage
from app.scraper import StationsClient
from app.tbank_client import fetch_tbank_stations, find_tbank_match
from app.gdebenz_client import fetch_gdebenz_stations, find_gdebenz_match

log = logging.getLogger(__name__)


def _headers_for(server: dict) -> dict:
    token = server.get("token")
    return {"Authorization": f"Bearer {token}"} if token else {}


async def _post_to_server(server: dict, path: str, payload: dict, timeout: int = 30):
    try:
        async with aiohttp.ClientSession(headers=_headers_for(server)) as session:
            async with session.post(
                f"{server['url']}{path}",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                resp.raise_for_status()
    except Exception:
        log.exception("Не удалось отправить %s на %s (%s)", path, server["name"], server["url"])


async def _build_tbank_matches(bbox: tuple, sber_stations: dict) -> dict:
    """Тянет T-Bank по тому же bbox и матчит с найденными станциями
    sberazs по географической близости. sberazs остаётся главным
    источником — это только вторичное подтверждение для карточки."""
    tbank_stations = await fetch_tbank_stations(bbox)
    if not tbank_stations:
        return {}

    matches = {}
    for api_id, st in sber_stations.items():
        location = st.get("location") or {}
        match = find_tbank_match(location.get("lat"), location.get("lon"), tbank_stations)
        if match:
            matches[api_id] = match
    return matches


async def _build_gdebenz_matches(bbox: tuple, sber_stations: dict) -> dict:
    """Тянет gdebenz (краудсорсинг) по тому же bbox и матчит так же, как
    T-Bank — третье, самое слабое по надёжности мнение рядом с sberazs."""
    gdebenz_stations = await fetch_gdebenz_stations(bbox)
    if not gdebenz_stations:
        return {}

    matches = {}
    for api_id, st in sber_stations.items():
        location = st.get("location") or {}
        match = find_gdebenz_match(location.get("lat"), location.get("lon"), gdebenz_stations)
        if match:
            matches[api_id] = match
    return matches


async def _push_notifications(updates: list[dict]):
    """Скрапер сам стучится на лёгкие сервера и отдаёт свежие уведомления —
    он же за NAT, принимать входящие соединения не может. Одни и те же
    обновления уходят КАЖДОМУ настроенному боту (LIGHT_SERVERS) — данные
    о станциях общие, просто у каждого бота свои подписчики."""
    if not updates or not LIGHT_SERVERS:
        return
    for server in LIGHT_SERVERS:
        await _post_to_server(server, "/notifications", {"updates": updates})


async def _push_areas():
    """Раз в цикл сообщаем каждому лёгкому серверу, какие города вообще
    настроены — он не видит GEOBOXES напрямую, только через этот пуш."""
    if not LIGHT_SERVERS:
        return
    for server in LIGHT_SERVERS:
        await _post_to_server(server, "/areas", {"areas": list(GEOBOXES.keys())}, timeout=15)


async def _push_station_catalog(area: str):
    """Пушит ПОЛНЫЙ список известных станций области (не только те, где
    топливо есть прямо сейчас) — нужен Mini App для экрана «Добавить
    зону», где пользователь выбирает конкретные заправки в свой личный
    список независимо от текущего наличия топлива. Лёгкий сервер хранит
    это локально (station_catalog) — сам он такого списка не видит."""
    if not LIGHT_SERVERS:
        return
    stations = storage.get_all_stations_for_area(area)
    if not stations:
        return
    payload = {
        "area": area,
        "stations": [
            {
                "station_id": s["api_id"],
                "name": s["name"],
                "address": s["address"],
                "lat": s["lat"],
                "lon": s["lon"],
            }
            for s in stations
        ],
    }
    for server in LIGHT_SERVERS:
        await _post_to_server(server, "/station-catalog", payload, timeout=30)


async def _scan_cycle(client: StationsClient):
    if not GEOBOXES:
        log.warning("GEOBOXES пуст — планировщику нечего сканировать")
        return

    await _push_areas()

    for area, bbox in GEOBOXES.items():
        log.info("Сканирую область: %s", area)
        try:
            found = await client.collect_area(bbox, STEP_LON, STEP_LAT)
        except Exception:
            log.exception("Ошибка сканирования области %s", area)
            continue

        try:
            tbank_matches = await _build_tbank_matches(bbox, found)
            if tbank_matches:
                log.info("Область %s: сматчено %d станций с T-Bank", area, len(tbank_matches))
        except Exception:
            log.exception("Ошибка матчинга T-Bank для области %s", area)
            tbank_matches = {}

        try:
            gdebenz_matches = await _build_gdebenz_matches(bbox, found)
            if gdebenz_matches:
                log.info("Область %s: сматчено %d станций с gdebenz", area, len(gdebenz_matches))
        except Exception:
            log.exception("Ошибка матчинга gdebenz для области %s", area)
            gdebenz_matches = {}

        updates = storage.upsert_stations_and_get_updates(
            area, found, MAX_PAYMENT_AGE_MINUTES, tbank_matches, gdebenz_matches
        )
        log.info("Область %s: найдено %d станций, новых обновлений %d", area, len(found), len(updates))

        await _push_notifications(updates)
        await _push_station_catalog(area)


async def run_scheduler(client: StationsClient):
    log.info("Планировщик запущен, интервал %d минут", SCAN_INTERVAL_MINUTES)
    while True:
        try:
            await _scan_cycle(client)
        except Exception:
            log.exception("Ошибка в цикле сканирования")
        await asyncio.sleep(SCAN_INTERVAL_MINUTES * 60)
