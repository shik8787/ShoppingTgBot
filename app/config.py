from dataclasses import dataclass
import os


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    database_path: str
    allowed_chat_ids: frozenset[int]
    max_items_per_list: int


def get_settings() -> Settings:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Missing required environment variable: TELEGRAM_BOT_TOKEN")

    database_path = os.getenv("DATABASE_PATH", "/data/shopping.db").strip()
    if not database_path:
        raise RuntimeError("DATABASE_PATH must not be empty")

    return Settings(
        telegram_bot_token=token,
        database_path=database_path,
        allowed_chat_ids=_parse_chat_ids(os.getenv("ALLOWED_CHAT_IDS", "")),
        max_items_per_list=_bounded_int("MAX_ITEMS_PER_LIST", 50, 1, 50),
    )


def _parse_chat_ids(raw: str) -> frozenset[int]:
    values = []
    for item in raw.split(","):
        value = item.strip()
        if not value:
            continue
        try:
            values.append(int(value))
        except ValueError as error:
            raise RuntimeError(f"Invalid Telegram chat ID: {value}") from error
    return frozenset(values)


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as error:
        raise RuntimeError(f"{name} must be an integer") from error
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value
