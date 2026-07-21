import asyncio
import logging

import aiohttp

from app.config import (
    GEOBOXES,
    MAX_PAYMENT_AGE_MINUTES,
    STEP_LON,
    STEP_LAT,
    LIGHT_SERVERS,
    COMMAND_POLL_INTERVAL_SEC,
)
from app import storage
from app.scraper import StationsClient
from app.scheduler import run_scheduler, _build_tbank_matches, _build_gdebenz_matches, _push_notifications, _headers_for

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)


async def _fulfil_command(client: StationsClient, session: aiohttp.ClientSession, server: dict, cmd: dict):
    area = cmd["area"]
    bbox = GEOBOXES.get(area)
    log.info("[%s] Обрабатываю команду id=%s area=%s", server["name"], cmd["id"], area)

    if bbox is None:
        log.warning(
            "Область «%s» не найдена в GEOBOXES (доступные: %s) — отвечаю пустым списком",
            area, list(GEOBOXES.keys()),
        )
        stations = []
    else:
        try:
            found = await client.collect_area(bbox, STEP_LON, STEP_LAT)
            try:
                tbank_matches = await _build_tbank_matches(bbox, found)
            except Exception:
                log.exception("Ошибка матчинга T-Bank для команды %s", cmd["id"])
                tbank_matches = {}
            try:
                gdebenz_matches = await _build_gdebenz_matches(bbox, found)
            except Exception:
                log.exception("Ошибка матчинга gdebenz для команды %s", cmd["id"])
                gdebenz_matches = {}
            updates = storage.upsert_stations_and_get_updates(
                area, found, MAX_PAYMENT_AGE_MINUTES, tbank_matches, gdebenz_matches
            )
            if updates:
                # Найденные обновления реальны для всех — рассылаем во ВСЕ
                # боты (LIGHT_SERVERS), а не только тому, кто прислал команду.
                log.info("Команда id=%s: обнаружено %d возврата топлива, рассылаю подписчикам", cmd["id"], len(updates))
                await _push_notifications(updates)
            stations = storage.get_fresh_stations(area, MAX_PAYMENT_AGE_MINUTES)
            log.info("Команда id=%s: найдено %d станций, свежих %d", cmd["id"], len(found), len(stations))
        except Exception:
            log.exception("Ошибка выполнения команды для области %s", area)
            stations = []

    try:
        async with session.post(
            f"{server['url']}/command-result",
            json={"command_id": cmd["id"], "area": area, "stations": stations},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            resp.raise_for_status()
    except Exception:
        log.exception("[%s] Не удалось отправить результат команды %s", server["name"], cmd["id"])


async def poll_commands_loop(client: StationsClient, server: dict):
    """Скрапер сам спрашивает лёгкий сервер: 'есть что сделать?' — он за
    NAT и не может принимать входящие соединения, поэтому опрашивает сам.
    Отдельный цикл на каждый настроенный лёгкий сервер (LIGHT_SERVERS),
    т.к. у каждого бота свои пользователи и свои команды /scan_city."""
    log.info("[%s] Опрос команд запущен, интервал %d сек (%s)", server["name"], COMMAND_POLL_INTERVAL_SEC, server["url"])
    async with aiohttp.ClientSession(headers=_headers_for(server)) as session:
        while True:
            try:
                async with session.get(
                    f"{server['url']}/commands", timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()

                for cmd in data.get("commands", []):
                    await _fulfil_command(client, session, server, cmd)

            except Exception:
                log.exception("[%s] Ошибка опроса команд", server["name"])

            await asyncio.sleep(COMMAND_POLL_INTERVAL_SEC)


async def main():
    if not LIGHT_SERVERS:
        log.error(
            "Не задан ни один лёгкий сервер (LIGHT_SERVER_URL / LIGHT_SERVER_URL_2 / ...) — "
            "скрапер не сможет достучаться ни до одного бота"
        )
    else:
        log.info("Настроено ботов: %d (%s)", len(LIGHT_SERVERS), ", ".join(s["name"] for s in LIGHT_SERVERS))

    storage.init_db()
    client = StationsClient()

    tasks = [run_scheduler(client)]
    tasks += [poll_commands_loop(client, server) for server in LIGHT_SERVERS]

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
