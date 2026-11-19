from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_ids: frozenset[int]
    admin_chat_id: int
    payment_mode: str = "manual"
    telegram_provider_token: str = ""
    kaspi_pay_url: str = ""
    privacy_policy_url: str = ""
    db_path: str = str(ROOT / "data" / "bot.sqlite3")
    catalog_path: str = str(ROOT / "catalog.yaml")
    ai_model: str = "claude-opus-5-5"
    ai_daily_limit: int = 40


def load_settings() -> Settings:
    load_dotenv(ROOT / ".env")
    missing = [
        name
        for name in ("BOT_TOKEN", "ADMIN_IDS", "ADMIN_CHAT_ID")
        if not os.getenv(name, "").strip()
    ]
    if missing:
        raise ConfigError(f"Заполните в .env: {', '.join(missing)}")

    try:
        admin_ids = frozenset(
            int(part) for part in os.environ["ADMIN_IDS"].split(",") if part.strip()
        )
        admin_chat_id = int(os.environ["ADMIN_CHAT_ID"])
        daily_limit = int(os.getenv("AI_DAILY_LIMIT", "40"))
    except ValueError as exc:
        raise ConfigError("ADMIN_IDS, ADMIN_CHAT_ID и AI_DAILY_LIMIT должны быть числами") from exc

    mode = os.getenv("PAYMENT_MODE", "manual").strip().lower()
    if mode not in {"manual", "telegram"}:
        raise ConfigError("PAYMENT_MODE должен быть manual или telegram")
    provider_token = os.getenv("TELEGRAM_PROVIDER_TOKEN", "").strip()
    if mode == "telegram" and not provider_token:
        raise ConfigError("Для PAYMENT_MODE=telegram нужен TELEGRAM_PROVIDER_TOKEN")

    return Settings(
        bot_token=os.environ["BOT_TOKEN"].strip(),
        admin_ids=admin_ids,
        admin_chat_id=admin_chat_id,
        payment_mode=mode,
        telegram_provider_token=provider_token,
        kaspi_pay_url=os.getenv("KASPI_PAY_URL", "").strip(),
        privacy_policy_url=os.getenv("PRIVACY_POLICY_URL", "").strip(),
        db_path=os.getenv("DB_PATH", str(ROOT / "data" / "bot.sqlite3")),
        ai_model=os.getenv("AI_MODEL", "claude-opus-5-5").strip(),
        ai_daily_limit=daily_limit,
    )
