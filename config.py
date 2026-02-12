import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Settings:
    bot_token: str
    db_url: str
    super_admin_id: int | None
    qr_expire_minutes: int = 15


def get_settings() -> Settings:
    token = os.getenv("BOT_TOKEN", "")
    if not token:
        raise RuntimeError("BOT_TOKEN is not set")

    db_url = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./bot.db")

    super_admin_raw = os.getenv("SUPER_ADMIN_ID")
    super_admin_id = int(super_admin_raw) if super_admin_raw else None

    return Settings(
        bot_token=token,
        db_url=db_url,
        super_admin_id=super_admin_id,
    )

