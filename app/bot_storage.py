import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Optional

from app.config import BOT_DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS subscribers (
    chat_id INTEGER NOT NULL,
    area TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'all',
    PRIMARY KEY (chat_id, area, category)
);

CREATE TABLE IF NOT EXISTS poll_state (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    area TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending -> sent -> done
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS known_areas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    area TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    chat_id INTEGER PRIMARY KEY,
    username TEXT,
    first_seen TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS access (
    chat_id INTEGER PRIMARY KEY,
    trial_started_at TEXT,   -- когда начался пробный период (12ч), один раз на chat_id
    paid_until TEXT,         -- до какого момента (UTC) действует платная подписка
    coins INTEGER NOT NULL DEFAULT 0
);

-- Всё ниже — под Mini App (только для бота с PAYWALL_ENABLED=true).

CREATE TABLE IF NOT EXISTS station_catalog (
    station_id TEXT PRIMARY KEY,   -- это api_id станции у скрапера
    area TEXT NOT NULL,
    brand TEXT NOT NULL DEFAULT 'Другое',  -- угадывается из названия, см. _guess_brand()
    name TEXT,
    address TEXT,
    lat REAL,
    lon REAL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS zones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS zone_stations (
    zone_id INTEGER NOT NULL,
    station_id TEXT NOT NULL,
    added_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (zone_id, station_id)
);

CREATE TABLE IF NOT EXISTS scan_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    zone_id INTEGER,   -- NULL, если сканировали просто область, а не зону
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS command_results (
    command_id INTEGER PRIMARY KEY,
    area TEXT NOT NULL,
    stations_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# Индексы — ОТДЕЛЬНО от SCHEMA и выполняются ПОСЛЕ миграций (_ensure_column
# в init_db). Причина: CREATE TABLE IF NOT EXISTS не трогает уже существующую
# старую таблицу, и если индекс из SCHEMA ссылается на столбец, которого в
# ней ещё нет (например, brand — добавлен позже commands.session_id), это
# падает с "no such column" на любой БД старее этого поля.
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_station_catalog_area ON station_catalog(area);
CREATE INDEX IF NOT EXISTS idx_station_catalog_brand ON station_catalog(brand);
CREATE INDEX IF NOT EXISTS idx_station_catalog_address ON station_catalog(address);
CREATE INDEX IF NOT EXISTS idx_zones_chat ON zones(chat_id);
"""

# Формат хранения дат в таблице access — фиксированная ширина, чтобы
# строки сравнивались лексикографически так же, как хронологически.
DT_FORMAT = "%Y-%m-%dT%H:%M:%S"


def _connect():
    Path(BOT_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(BOT_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _column_type_sql(row) -> str:
    """Строит SQL-фрагмент столбца по строке PRAGMA table_info — нужно
    для пересборки таблицы в обходе бага ниже."""
    parts = [row["name"], row["type"] or "TEXT"]
    if row["pk"]:
        parts.append("PRIMARY KEY")
    elif row["notnull"]:
        parts.append("NOT NULL")
    if row["dflt_value"] is not None:
        parts.append(f"DEFAULT {row['dflt_value']}")
    return " ".join(parts)


def _ensure_column(conn, table: str, column: str, coltype: str):
    """Добавляет столбец, если его ещё нет. Идемпотентно.

    ВАЖНО: SQLite отказывается выполнять ALTER TABLE ADD COLUMN, если у
    таблицы уже есть ДРУГОЙ столбец с не-константным DEFAULT (у нас это
    почти везде created_at DEFAULT (datetime('now'))) — падает с
    'default value of column [...] is not constant', хотя мы этот
    столбец не трогаем. Известное ограничение движка. Ловим и в этом
    случае пересобираем таблицу по стандартному рецепту SQLite:
    https://sqlite.org/lang_altertable.html
    """
    existing = conn.execute(f"PRAGMA table_info({table})").fetchall()
    cols = [r["name"] for r in existing]
    if column in cols:
        return

    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
        return
    except sqlite3.OperationalError as e:
        if "not constant" not in str(e):
            raise

    tmp = f"{table}__migrate_tmp"
    conn.execute(f"DROP TABLE IF EXISTS {tmp}")
    old_cols_sql = ", ".join(_column_type_sql(r) for r in existing)
    conn.execute(f"CREATE TABLE {tmp} ({old_cols_sql}, {column} {coltype})")
    old_col_names = ", ".join(r["name"] for r in existing)
    conn.execute(f"INSERT INTO {tmp} ({old_col_names}) SELECT {old_col_names} FROM {table}")
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {tmp} RENAME TO {table}")


def init_db():
    with closing(_connect()) as conn:
        conn.executescript(SCHEMA)
        # commands появилась раньше scan_sessions — привязка к сессии
        # мультиобластного скана зоны добавляется миграцией, не в SCHEMA
        # (там уже была бы ошибка "duplicate column" при повторных запусках).
        _ensure_column(conn, "commands", "session_id", "INTEGER")
        # station_catalog появился раньше поля brand (фильтр по фирме в
        # Mini App) — у старых строк будет NULL до следующего пуша от
        # скрапера (sync_station_catalog полностью перезаписывает область).
        _ensure_column(conn, "station_catalog", "brand", "TEXT")
        # Индексы — после миграций, см. комментарий у INDEXES выше.
        conn.executescript(INDEXES)
        conn.commit()


def get_last_notification_id() -> int:
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT value FROM poll_state WHERE key = 'last_notification_id'"
        ).fetchone()
    return int(row["value"]) if row else 0


def set_last_notification_id(value: int):
    with closing(_connect()) as conn:
        conn.execute(
            "INSERT INTO poll_state (key, value) VALUES ('last_notification_id', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = ?",
            (str(value), str(value)),
        )
        conn.commit()


def sync_known_areas(areas: list[str]):
    """Обновляет список городов, которые прислал скрапер (INSERT OR IGNORE,
    чтобы id городов не менялись между синхронизациями)."""
    with closing(_connect()) as conn:
        for area in areas:
            conn.execute("INSERT OR IGNORE INTO known_areas (area) VALUES (?)", (area,))
        conn.commit()


def get_known_areas() -> list[dict]:
    with closing(_connect()) as conn:
        rows = conn.execute("SELECT id, area FROM known_areas ORDER BY area").fetchall()
    return [dict(r) for r in rows]


def get_area_by_id(area_id: int) -> Optional[str]:
    with closing(_connect()) as conn:
        row = conn.execute("SELECT area FROM known_areas WHERE id = ?", (area_id,)).fetchone()
    return row["area"] if row else None


def add_command(chat_id: int, area: str, session_id: Optional[int] = None) -> int:
    with closing(_connect()) as conn:
        cur = conn.execute(
            "INSERT INTO commands (chat_id, area, session_id) VALUES (?, ?, ?)",
            (chat_id, area, session_id),
        )
        conn.commit()
        return cur.lastrowid


def pop_pending_commands() -> list[dict]:
    """Отдаёт все команды со статусом pending и сразу помечает их sent."""
    with closing(_connect()) as conn:
        rows = conn.execute("SELECT * FROM commands WHERE status = 'pending'").fetchall()
        conn.execute("UPDATE commands SET status = 'sent' WHERE status = 'pending'")
        conn.commit()
    return [dict(r) for r in rows]


def get_command(command_id: int) -> Optional[dict]:
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM commands WHERE id = ?", (command_id,)).fetchone()
    return dict(row) if row else None


def mark_command_done(command_id: int):
    with closing(_connect()) as conn:
        conn.execute("UPDATE commands SET status = 'done' WHERE id = ?", (command_id,))
        conn.commit()


def add_subscriber(chat_id: int, area: str, category: str = "all"):
    with closing(_connect()) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO subscribers (chat_id, area, category) VALUES (?, ?, ?)",
            (chat_id, area, category),
        )
        conn.commit()


def remove_subscriber(chat_id: int, area: Optional[str] = None):
    with closing(_connect()) as conn:
        if area is None:
            conn.execute("DELETE FROM subscribers WHERE chat_id = ?", (chat_id,))
        else:
            conn.execute("DELETE FROM subscribers WHERE chat_id = ? AND area = ?", (chat_id, area))
        conn.commit()


def get_subscribers_for_area(area: str, category: str = "all") -> list[int]:
    """category='all' у подписчика значит 'интересны все категории'; тот
    же смысл 'all' у переданного category здесь значит 'подходит любая
    подписка' (используется когда уведомление без привязки к категории)."""
    with closing(_connect()) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT chat_id FROM subscribers
            WHERE (area = ? OR area = 'all')
              AND (category = 'all' OR category = ?)
            """,
            (area, category),
        ).fetchall()
    return [r["chat_id"] for r in rows]


def get_subscriptions(chat_id: int) -> list[dict]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT area, category FROM subscribers WHERE chat_id = ?", (chat_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def register_user(chat_id: int, username: Optional[str] = None):
    """Фиксирует, что chat_id хоть раз писал боту — только для рассылки
    через консоль (app/broadcast.py). Не связано с подписками."""
    with closing(_connect()) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (chat_id, username) VALUES (?, ?)",
            (chat_id, username),
        )
        if username:
            conn.execute("UPDATE users SET username = ? WHERE chat_id = ?", (username, chat_id))
        conn.commit()


def get_all_user_ids() -> list[int]:
    with closing(_connect()) as conn:
        rows = conn.execute("SELECT chat_id FROM users ORDER BY chat_id").fetchall()
    return [r["chat_id"] for r in rows]


# ---------------------------------------------------------------------
# Доступ: пробный период / платная подписка / баланс коинов.
# Бизнес-логика (расчёт статуса, стоимость, длительность) — в
# app/subscription.py, здесь только хранение.
# ---------------------------------------------------------------------

def get_access(chat_id: int) -> Optional[dict]:
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM access WHERE chat_id = ?", (chat_id,)).fetchone()
    return dict(row) if row else None


def start_trial(chat_id: int, trial_started_at: str) -> dict:
    """Создаёт запись доступа с пробным периодом. INSERT OR IGNORE — если
    запись уже есть (повторный вызов, например из разных хэндлеров),
    триал не продлевается заново."""
    with closing(_connect()) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO access (chat_id, trial_started_at, paid_until, coins) "
            "VALUES (?, ?, NULL, 0)",
            (chat_id, trial_started_at),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM access WHERE chat_id = ?", (chat_id,)).fetchone()
    return dict(row)


def add_coins(chat_id: int, amount: int):
    with closing(_connect()) as conn:
        conn.execute(
            "INSERT INTO access (chat_id, coins) VALUES (?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET coins = coins + ?",
            (chat_id, amount, amount),
        )
        conn.commit()


def spend_coins(chat_id: int, amount: int) -> bool:
    """Атомарно списывает коины, только если хватает баланса. Возвращает
    False без изменений, если коинов недостаточно."""
    with closing(_connect()) as conn:
        cur = conn.execute(
            "UPDATE access SET coins = coins - ? WHERE chat_id = ? AND coins >= ?",
            (amount, chat_id, amount),
        )
        conn.commit()
        return cur.rowcount > 0


def set_paid_until(chat_id: int, paid_until: str):
    with closing(_connect()) as conn:
        conn.execute("UPDATE access SET paid_until = ? WHERE chat_id = ?", (paid_until, chat_id))
        conn.commit()


def get_coins(chat_id: int) -> int:
    row = get_access(chat_id)
    return row["coins"] if row else 0


# ---------------------------------------------------------------------
# Mini App: каталог станций (пуш от скрапера, см. app/scheduler.py::
# _push_station_catalog). Хранит ВСЕ известные станции области, не
# только те, где топливо есть прямо сейчас — нужен для экрана
# «Добавить зону» (браузер станций).
# ---------------------------------------------------------------------

# Известные сети АЗС в России — используется, чтобы угадать бренд по
# названию станции (сырые данные с sberazs бренд отдельным полем не
# дают). Порядок важен: более длинные/специфичные названия проверяются
# раньше коротких, чтобы "Газпромнефть" не схлопнулась в "Газпром".
_KNOWN_BRANDS = [
    "Газпромнефть", "Роснефть", "Лукойл", "Башнефть", "Татнефть",
    "Сургутнефтегаз", "Русснефть", "Славнефть", "Трасса", "Несте", "Neste",
    "Shell", "Газпром", "ТНС", "ТЗК", "Fas-Trade", "Fas Trade",
    "Трансервис", "Нефтьмагистраль", "Экто", "ЕКА", "Икс", "Питер",
    "Роснефть-Ойл",
]


def _guess_brand(name: Optional[str]) -> str:
    if not name:
        return "Другое"
    lowered = name.lower()
    for brand in _KNOWN_BRANDS:
        if brand.lower() in lowered:
            return brand
    return "Другое"


def sync_station_catalog(area: str, stations: list[dict]):
    """stations: [{'station_id', 'name', 'address', 'lat', 'lon'}, ...]
    Полностью заменяет карточки станций этой области — если станция
    исчезла из ответа скрапера (например, больше не проходит по
    операциям), она удаляется и из каталога, чтобы не копить мусор."""
    with closing(_connect()) as conn:
        seen_ids = []
        for s in stations:
            sid = s["station_id"]
            seen_ids.append(sid)
            name = s.get("name")
            conn.execute(
                """
                INSERT INTO station_catalog (station_id, area, brand, name, address, lat, lon, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(station_id) DO UPDATE SET
                    area=excluded.area, brand=excluded.brand, name=excluded.name,
                    address=excluded.address, lat=excluded.lat, lon=excluded.lon,
                    updated_at=excluded.updated_at
                """,
                (sid, area, _guess_brand(name), name, s.get("address"), s.get("lat"), s.get("lon")),
            )
        if seen_ids:
            placeholders = ",".join("?" * len(seen_ids))
            conn.execute(
                f"DELETE FROM station_catalog WHERE area = ? AND station_id NOT IN ({placeholders})",
                (area, *seen_ids),
            )
        else:
            conn.execute("DELETE FROM station_catalog WHERE area = ?", (area,))
        conn.commit()


def get_catalog_areas() -> list[str]:
    with closing(_connect()) as conn:
        rows = conn.execute("SELECT DISTINCT area FROM station_catalog ORDER BY area").fetchall()
    return [r["area"] for r in rows]


def get_catalog_stations(area: str) -> list[dict]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT station_id, area, brand, name, address, lat, lon FROM station_catalog "
            "WHERE area = ? ORDER BY name",
            (area,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_catalog_brands() -> list[dict]:
    """Список брендов по всему каталогу (по всем областям сразу),
    отсортированный по популярности — для панели фильтра «Фирма»."""
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT COALESCE(brand, 'Другое') AS brand, COUNT(*) AS count "
            "FROM station_catalog GROUP BY brand ORDER BY count DESC, brand"
        ).fetchall()
    return [dict(r) for r in rows]


def search_catalog_stations(query: str = "", brands: Optional[list[str]] = None, limit: int = 60) -> list[dict]:
    """Поиск станций ПО ВСЕМУ каталогу (не привязан к одной области) —
    для экрана «Добавить зону»: текст ищется в адресе и названии,
    опционально фильтр по списку брендов. limit защищает от выгрузки
    всего каталога разом, если запрос слишком широкий."""
    sql = "SELECT station_id, area, brand, name, address, lat, lon FROM station_catalog WHERE 1=1"
    params: list = []
    q = (query or "").strip()
    if q:
        sql += " AND (address LIKE ? OR name LIKE ?)"
        like = f"%{q}%"
        params += [like, like]
    if brands:
        placeholders = ",".join("?" * len(brands))
        sql += f" AND COALESCE(brand, 'Другое') IN ({placeholders})"
        params += list(brands)
    sql += " ORDER BY name LIMIT ?"
    params.append(limit)
    with closing(_connect()) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_stations_by_ids(station_ids: list[str]) -> dict[str, dict]:
    """Для отображения зоны: station_id -> {name, address, area}."""
    if not station_ids:
        return {}
    with closing(_connect()) as conn:
        placeholders = ",".join("?" * len(station_ids))
        rows = conn.execute(
            f"SELECT station_id, area, brand, name, address, lat, lon FROM station_catalog "
            f"WHERE station_id IN ({placeholders})",
            station_ids,
        ).fetchall()
    return {r["station_id"]: dict(r) for r in rows}


def get_zone_owners_for_station(station_id: str) -> list[int]:
    """chat_id всех, у кого эта станция сохранена в какой-либо зоне —
    используется для автоматических оповещений по зонам (см.
    handle_notifications в light_bot.py): владелец зоны не должен
    отдельно оформлять area+category подписку, чтобы получать
    уведомления по своим станциям."""
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT DISTINCT z.chat_id FROM zones z "
            "JOIN zone_stations zs ON zs.zone_id = z.id "
            "WHERE zs.station_id = ?",
            (station_id,),
        ).fetchall()
    return [r["chat_id"] for r in rows]


# ---------------------------------------------------------------------
# Mini App: пользовательские зоны (кастомные списки избранных заправок).
# ---------------------------------------------------------------------

def create_zone(chat_id: int, name: str) -> int:
    with closing(_connect()) as conn:
        cur = conn.execute(
            "INSERT INTO zones (chat_id, name) VALUES (?, ?)", (chat_id, name)
        )
        conn.commit()
        return cur.lastrowid


def list_zones(chat_id: int) -> list[dict]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            """
            SELECT z.id, z.name, z.created_at, COUNT(zs.station_id) AS station_count
            FROM zones z
            LEFT JOIN zone_stations zs ON zs.zone_id = z.id
            WHERE z.chat_id = ?
            GROUP BY z.id
            ORDER BY z.created_at
            """,
            (chat_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_zone(chat_id: int, zone_id: int) -> Optional[dict]:
    """Возвращает зону, только если она принадлежит chat_id — простая
    проверка владения на уровне запроса, а не отдельным if в хэндлере."""
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT id, chat_id, name, created_at FROM zones WHERE id = ? AND chat_id = ?",
            (zone_id, chat_id),
        ).fetchone()
    return dict(row) if row else None


def delete_zone(chat_id: int, zone_id: int) -> bool:
    with closing(_connect()) as conn:
        cur = conn.execute("DELETE FROM zones WHERE id = ? AND chat_id = ?", (zone_id, chat_id))
        conn.execute("DELETE FROM zone_stations WHERE zone_id = ?", (zone_id,))
        conn.commit()
        return cur.rowcount > 0


def add_station_to_zone(chat_id: int, zone_id: int, station_id: str) -> bool:
    if get_zone(chat_id, zone_id) is None:
        return False
    with closing(_connect()) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO zone_stations (zone_id, station_id) VALUES (?, ?)",
            (zone_id, station_id),
        )
        conn.commit()
    return True


def remove_station_from_zone(chat_id: int, zone_id: int, station_id: str) -> bool:
    if get_zone(chat_id, zone_id) is None:
        return False
    with closing(_connect()) as conn:
        conn.execute(
            "DELETE FROM zone_stations WHERE zone_id = ? AND station_id = ?",
            (zone_id, station_id),
        )
        conn.commit()
    return True


def get_zone_station_ids(zone_id: int) -> list[str]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT station_id FROM zone_stations WHERE zone_id = ?", (zone_id,)
        ).fetchall()
    return [r["station_id"] for r in rows]


def get_zone_stations(chat_id: int, zone_id: int) -> list[dict]:
    """Станции зоны с именем/адресом/областью (джойн с каталогом) — для
    отображения в Mini App. Если станция вдруг пропала из каталога
    (скрапер больше её не видит), отдаём заглушку по station_id, чтобы
    список не "терял" запись молча."""
    if get_zone(chat_id, zone_id) is None:
        return []
    station_ids = get_zone_station_ids(zone_id)
    catalog = get_stations_by_ids(station_ids)
    result = []
    for sid in station_ids:
        info = catalog.get(sid)
        if info:
            result.append(info)
        else:
            result.append({"station_id": sid, "area": None, "name": "(станция больше не в каталоге)", "address": "", "lat": None, "lon": None})
    return result


def get_zone_areas(chat_id: int, zone_id: int) -> list[str]:
    """Уникальные области, которые нужно сканировать, чтобы обновить
    статус топлива по всем станциям этой зоны."""
    stations = get_zone_stations(chat_id, zone_id)
    return sorted({s["area"] for s in stations if s.get("area")})


# ---------------------------------------------------------------------
# Mini App: сессии сканирования (один клик "сканировать зону" может
# развернуться в несколько команд скраперу — по одной на область,
# затрагиваемую зоной) и сырые результаты команд для последующего опроса
# фронтендом (в отличие от текстового ответа в чат для /scan_city).
# ---------------------------------------------------------------------

def create_scan_session(chat_id: int, zone_id: Optional[int] = None) -> int:
    with closing(_connect()) as conn:
        cur = conn.execute(
            "INSERT INTO scan_sessions (chat_id, zone_id) VALUES (?, ?)", (chat_id, zone_id)
        )
        conn.commit()
        return cur.lastrowid


def get_scan_session(session_id: int) -> Optional[dict]:
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM scan_sessions WHERE id = ?", (session_id,)).fetchone()
    return dict(row) if row else None


def get_session_commands(session_id: int) -> list[dict]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM commands WHERE session_id = ? ORDER BY id", (session_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def save_command_result(command_id: int, area: str, stations: list[dict]):
    import json as _json
    with closing(_connect()) as conn:
        conn.execute(
            "INSERT INTO command_results (command_id, area, stations_json) VALUES (?, ?, ?) "
            "ON CONFLICT(command_id) DO UPDATE SET area=excluded.area, stations_json=excluded.stations_json",
            (command_id, area, _json.dumps(stations, ensure_ascii=False)),
        )
        conn.commit()


def get_command_result(command_id: int) -> Optional[dict]:
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT * FROM command_results WHERE command_id = ?", (command_id,)
        ).fetchone()
    return dict(row) if row else None
