"""
Логика сбора данных с sberazs.ru/api/stations.

Куки от антибот-защиты кэшируются в памяти процесса и обновляются
только когда API реально отвечает 401/403 — не гоняем браузер на
каждый цикл обновления, только когда сессия протухла.
"""

import asyncio
import logging
import time
from typing import Awaitable, Callable, Optional

import requests
from playwright.async_api import async_playwright

from app.config import REQUEST_DELAY_SEC

log = logging.getLogger(__name__)

BASE_URL = "https://sberazs.ru"
API_URL = "https://sberazs.ru/api/stations"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)


async def get_cookies_via_browser() -> dict:
    """Открывает сайт в headless-браузере, дожидается антибот-кук."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=_UA, locale="ru-RU")
        page = await context.new_page()
        await page.goto(BASE_URL, wait_until="networkidle")
        await page.wait_for_timeout(3000)
        cookies = await context.cookies()
        await browser.close()
    return {c["name"]: c["value"] for c in cookies}


def build_grid(bbox: tuple[float, float, float, float], step_lon: float, step_lat: float):
    min_lon, min_lat, max_lon, max_lat = bbox
    cells = []
    lon = min_lon
    while lon < max_lon:
        lat = min_lat
        while lat < max_lat:
            cells.append((lon, lat, min(lon + step_lon, max_lon), min(lat + step_lat, max_lat)))
            lat += step_lat
        lon += step_lon
    return cells


class InvalidResponse(Exception):
    """Ответ пришёл с 2xx, но это не тот JSON, который мы ожидали —
    обычно значит, что куки протухли, а антибот вернул HTML/пустоту."""


def _fetch_stations_sync(session: requests.Session, bbox_cell) -> list:
    min_lon, min_lat, max_lon, max_lat = bbox_cell
    params = {"bbox": f"{min_lon},{min_lat},{max_lon},{max_lat}"}
    headers = {
        "accept": "*/*",
        "accept-language": "ru,en-US;q=0.9,en;q=0.8",
        "referer": BASE_URL + "/",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }
    resp = session.get(API_URL, params=params, headers=headers, timeout=15)
    resp.raise_for_status()
    try:
        return resp.json().get("stations", [])
    except ValueError as e:
        # resp.json() бросает requests.exceptions.JSONDecodeError,
        # это подкласс ValueError — ловим здесь и логируем тело ответа
        # для диагностики, дальше пусть вызывающий код обновит куки.
        log.error(
            "Не удалось распарсить JSON (status=%s), первые 200 символов тела: %r",
            resp.status_code, resp.text[:200],
        )
        raise InvalidResponse(str(e)) from e


def _needs_cookie_refresh(exc: Exception) -> bool:
    if isinstance(exc, InvalidResponse):
        return True
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return exc.response.status_code in (401, 403)
    return False


class StationsClient:
    """Держит сессию с куками, умеет сама обновлять их при 401/403.

    Один клиент параллельно используют планировщик и опрос команд —
    обновление кук идёт под замком, чтобы при протухшей сессии не
    поднималось несколько браузеров разом."""

    def __init__(self):
        self._session = requests.Session()
        self._cookies_ready = False
        # Растёт при каждом обновлении кук — по нему понимаем, что пока
        # мы ждали замок, куки уже обновил кто-то другой.
        self._cookie_generation = 0
        self._cookie_lock = asyncio.Lock()

    async def _ensure_cookies(self):
        if self._cookies_ready:
            return
        async with self._cookie_lock:
            if not self._cookies_ready:
                await self._load_cookies()

    async def _refresh_cookies(self, stale_generation: int):
        async with self._cookie_lock:
            if self._cookie_generation != stale_generation:
                return
            await self._load_cookies()

    async def _load_cookies(self):
        log.info("Получаю антибот-куки через браузер...")
        cookies = await get_cookies_via_browser()
        self._session.cookies.clear()
        self._session.cookies.update(cookies)
        self._cookies_ready = True
        self._cookie_generation += 1

    async def _fetch_cell(self, cell) -> list:
        """Запрос одной ячейки; при протухших куках — обновляет их и
        повторяет один раз. Прочие ошибки пробрасывает."""
        generation = self._cookie_generation
        try:
            return await asyncio.to_thread(_fetch_stations_sync, self._session, cell)
        except (requests.HTTPError, InvalidResponse) as e:
            if not _needs_cookie_refresh(e):
                raise
        log.warning("Куки истекли/невалидны, обновляю сессию...")
        await self._refresh_cookies(generation)
        return await asyncio.to_thread(_fetch_stations_sync, self._session, cell)

    async def collect_area(
        self,
        bbox: tuple[float, float, float, float],
        step_lon: float,
        step_lat: float,
        on_progress: Optional[Callable[[list], Awaitable[None]]] = None,
        progress_every_cells: int = 20,
    ) -> dict:
        """
        Обходит bbox сеткой, возвращает словарь {api_id: station_dict}
        со всеми найденными станциями (без фильтрации по свежести —
        это решает вызывающий код).

        on_progress (опционально) вызывается периодически — каждые
        progress_every_cells ячеек сетки, плюс всегда в самом конце —
        со списком СЫРЫХ станций, найденных с прошлого вызова (не со
        всем накопленным результатом). Нужно для очень больших bbox
        (например, вся Россия одним боксом): вызывающий код может сразу
        отдавать частичные результаты наружу (в БД/боту), не дожидаясь
        конца скана всей области — иначе пользователи видят пустой
        каталог часами, пока идёт первый полный проход.
        """
        await self._ensure_cookies()

        grid = build_grid(bbox, step_lon, step_lat)
        result: dict[str, dict] = {}
        pending_batch: list = []

        for i, cell in enumerate(grid):
            try:
                stations = await self._fetch_cell(cell)
            except (requests.RequestException, InvalidResponse) as e:
                # Включая таймауты/обрывы соединения — одна плохая ячейка
                # не должна обрывать скан всей области.
                log.error("Ошибка на ячейке %s: %s", cell, e)
                await asyncio.sleep(REQUEST_DELAY_SEC)
                continue

            for st in stations:
                api_id = st.get("id")
                if api_id is not None:
                    result[str(api_id)] = st
                    pending_batch.append(st)

            if on_progress and pending_batch and (i + 1) % progress_every_cells == 0:
                batch, pending_batch = pending_batch, []
                try:
                    await on_progress(batch)
                except Exception:
                    log.exception("Ошибка в on_progress при обходе сетки (не прерываю скан)")

            await asyncio.sleep(REQUEST_DELAY_SEC)

        if on_progress and pending_batch:
            try:
                await on_progress(pending_batch)
            except Exception:
                log.exception("Ошибка в on_progress (финальный батч, не прерываю скан)")

        return result
