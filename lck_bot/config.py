from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


DEFAULT_LCK_API_KEY = "0TvQnueqKa5mxJntVWt0w4LpLfEkrV1Ta8rQBb9Z"


@dataclass(frozen=True)
class Settings:
    discord_token: str
    discord_guild_id: int | None
    locale: str
    league_id: str
    api_key: str
    poll_seconds: int


def load_settings() -> Settings:
    load_dotenv()

    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise RuntimeError("DISCORD_TOKEN is required. Copy .env.example to .env and set it.")

    guild_id_raw = os.getenv("DISCORD_GUILD_ID", "").strip()
    guild_id = int(guild_id_raw) if guild_id_raw else None

    poll_seconds_raw = os.getenv("LCK_POLL_SECONDS", "15").strip()
    poll_seconds = max(5, int(poll_seconds_raw))

    return Settings(
        discord_token=token,
        discord_guild_id=guild_id,
        locale=os.getenv("LCK_LOCALE", "ko-KR").strip() or "ko-KR",
        league_id=os.getenv("LCK_LEAGUE_ID", "98767991310872058").strip(),
        api_key=os.getenv("LCK_API_KEY", DEFAULT_LCK_API_KEY).strip() or DEFAULT_LCK_API_KEY,
        poll_seconds=poll_seconds,
    )
