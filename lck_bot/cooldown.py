from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True)
class CooldownRule:
    user_seconds: int
    guild_seconds: int


@dataclass(frozen=True)
class CooldownHit:
    scope: str
    remaining_seconds: int
    limit_seconds: int


class CooldownManager:
    def __init__(self) -> None:
        self._user_last_used: dict[tuple[str, int], float] = {}
        self._guild_last_used: dict[tuple[str, int], float] = {}

    def check(
        self,
        command_name: str,
        user_id: int,
        guild_id: int | None,
        rule: CooldownRule,
    ) -> CooldownHit | None:
        now = time.monotonic()
        user_key = (command_name, user_id)
        user_hit = self._hit(
            last_used=self._user_last_used.get(user_key),
            now=now,
            limit_seconds=rule.user_seconds,
            scope="user",
        )
        if user_hit:
            return user_hit

        guild_hit = None
        guild_key = None
        if guild_id is not None:
            guild_key = (command_name, guild_id)
            guild_hit = self._hit(
                last_used=self._guild_last_used.get(guild_key),
                now=now,
                limit_seconds=rule.guild_seconds,
                scope="guild",
            )
        if guild_hit:
            return guild_hit

        self._user_last_used[user_key] = now
        if guild_key is not None:
            self._guild_last_used[guild_key] = now
        return None

    def _hit(
        self,
        last_used: float | None,
        now: float,
        limit_seconds: int,
        scope: str,
    ) -> CooldownHit | None:
        if last_used is None:
            return None
        elapsed = now - last_used
        if elapsed >= limit_seconds:
            return None
        remaining = max(1, int(limit_seconds - elapsed + 0.999))
        return CooldownHit(scope=scope, remaining_seconds=remaining, limit_seconds=limit_seconds)


def format_remaining(seconds: int) -> str:
    minutes, rest = divmod(seconds, 60)
    if minutes:
        return f"{minutes}분 {rest:02d}초"
    return f"{rest}초"
