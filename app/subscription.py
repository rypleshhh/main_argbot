"""
Доступ к боту: пробный период 12ч при первом /start, дальше — платная
подписка на app.config.SUBSCRIPTION_DAYS за app.config.SUBSCRIPTION_COST_COINS
коинов. Хранение — в bot_storage (таблица `access`), здесь только расчёты.

Эквайринг НЕ подключён. Кнопка "Пополнить коины" сейчас показывает
заглушку-инструкцию — см. COIN_PACKAGES и TODO в light_bot.py
(cb_topup_pkg). Пока платёжный шлюз не готов, коины начисляет админ
вручную: `python -m app.grant_coins <chat_id> <amount>` (см. этот файл).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.config import PAYWALL_ENABLED, SUBSCRIPTION_COST_COINS, SUBSCRIPTION_DAYS, TRIAL_HOURS
from app.formatting import DISPLAY_TZ
from app import bot_storage as storage

# Пакеты пополнения коинов, которые увидит пользователь в кнопках.
# Цены — заглушка, подставьте свои, когда подключите эквайринг.
COIN_PACKAGES = [
    {"id": "s", "coins": 100, "price_rub": 199},
    {"id": "m", "coins": 300, "price_rub": 499},
    {"id": "l", "coins": 1000, "price_rub": 1490},
]


@dataclass
class AccessStatus:
    has_access: bool
    on_trial: bool
    paid_active: bool
    trial_ends_at: Optional[datetime]  # UTC
    paid_until: Optional[datetime]     # UTC
    coins: int


def _now() -> datetime:
    """Текущее время в UTC, наивное — в таком виде даты хранятся в БД."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_utc_iso(dt: Optional[datetime]) -> Optional[str]:
    """Наивный UTC -> ISO с явным '+00:00'. Без пояса JS (new Date(...))
    читает строку как ЛОКАЛЬНОЕ время и сдвигает отображение."""
    return dt.replace(tzinfo=timezone.utc).isoformat() if dt else None


def _parse(value: Optional[str]) -> Optional[datetime]:
    return datetime.strptime(value, storage.DT_FORMAT) if value else None


def _format(dt: datetime) -> str:
    return dt.strftime(storage.DT_FORMAT)


def _to_display(dt: datetime) -> str:
    """UTC (наивный datetime) -> строка в московском времени, как везде
    в проекте (см. app.formatting.DISPLAY_TZ)."""
    dt_utc = dt.replace(tzinfo=timezone.utc)
    return dt_utc.astimezone(DISPLAY_TZ).strftime("%d.%m.%Y %H:%M")


def format_dt_display(dt: datetime) -> str:
    """Публичная обёртка над _to_display — для внешнего кода (light_bot.py)."""
    return _to_display(dt)


def _ensure_row(chat_id: int) -> dict:
    row = storage.get_access(chat_id)
    if row is None:
        row = storage.start_trial(chat_id, _format(_now()))
    return row


def ensure_trial_started(chat_id: int) -> bool:
    """Вызывать из /start. Возвращает True, только если это первый визит
    пользователя (запись доступа только что создана и триал только что
    стартовал) — чтобы приветственное сообщение о триале не дублировалось
    при каждом повторном /start."""
    existing = storage.get_access(chat_id)
    if existing is not None:
        return False
    storage.start_trial(chat_id, _format(_now()))
    return True


def get_status(chat_id: int) -> AccessStatus:
    """Читает/при необходимости создаёт запись доступа (первый визит —
    автоматически стартует пробный период)."""
    row = _ensure_row(chat_id)
    now = _now()

    trial_ends_at = None
    if row["trial_started_at"]:
        trial_ends_at = _parse(row["trial_started_at"]) + timedelta(hours=TRIAL_HOURS)

    paid_until = _parse(row["paid_until"])

    paid_active = paid_until is not None and now < paid_until
    trial_active = trial_ends_at is not None and now < trial_ends_at

    return AccessStatus(
        has_access=paid_active or trial_active,
        on_trial=trial_active and not paid_active,
        paid_active=paid_active,
        trial_ends_at=trial_ends_at,
        paid_until=paid_until,
        coins=row["coins"],
    )


def has_access(chat_id: int) -> bool:
    if not PAYWALL_ENABLED:
        return True
    return get_status(chat_id).has_access


def has_paid_access(chat_id: int) -> bool:
    """В отличие от has_access(), НЕ засчитывает пробный период — только
    оплаченную подписку. Используется там, где триал не должен давать
    полную функциональность (сейчас: push-уведомления о топливе)."""
    if not PAYWALL_ENABLED:
        return True
    return get_status(chat_id).paid_active


def purchase_subscription(chat_id: int) -> tuple[bool, AccessStatus]:
    """Списывает SUBSCRIPTION_COST_COINS и продлевает подписку на
    SUBSCRIPTION_DAYS от большего из (сейчас, текущий paid_until) —
    чтобы подписку можно было продлить заранее, не теряя остаток."""
    _ensure_row(chat_id)
    if not storage.spend_coins(chat_id, SUBSCRIPTION_COST_COINS):
        return False, get_status(chat_id)

    now = _now()
    row = storage.get_access(chat_id)
    current_paid_until = _parse(row["paid_until"])
    base = current_paid_until if (current_paid_until and current_paid_until > now) else now
    new_paid_until = base + timedelta(days=SUBSCRIPTION_DAYS)

    storage.set_paid_until(chat_id, _format(new_paid_until))
    return True, get_status(chat_id)


def fmt_remaining(delta: timedelta) -> str:
    total_minutes = int(delta.total_seconds() // 60)
    if total_minutes <= 0:
        return "меньше минуты"
    days, rem_minutes = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(rem_minutes, 60)
    parts = []
    if days:
        parts.append(f"{days} дн")
    if hours:
        parts.append(f"{hours} ч")
    if not days and minutes:
        parts.append(f"{minutes} мин")
    return " ".join(parts) if parts else "меньше минуты"


def format_status_text(chat_id: int, status: AccessStatus) -> str:
    lines = []
    if status.on_trial:
        remaining = status.trial_ends_at - _now()
        lines.append(f"🎁 Пробный доступ активен, осталось {fmt_remaining(remaining)}.")
        lines.append("ℹ️ На пробном доступны сканы городов и оформление подписок, "
                      "но push-уведомления о появлении топлива придут только после оплаты.")
    elif status.paid_until and status.has_access:
        lines.append(f"✅ Подписка активна до {_to_display(status.paid_until)} (МСК).")
    else:
        lines.append("⛔️ Доступ закончился.")

    lines.append(f"💰 Баланс: {status.coins} коинов")
    lines.append(f"💳 Подписка: {SUBSCRIPTION_DAYS} дней за {SUBSCRIPTION_COST_COINS} коинов")
    lines.append(f"🆔 Ваш ID (для поддержки): <code>{chat_id}</code>")
    return "\n".join(lines)
