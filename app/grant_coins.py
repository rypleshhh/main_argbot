"""
Начислить коины пользователю вручную — пока не подключён эквайринг
(см. TODO в app/light_bot.py::cb_topup_pkg и app/subscription.py).

chat_id пользователя виден ему самому в разделе «💳 Тариф и коины»
(строка «🆔 Ваш ID»).

Запуск (на лёгком сервере):

    docker compose -f docker-compose.bot.yml exec bot python -m app.grant_coins <chat_id> <amount>

Пример:

    docker compose -f docker-compose.bot.yml exec bot python -m app.grant_coins 123456789 300
"""

import argparse

from app import bot_storage as storage


def main():
    parser = argparse.ArgumentParser(description="Начислить коины пользователю бота")
    parser.add_argument("chat_id", type=int, help="Telegram chat_id пользователя")
    parser.add_argument("amount", type=int, help="Сколько коинов начислить (можно отрицательное — списать)")
    args = parser.parse_args()

    storage.init_db()
    storage.add_coins(args.chat_id, args.amount)
    balance = storage.get_coins(args.chat_id)
    print(f"chat_id={args.chat_id}: начислено {args.amount}, текущий баланс {balance} коинов.")


if __name__ == "__main__":
    main()
