from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from lck_bot.formatting import format_game_clock
from lck_bot.lolesports import LolesportsClient, LolesportsError
from lck_bot.match_data import DraftLine, game_number, game_state, match_title, max_sets

LOGGER = logging.getLogger(__name__)


@dataclass
class KillEvent:
    game_id: str
    timestamp_ms: int | None
    killer: str
    victim: str
    inferred: bool = True


@dataclass
class GameSnapshot:
    game_id: str
    event_name: str
    set_number: int
    max_sets: int
    state: str
    blue_name: str
    red_name: str
    blue_gold: int
    red_gold: int
    blue_kills: int
    red_kills: int
    blue_barons: int
    red_barons: int
    timestamp_ms: int | None
    blue_dragons: list[str] = field(default_factory=list)
    red_dragons: list[str] = field(default_factory=list)
    draft: list[DraftLine] = field(default_factory=list)
    kill_events: list[KillEvent] = field(default_factory=list)

    @property
    def gold_diff(self) -> int:
        return self.blue_gold - self.red_gold


class LiveTracker:
    def __init__(self, client: LolesportsClient, poll_seconds: int) -> None:
        self.client = client
        self.poll_seconds = poll_seconds
        self.snapshots: dict[str, GameSnapshot] = {}
        self._last_frame_by_game: dict[str, dict[str, Any]] = {}
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()
        self._lock = asyncio.Lock()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopped.clear()
            self._task = asyncio.create_task(self._run(), name="lck-live-tracker")

    async def stop(self) -> None:
        self._stopped.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def refresh_once(self) -> GameSnapshot | None:
        try:
            live = await self.client.find_live_window()
        except LolesportsError:
            raise
        if not live:
            return None

        selected, window = live
        game_id = str(selected.game.get("id")) if selected.game else str(window.get("esportsGameId", ""))
        async with self._lock:
            return self._ingest_window(game_id, window, selected.event, selected.game or {})

    async def get_snapshot(self) -> GameSnapshot | None:
        snapshot = await self.refresh_once()
        if snapshot:
            return snapshot
        async with self._lock:
            if not self.snapshots:
                return None
            return next(reversed(self.snapshots.values()))

    async def _run(self) -> None:
        while not self._stopped.is_set():
            try:
                await self.refresh_once()
            except LolesportsError as exc:
                LOGGER.debug("Live tracker poll failed: %s", exc)
            except Exception:
                LOGGER.exception("Unexpected live tracker error")

            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                continue

    def _ingest_window(
        self,
        game_id: str,
        window: dict[str, Any],
        event: dict[str, Any],
        game: dict[str, Any],
    ) -> GameSnapshot | None:
        frames = window.get("frames", [])
        if not frames:
            return None

        metadata = window.get("gameMetadata", {})
        participant_names = _participant_names(metadata)
        team_names = _team_names(metadata, event)
        draft = _draft_lines(metadata, team_names)

        last_seen = self._last_frame_by_game.get(game_id)
        new_events: list[KillEvent] = []
        ordered_frames = sorted(frames, key=_frame_sort_time)
        for frame in ordered_frames:
            if last_seen and _frame_sort_time(frame) <= _frame_sort_time(last_seen):
                continue
            if last_seen:
                new_events.extend(_infer_kill_events(game_id, last_seen, frame, participant_names))
            last_seen = frame

        if last_seen is None:
            return None

        self._last_frame_by_game[game_id] = last_seen
        blue = last_seen.get("blueTeam", {})
        red = last_seen.get("redTeam", {})

        existing = self.snapshots.get(
            game_id,
            GameSnapshot(
                game_id=game_id,
                event_name=match_title(event),
                set_number=game_number(game, 1),
                max_sets=max_sets(event),
                state=game_state(game),
                blue_name=team_names[0],
                red_name=team_names[1],
                blue_gold=0,
                red_gold=0,
                blue_kills=0,
                red_kills=0,
                blue_barons=0,
                red_barons=0,
                timestamp_ms=None,
            ),
        ).kill_events

        snapshot = GameSnapshot(
            game_id=game_id,
            event_name=match_title(event),
            set_number=game_number(game, 1),
            max_sets=max_sets(event),
            state=game_state(game),
            blue_name=team_names[0],
            red_name=team_names[1],
            blue_gold=int(blue.get("totalGold", 0) or 0),
            red_gold=int(red.get("totalGold", 0) or 0),
            blue_kills=int(blue.get("totalKills", 0) or 0),
            red_kills=int(red.get("totalKills", 0) or 0),
            blue_barons=int(blue.get("barons", 0) or 0),
            red_barons=int(red.get("barons", 0) or 0),
            blue_dragons=_dragon_list(blue.get("dragons")),
            red_dragons=_dragon_list(red.get("dragons")),
            timestamp_ms=_frame_time(last_seen),
            draft=draft,
            kill_events=[*existing, *new_events][-30:],
        )
        self.snapshots[game_id] = snapshot
        return snapshot


def _team_names(metadata: dict[str, Any], event: dict[str, Any]) -> tuple[str, str]:
    blue = metadata.get("blueTeamMetadata", {})
    red = metadata.get("redTeamMetadata", {})
    id_to_name = {
        str(team.get("id")): str(team.get("code") or team.get("name"))
        for team in event.get("match", {}).get("teams", [])
        if team.get("id")
    }
    return (
        id_to_name.get(str(blue.get("esportsTeamId")), str(blue.get("esportsTeamId") or "Blue")),
        id_to_name.get(str(red.get("esportsTeamId")), str(red.get("esportsTeamId") or "Red")),
    )


def _draft_lines(metadata: dict[str, Any], team_names: tuple[str, str]) -> list[DraftLine]:
    lines: list[DraftLine] = []
    for side, side_label, team_name in (
        ("blueTeamMetadata", "블루", team_names[0]),
        ("redTeamMetadata", "레드", team_names[1]),
    ):
        team_metadata = metadata.get(side, {})
        picks = [
            _champion_name(participant)
            for participant in team_metadata.get("participantMetadata", [])
            if _champion_name(participant) != "-"
        ]
        bans = _extract_bans(team_metadata)
        lines.append(DraftLine(team=team_name, side=side_label, picks=picks, bans=bans))
    return lines


def _extract_bans(source: dict[str, Any]) -> list[str]:
    for key in ("bans", "bannedChampions", "bannedChampionIds", "ban"):
        value = source.get(key)
        if isinstance(value, list):
            return [_champion_name(item) if isinstance(item, dict) else str(item) for item in value]
        if isinstance(value, dict):
            return [_champion_name(value)]
    return []


def _dragon_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item]


def _participant_names(metadata: dict[str, Any]) -> dict[int, str]:
    result: dict[int, str] = {}
    for side_key in ("blueTeamMetadata", "redTeamMetadata"):
        participants = metadata.get(side_key, {}).get("participantMetadata", [])
        for participant in participants:
            participant_id = participant.get("participantId")
            if participant_id is None:
                continue
            result[int(participant_id)] = (
                participant.get("summonerName")
                or participant.get("name")
                or participant.get("esportsPlayerId")
                or f"P{participant_id}"
            )
    return result


def _champion_name(participant: dict[str, Any]) -> str:
    champion = participant.get("champion")
    if isinstance(champion, dict):
        return str(champion.get("name") or champion.get("id") or "-")
    for key in ("championName", "championId", "champion", "name", "id"):
        if participant.get(key):
            return str(participant[key])
    return "-"


def _frame_time(frame: dict[str, Any]) -> int | None:
    for key in ("gameTime", "gameTimeMs", "gameTimeMillis"):
        timestamp = frame.get(key)
        if isinstance(timestamp, int):
            return timestamp
        if isinstance(timestamp, str) and timestamp.isdigit():
            return int(timestamp)
    return None


def _frame_sort_time(frame: dict[str, Any]) -> int:
    timestamp = frame.get("rfc460Timestamp") or frame.get("rfc3339Timestamp")
    if isinstance(timestamp, str):
        try:
            return int(datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            return 0
    game_time = _frame_time(frame)
    return game_time or 0


def _participants(frame: dict[str, Any]) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for team_key in ("blueTeam", "redTeam"):
        for participant in frame.get(team_key, {}).get("participants", []):
            participant_id = participant.get("participantId")
            if participant_id is not None:
                result[int(participant_id)] = participant
    return result


def _infer_kill_events(
    game_id: str,
    previous: dict[str, Any],
    current: dict[str, Any],
    names: dict[int, str],
) -> list[KillEvent]:
    before = _participants(previous)
    after = _participants(current)
    killers: list[str] = []
    victims: list[str] = []

    for participant_id, current_participant in after.items():
        previous_participant = before.get(participant_id, {})
        kill_delta = int(current_participant.get("kills", 0) or 0) - int(
            previous_participant.get("kills", 0) or 0
        )
        death_delta = int(current_participant.get("deaths", 0) or 0) - int(
            previous_participant.get("deaths", 0) or 0
        )
        if kill_delta > 0:
            killers.extend([names.get(participant_id, f"P{participant_id}")] * kill_delta)
        if death_delta > 0:
            victims.extend([names.get(participant_id, f"P{participant_id}")] * death_delta)

    if not killers or not victims:
        return []

    timestamp_ms = _frame_time(current)
    if len(killers) == 1 and len(victims) == 1:
        return [KillEvent(game_id, timestamp_ms, killers[0], victims[0], inferred=True)]

    return [
        KillEvent(
            game_id,
            timestamp_ms,
            " / ".join(killers),
            " / ".join(victims),
            inferred=True,
        )
    ]


def format_kill_event(event: KillEvent) -> str:
    marker = "추론" if event.inferred else "확정"
    return f"{format_game_clock(event.timestamp_ms)} | {event.killer} -> {event.victim} ({marker})"
