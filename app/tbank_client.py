"""
Клиент для toplivo.tbank.ru — запрос полностью открытый, антибот не
нужен (в отличие от sberazs.ru). Используется как ВТОРИЧНЫЙ источник:
подтверждает/дополняет данные sberazs, но не заменяет их — sberazs
остаётся главным (именно он триггерит уведомления о возврате топлива).
"""

import logging

import aiohttp

from app.geo import find_nearest

log = logging.getLogger(__name__)

TBANK_API_URL = "https://toplivo.tbank.ru/api/v1/stations"

# Максимальное расстояние (метры), при котором считаем станцию sberazs
# и станцию T-Bank одной и той же заправкой.
MATCH_DISTANCE_M = 80

# Числовые ключи T-Bank -> канонические ключи топлива (как у sberazs).
TBANK_FUEL_TYPE_MAP = {
    "92": "ai92",
    "95": "ai95",
    "98": "ai98",
    "100": "ai100",
    "dt": "diesel",
    "diesel": "diesel",
}


async def fetch_tbank_stations(bbox: tuple[float, float, float, float]) -> list[dict]:
    """bbox: (min_lon, min_lat, max_lon, max_lat) — как у sberazs."""
    min_lon, min_lat, max_lon, max_lat = bbox
    params = {
        "minLat": min_lat, "maxLat": max_lat,
        "minLon": min_lon, "maxLon": max_lon,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                TBANK_API_URL, params=params, timeout=aiohttp.ClientTimeout(total=20)
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
        return data.get("payload", [])
    except Exception:
        log.exception("Не удалось получить данные T-Bank для bbox=%s", bbox)
        return []


def find_tbank_match(sber_lat: float, sber_lon: float, tbank_stations: list[dict]) -> dict | None:
    """Ищет ближайшую станцию T-Bank в пределах MATCH_DISTANCE_M."""
    return find_nearest(sber_lat, sber_lon, tbank_stations, MATCH_DISTANCE_M)
