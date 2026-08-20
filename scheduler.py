import asyncio
import logging
from datetime import datetime, timedelta, timezone

import aiohttp

from app.config import (
    GEOBOXES,
    MAX_PAYMENT_AGE_MINUTES,
    SCAN_INTERVAL_MINUTES,
    STEP_LON,
    STEP_LAT,
    LIGHT_SERVERS,
    TARGETED_SCAN_ENABLED,
    CATALOG_SWEEP_INTERVAL_HOURS,
    WATCH_BOX_MARGIN_DEG,
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
    это локально (station_catalog) — сам он такого списка не видит.
    Полная синхронизация — чистит станции, пропавшие у скрапера.
    Вызывать в КОНЦЕ скана области целиком."""
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


async def _push_station_catalog_partial(area: str, found_batch: dict):
    """Частичный пуш каталога — ТОЛЬКО добавляет/обновляет, ничего не
    удаляет (в отличие от _push_station_catalog). Нужен для
    промежуточных батчей во время долгого скана огромной области: без
    этого пользователи видят пустой каталог часами, пока не закончится
    первый полный проход (например, по всей России одним bbox).

    found_batch: {api_id: raw_station_dict} — сырые данные API, как
    возвращает StationsClient.collect_area; приводим через
    storage.flatten к тем же полям, что и обычный снимок в БД."""
    if not LIGHT_SERVERS or not found_batch:
        return
    payload = {
        "area": area,
        "stations": [
            {
                "station_id": api_id,
                "name": flat["name"],
                "address": flat["address"],
                "lat": flat["lat"],
                "lon": flat["lon"],
            }
            for api_id, st in found_batch.items()
            for flat in [storage.flatten(api_id, area, st)]
        ],
    }
    for server in LIGHT_SERVERS:
        await _post_to_server(server, "/station-catalog/upsert", payload, timeout=30)


async def _full_sweep_cycle(client: StationsClient):
    """Полный проход по ВСЕМ GEOBOXES — наполняет/обновляет каталог
    станций и заодно проверяет топливо везде. В targeted-режиме это
    происходит РЕДКО (CATALOG_SWEEP_INTERVAL_HOURS), в обычном —
    каждый цикл (SCAN_INTERVAL_MINUTES), как было раньше."""
    if not GEOBOXES:
        log.warning("GEOBOXES пуст — планировщику нечего сканировать")
        return

    await _push_areas()

    for area, bbox in GEOBOXES.items():
        log.info("Полный скан области: %s", area)

        async def _on_progress(batch: list, area=area, bbox=bbox):
            # Промежуточный батч по ходу долгого скана (актуально для
            # огромных bbox — например, вся Россия одним боксом): сразу
            # считаем обновления топлива и пушим частичный каталог, не
            # дожидаясь конца скана всей области целиком.
            found_batch = {str(st["id"]): st for st in batch if st.get("id") is not None}
            if not found_batch:
                return
            try:
                tb = await _build_tbank_matches(bbox, found_batch)
            except Exception:
                tb = {}
            try:
                gd = await _build_gdebenz_matches(bbox, found_batch)
            except Exception:
                gd = {}
            batch_updates = storage.upsert_stations_and_get_updates(
                area, found_batch, MAX_PAYMENT_AGE_MINUTES, tb, gd
            )
            await _push_notifications(batch_updates)
            await _push_station_catalog_partial(area, found_batch)

        try:
            found = await client.collect_area(bbox, STEP_LON, STEP_LAT, on_progress=_on_progress)
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

        # Финальный проход по всему накопленному — досчитывает то, что не
        # попало в промежуточные батчи (последний неполный батч), и
        # ПОЛНОСТЬЮ синхронизирует каталог (с удалением пропавших станций,
        # чего частичные пуши намеренно не делают). Повторная обработка
        # уже учтённых в батчах станций безопасна и идемпотентна —
        # detect-логика сравнивает с последней записью в истории, если
        # статус не изменился, повторного уведомления не будет.
        updates = storage.upsert_stations_and_get_updates(
            area, found, MAX_PAYMENT_AGE_MINUTES, tbank_matches, gdebenz_matches
        )
        log.info("Область %s: найдено %d станций, новых обновлений %d", area, len(found), len(updates))

        await _push_notifications(updates)
        await _push_station_catalog(area)


async def _maybe_full_sweep(client: StationsClient):
    """В targeted-режиме — полный проход только раз в
    CATALOG_SWEEP_INTERVAL_HOURS (или если его вообще ещё не было)."""
    last = storage.get_last_full_sweep()
    now = datetime.now(timezone.utc)
    if last is not None and (now - last) < timedelta(hours=CATALOG_SWEEP_INTERVAL_HOURS):
        return
    log.info("Пора обновить каталог — запускаю полный проход по GEOBOXES")
    await _full_sweep_cycle(client)
    storage.set_last_full_sweep_now()


async def _fetch_watched_stations() -> list[dict]:
    """Объединённый (с дедупликацией по station_id) список станций,
    которые реально нужно проверять фоном — те, что сохранены хоть у
    кого-то в зонах на лёгком(их) сервере(ах)."""
    if not LIGHT_SERVERS:
        return []
    seen: dict[str, dict] = {}
    for server in LIGHT_SERVERS:
        try:
            async with aiohttp.ClientSession(headers=_headers_for(server)) as session:
                async with session.get(
                    f"{server['url']}/watched-stations",
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
            for s in data.get("stations", []):
                if s.get("station_id"):
                    seen[s["station_id"]] = s
        except Exception:
            log.exception("Не удалось получить список отслеживаемых станций с %s", server["name"])
    return list(seen.values())


async def _targeted_cycle(client: StationsClient):
    """Фоновый цикл в targeted-режиме: сканируем не область целиком, а
    маленький бокс вокруг КАЖДОЙ отслеживаемой станции — сильно дешевле,
    когда GEOBOXES огромный (например, вся Россия), а реально людям
    нужны уведомления по десяткам конкретных станций в их зонах."""
    watched = await _fetch_watched_stations()
    if not watched:
        log.info("Нет отслеживаемых станций (никто ещё не добавил станции в зоны) — целевой скан пропущен")
        return

    log.info("Целевой скан: %d отслеживаемых станций", len(watched))
    for s in watched:
        area = s.get("area") or "unknown"
        lat, lon = s.get("lat"), s.get("lon")
        if lat is None or lon is None:
            continue

        m = WATCH_BOX_MARGIN_DEG
        bbox = (lon - m, lat - m, lon + m, lat + m)
        try:
            found = await client.collect_area(bbox, STEP_LON, STEP_LAT)
        except Exception:
            log.exception("Ошибка целевого скана станции %s (%s)", s.get("station_id"), area)
            continue
        if not found:
            continue

        try:
            tbank_matches = await _build_tbank_matches(bbox, found)
        except Exception:
            tbank_matches = {}
        try:
            gdebenz_matches = await _build_gdebenz_matches(bbox, found)
        except Exception:
            gdebenz_matches = {}

        updates = storage.upsert_stations_and_get_updates(
            area, found, MAX_PAYMENT_AGE_MINUTES, tbank_matches, gdebenz_matches
        )
        await _push_notifications(updates)
        # Каталог (имя/адрес) целевой скан НЕ трогает — он не меняется от
        # проверки топлива, обновляется только при полном проходе.


async def run_scheduler(client: StationsClient):
    log.info(
        "Планировщик запущен, интервал %d минут (targeted=%s)",
        SCAN_INTERVAL_MINUTES, TARGETED_SCAN_ENABLED,
    )
    while True:
        try:
            if TARGETED_SCAN_ENABLED:
                await _maybe_full_sweep(client)
                await _targeted_cycle(client)
            else:
                await _full_sweep_cycle(client)
        except Exception:
            log.exception("Ошибка в цикле сканирования")
        await asyncio.sleep(SCAN_INTERVAL_MINUTES * 60)
