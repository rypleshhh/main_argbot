"""Общая геометрия для матчинга станций между источниками (T-Bank, gdebenz)."""

import math
from typing import Optional


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def find_nearest(lat: Optional[float], lon: Optional[float], stations: list[dict], max_distance_m: float) -> Optional[dict]:
    """Ближайшая станция из stations (поля lat/lon) в пределах
    max_distance_m, либо None. Полный перебор — станций в одном bbox
    обычно десятки-сотни, индекс не нужен."""
    if lat is None or lon is None:
        return None

    best = None
    best_dist = max_distance_m
    for s in stations:
        s_lat, s_lon = s.get("lat"), s.get("lon")
        if s_lat is None or s_lon is None:
            continue
        dist = haversine_m(lat, lon, s_lat, s_lon)
        if dist < best_dist:
            best = s
            best_dist = dist
    return best
