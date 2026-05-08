from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any, Awaitable, Callable, TypeVar

import aiohttp


class LolesportsError(RuntimeError):
    pass


T = TypeVar("T")
LIVE_WINDOW_DELAY_SECONDS = 90


@dataclass(frozen=True)
class SelectedEvent:
    event: dict[str, Any]
    game: dict[str, Any] | None


@dataclass
class _CacheEntry:
    expires_at: float
    payload: dict[str, Any]


class LolesportsClient:
    def __init__(self, api_key: str, locale: str, league_id: str) -> None:
        self.api_key = api_key
        self.locale = locale
        self.league_id = league_id
        self._session: aiohttp.ClientSession | None = None
        self._cache: dict[tuple[str, tuple[tuple[str, str], ...]], _CacheEntry] = {}

    async def __aenter__(self) -> LolesportsClient:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=15)
            headers = {
                "User-Agent": "lck-discord-bot/0.1",
                "x-api-key": self.api_key,
            }
            self._session = aiohttp.ClientSession(timeout=timeout, headers=headers)

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        ttl_seconds: int = 0,
    ) -> dict[str, Any]:
        key = _cache_key(url, params)
        if ttl_seconds > 0:
            cached = self._cache.get(key)
            if cached and cached.expires_at > monotonic():
                return cached.payload

        await self.start()
        assert self._session is not None
        try:
            async with self._session.get(url, params=params) as response:
                if response.status == 204:
                    payload: dict[str, Any] = {}
                    if ttl_seconds > 0:
                        self._cache[key] = _CacheEntry(monotonic() + ttl_seconds, payload)
                    return payload
                if response.status >= 400:
                    text = await response.text()
                    raise LolesportsError(f"LoL Esports API {response.status}: {text[:200]}")
                payload = await response.json()
                if ttl_seconds > 0:
                    self._cache[key] = _CacheEntry(monotonic() + ttl_seconds, payload)
                return payload
        except TimeoutError as exc:
            raise LolesportsError("LoL Esports API request timed out.") from exc
        except aiohttp.ClientError as exc:
            raise LolesportsError(f"LoL Esports API request failed: {exc}") from exc

    async def get_schedule(self) -> dict[str, Any]:
        return await self._get(
            "https://esports-api.lolesports.com/persisted/gw/getSchedule",
            {"hl": self.locale, "leagueId": self.league_id},
            ttl_seconds=60,
        )

    async def get_schedule_events(self) -> list[dict[str, Any]]:
        return self._schedule_events(await self.get_schedule())

    async def get_event_details(self, event_id: str) -> dict[str, Any]:
        return await self._get(
            "https://esports-api.lolesports.com/persisted/gw/getEventDetails",
            {"hl": self.locale, "id": event_id},
            ttl_seconds=60,
        )

    async def get_live_window(
        self, game_id: str, starting_time: str | None = None
    ) -> dict[str, Any]:
        params = {"startingTime": starting_time} if starting_time else None
        return await self._get(
            f"https://feed.lolesports.com/livestats/v1/window/{game_id}",
            params,
            ttl_seconds=15,
        )

    async def get_live_details(
        self, game_id: str, starting_time: str | None = None
    ) -> dict[str, Any]:
        params = {"startingTime": starting_time} if starting_time else None
        return await self._get(
            f"https://feed.lolesports.com/livestats/v1/details/{game_id}",
            params,
            ttl_seconds=60,
        )

    async def find_current_event(self) -> SelectedEvent | None:
        events = await self.get_schedule_events()
        if not events:
            return None

        live = [event for event in events if self._state(event) in {"inprogress", "in_progress"}]
        candidate = live[0] if live else events[0]
        details = await self.get_event_details(self._event_id(candidate))
        event = details.get("data", {}).get("event", candidate)
        return SelectedEvent(event=event, game=self._select_game(event, prefer_live=True))

    async def find_recent_completed_event(self) -> SelectedEvent | None:
        events = await self.get_schedule_events()
        completed = [event for event in events if self._state(event) == "completed"]
        if not completed:
            return None

        completed.sort(key=lambda event: str(event.get("startTime") or ""), reverse=True)
        candidate = completed[0]
        details = await self.get_event_details(self._event_id(candidate))
        event = details.get("data", {}).get("event", candidate)
        return SelectedEvent(event=event, game=self._select_game(event, prefer_live=False))

    async def find_live_window(self) -> tuple[SelectedEvent, dict[str, Any]] | None:
        selected = await self.find_current_event()
        if not selected or not selected.game:
            return None
        game_id = str(selected.game.get("id", ""))
        if not game_id:
            return None
        window = await self.get_live_window(game_id, latest_starting_time())
        return selected, window

    def _schedule_events(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        return payload.get("data", {}).get("schedule", {}).get("events", [])

    def _select_game(self, event: dict[str, Any], prefer_live: bool) -> dict[str, Any] | None:
        games = event.get("match", {}).get("games", [])
        if not games:
            return None

        if prefer_live:
            for game in games:
                if self._state(game) in {"inprogress", "in_progress"}:
                    return game
            for game in games:
                if self._state(game) == "unstarted":
                    return game

        for game in reversed(games):
            if self._state(game) == "completed":
                return game
        return games[0]

    def _state(self, item: dict[str, Any]) -> str:
        return str(item.get("state", "")).replace("-", "_").lower()

    def _event_id(self, event: dict[str, Any]) -> str:
        event_id = event.get("id") or event.get("match", {}).get("id")
        if not event_id:
            raise LolesportsError("LoL Esports schedule event has no id or match.id.")
        return str(event_id)

    def event_id(self, event: dict[str, Any]) -> str:
        return self._event_id(event)


def _cache_key(url: str, params: dict[str, Any] | None) -> tuple[str, tuple[tuple[str, str], ...]]:
    return url, tuple(sorted((str(key), str(value)) for key, value in (params or {}).items()))


async def retry_api_call(call: Callable[[], Awaitable[T]], attempts: int = 2) -> T:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return await call()
        except LolesportsError as exc:
            last_error = exc
            if attempt + 1 < attempts:
                await asyncio.sleep(1)
    assert last_error is not None
    raise last_error


def latest_starting_time() -> str:
    now = (datetime.now(UTC) - timedelta(seconds=LIVE_WINDOW_DELAY_SECONDS)).replace(microsecond=0)
    rounded = now.replace(second=now.second - (now.second % 10))
    return rounded.isoformat().replace("+00:00", "Z")
