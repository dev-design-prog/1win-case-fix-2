"""
Общие настройки, которые нужны и боту, и API (чтобы не дублировать списки).
Всё, что можно менять без правки кода, берётся из переменных окружения.
"""
import os

# Админы бота. Они же заходят в мини-апп во время техработ и без проверки подписки.
ADMIN_USERNAMES = ["Richie", "boros", "onewino"]

# Кто может заходить в мини-апп, когда включены технические работы.
# По умолчанию — все админы. Можно переопределить: MAINTENANCE_WHITELIST="Richie,Boros"
_wl = os.getenv("MAINTENANCE_WHITELIST", "")
MAINTENANCE_WHITELIST = [u.strip().lstrip("@") for u in _wl.split(",") if u.strip()] or list(ADMIN_USERNAMES)

# Обязательная подписка на канал.
# REQUIRED_CHANNEL — @username канала или числовой id (-100...). Бот должен быть АДМИНОМ канала,
# иначе Telegram не даст проверить подписчиков. Пока переменная пуста — проверка выключена.
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "").strip()
CHANNEL_SUB_URL = os.getenv("CHANNEL_SUB_URL", "https://catup.lol/casino").strip()

# Адрес Bot API (бот работает через свой сервер bot.catup.lol)
TELEGRAM_API_BASE = os.getenv("TELEGRAM_API_BASE", "https://bot.catup.lol").rstrip("/")


def _norm(username) -> str:
    return (username or "").strip().lstrip("@").lower()


def is_admin_username(username) -> bool:
    return _norm(username) in {_norm(u) for u in ADMIN_USERNAMES}


def is_maintenance_whitelisted(username) -> bool:
    return _norm(username) in {_norm(u) for u in MAINTENANCE_WHITELIST}
