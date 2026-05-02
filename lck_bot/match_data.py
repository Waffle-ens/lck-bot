from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from lck_bot.formatting import normalize_role


@dataclass(frozen=True)
class PlayerLine:
    team: str
    side: str
    participant_id: int
    role: str
    player: str
    champion: str
    kills: int | None = None
    deaths: int | None = None
    assists: int | None = None
    damage: int | None = None
    result: str = "-"


@dataclass(frozen=True)
class DraftLine:
    team: str
    side: str
    picks: list[str] = field(default_factory=list)
    bans: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class GameReport:
    game_id: str
    set_number: int
    max_sets: int
    state: str
    duration_ms: int | None
    winner: str | None
    draft: list[DraftLine]
    players: list[PlayerLine]


def game_number(game: dict[str, Any], fallback: int) -> int:
    return _to_int(game.get("number")) or fallback


def max_sets(event: dict[str, Any]) -> int:
    count = event.get("match", {}).get("strategy", {}).get("count")
    parsed = _to_int(count)
    if parsed:
        return parsed
    games = event.get("match", {}).get("games", [])
    return max(1, len(games))


def game_state(game: dict[str, Any] | None) -> str:
    return str((game or {}).get("state") or "").replace("-", "_").lower()


def state_label(value: Any) -> str:
    state = str(value or "").replace("-", "_").lower()
    if state == "completed":
        return "종료"
    if state in {"inprogress", "in_progress"}:
        return "진행중"
    if state == "unstarted":
        return "예정"
    return state or "-"


def event_team_names(event: dict[str, Any]) -> dict[str, str]:
    return {
        str(team.get("id")): str(team.get("code") or team.get("name"))
        for team in event.get("match", {}).get("teams", [])
        if team.get("id")
    }


def match_title(event: dict[str, Any]) -> str:
    teams = event.get("match", {}).get("teams", [])
    if len(teams) >= 2:
        return f"{teams[0].get('code') or teams[0].get('name')} vs {teams[1].get('code') or teams[1].get('name')}"
    return event.get("blockName") or "LCK 경기"


def build_game_report(
    event: dict[str, Any],
    game: dict[str, Any],
    window: dict[str, Any] | None,
    details: dict[str, Any] | None = None,
    fallback_number: int = 1,
) -> GameReport:
    window = window or {}
    details = details or {}
    metadata = window.get("gameMetadata", {})
    team_names = _team_names(metadata, event)
    last_frame = _last_frame(window)
    stats_by_participant = _stats_by_participant(last_frame, details)
    winner = _winner_name(game, event, metadata, last_frame)

    return GameReport(
        game_id=str(game.get("id") or window.get("esportsGameId") or ""),
        set_number=game_number(game, fallback_number),
        max_sets=max_sets(event),
        state=game_state(game),
        duration_ms=_frame_time(last_frame) or _vod_duration_ms(game) or _duration_from_frames(window),
        winner=winner,
        draft=_draft_lines(metadata, details, team_names),
        players=_player_lines(metadata, stats_by_participant, team_names, winner),
    )


def _team_names(metadata: dict[str, Any], event: dict[str, Any]) -> tuple[str, str]:
    id_to_name = event_team_names(event)
    blue_id = str(metadata.get("blueTeamMetadata", {}).get("esportsTeamId") or "")
    red_id = str(metadata.get("redTeamMetadata", {}).get("esportsTeamId") or "")
    return (
        id_to_name.get(blue_id, blue_id or "Blue"),
        id_to_name.get(red_id, red_id or "Red"),
    )


def _draft_lines(
    metadata: dict[str, Any],
    details: dict[str, Any],
    team_names: tuple[str, str],
) -> list[DraftLine]:
    lines = []
    for side, side_label, team_name in (
        ("blueTeamMetadata", "블루", team_names[0]),
        ("redTeamMetadata", "레드", team_names[1]),
    ):
        team_metadata = metadata.get(side, {})
        picks = [_champion_name(participant) for participant in team_metadata.get("participantMetadata", [])]
        picks = [pick for pick in picks if pick != "-"]
        bans = _extract_bans(team_metadata) or _extract_team_bans(details, team_name, side_label)
        lines.append(DraftLine(team=team_name, side=side_label, picks=picks, bans=bans))
    return lines


def _player_lines(
    metadata: dict[str, Any],
    stats_by_participant: dict[int, dict[str, Any]],
    team_names: tuple[str, str],
    winner: str | None,
) -> list[PlayerLine]:
    players: list[PlayerLine] = []
    for side, side_label, team_name in (
        ("blueTeamMetadata", "블루", team_names[0]),
        ("redTeamMetadata", "레드", team_names[1]),
    ):
        for participant in metadata.get(side, {}).get("participantMetadata", []):
            participant_id = _to_int(participant.get("participantId")) or 0
            stats = stats_by_participant.get(participant_id, {})
            players.append(
                PlayerLine(
                    team=team_name,
                    side=side_label,
                    participant_id=participant_id,
                    role=normalize_role(participant.get("role"), participant_id),
                    player=_player_name(participant),
                    champion=_champion_name(participant),
                    kills=_stat_int(stats, "kills"),
                    deaths=_stat_int(stats, "deaths"),
                    assists=_stat_int(stats, "assists"),
                    damage=_damage(stats),
                    result=_result_label(team_name, winner),
                )
            )
    return players


def _last_frame(window: dict[str, Any]) -> dict[str, Any]:
    frames = window.get("frames", [])
    if not frames:
        return {}
    return max(frames, key=_frame_sort_time)


def _frame_time(frame: dict[str, Any]) -> int | None:
    for key in ("gameTime", "gameTimeMs", "gameTimeMillis", "timestamp"):
        timestamp = frame.get(key)
        if isinstance(timestamp, int):
            return timestamp
        if isinstance(timestamp, str) and timestamp.isdigit():
            return int(timestamp)
    return 0


def _frame_sort_time(frame: dict[str, Any]) -> int:
    game_time = _frame_time(frame)
    if game_time is not None:
        return game_time
    timestamp = frame.get("rfc460Timestamp") or frame.get("rfc3339Timestamp")
    if isinstance(timestamp, int):
        return timestamp
    if isinstance(timestamp, str) and timestamp.isdigit():
        return int(timestamp)
    if isinstance(timestamp, str):
        try:
            return int(datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            return 0
    return 0


def _duration_from_frames(window: dict[str, Any]) -> int | None:
    frames = window.get("frames", [])
    if len(frames) < 2:
        return None
    first = _frame_sort_time(frames[0])
    last = _frame_sort_time(frames[-1])
    if first and last and last > first:
        return last - first
    return None


def _vod_duration_ms(game: dict[str, Any]) -> int | None:
    for vod in game.get("vods", []):
        start = _to_int(vod.get("startMillis"))
        end = _to_int(vod.get("endMillis"))
        if start is not None and end is not None and end > start:
            return end - start
    return None
    return None


def _stats_by_participant(
    frame: dict[str, Any],
    details: dict[str, Any],
) -> dict[int, dict[str, Any]]:
    stats: dict[int, dict[str, Any]] = {}
    for team_key in ("blueTeam", "redTeam"):
        for participant in frame.get(team_key, {}).get("participants", []):
            participant_id = _to_int(participant.get("participantId"))
            if participant_id is not None:
                stats[participant_id] = participant

    for item in _walk_dicts(details):
        participant_id = _to_int(item.get("participantId"))
        if participant_id is None:
            nested_stats = item.get("stats")
            if isinstance(nested_stats, dict):
                participant_id = _to_int(nested_stats.get("participantId"))
        if participant_id is None or not 1 <= participant_id <= 10:
            continue

        merged = dict(stats.get(participant_id, {}))
        nested = item.get("stats")
        if isinstance(nested, dict):
            merged.update(nested)
        merged.update(item)
        stats[participant_id] = merged
    return stats


def _extract_bans(source: dict[str, Any]) -> list[str]:
    for key in ("bans", "bannedChampions", "bannedChampionIds", "ban"):
        value = source.get(key)
        bans = _champion_list(value)
        if bans:
            return bans
    return []


def _extract_team_bans(details: dict[str, Any], team_name: str, side_label: str) -> list[str]:
    candidates: list[str] = []
    lower_team = team_name.lower()
    side_en = "blue" if side_label == "블루" else "red"
    for item in _walk_dicts(details):
        item_text = " ".join(str(value).lower() for value in item.values() if isinstance(value, str))
        if lower_team not in item_text and side_en not in item_text:
            continue
        candidates.extend(_extract_bans(item))
    return _unique(candidates)


def _champion_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            if isinstance(item, dict):
                result.append(_champion_name(item))
            elif item:
                result.append(str(item))
        return [item for item in result if item and item != "-"]
    if isinstance(value, dict):
        champion = _champion_name(value)
        return [] if champion == "-" else [champion]
    return [str(value)]


def _winner_name(
    game: dict[str, Any],
    event: dict[str, Any],
    metadata: dict[str, Any],
    frame: dict[str, Any],
) -> str | None:
    for team in game.get("teams", []):
        result = team.get("result", {}) or {}
        if result.get("outcome") == "win":
            return str(team.get("code") or team.get("name"))

    winning_team_id = str(game.get("winningTeamId") or game.get("winnerId") or "")
    id_to_name = event_team_names(event)
    if winning_team_id and winning_team_id in id_to_name:
        return id_to_name[winning_team_id]

    for side, team_key in (("blueTeamMetadata", "blueTeam"), ("redTeamMetadata", "redTeam")):
        team = frame.get(team_key, {})
        if team.get("gameOver") and team.get("winner"):
            team_id = str(metadata.get(side, {}).get("esportsTeamId") or "")
            return id_to_name.get(team_id, team_id or None)
    return None


def _result_label(team_name: str, winner: str | None) -> str:
    if not winner:
        return "-"
    return "승" if team_name == winner else "패"


def _champion_name(participant: dict[str, Any]) -> str:
    champion = participant.get("champion")
    if isinstance(champion, dict):
        return str(champion.get("name") or champion.get("id") or "-")
    for key in (
        "championName",
        "championId",
        "champion",
        "name",
        "id",
    ):
        if participant.get(key):
            return str(participant[key])
    return "-"


def _player_name(participant: dict[str, Any]) -> str:
    return str(
        participant.get("summonerName")
        or participant.get("name")
        or participant.get("esportsPlayerId")
        or "-"
    )


def _damage(stats: dict[str, Any]) -> int | None:
    for key in (
        "totalDamageDealtToChampions",
        "totalDamageDealtChampions",
        "totalDamageDealtPlayer",
    ):
        value = _to_int(stats.get(key))
        if value is not None:
            return value

    parts = [
        _to_int(stats.get("physicalDamageDealtToChampions")),
        _to_int(stats.get("magicDamageDealtToChampions")),
        _to_int(stats.get("trueDamageDealtToChampions")),
    ]
    if any(value is not None for value in parts):
        return sum(value or 0 for value in parts)
    return None


def _stat_int(stats: dict[str, Any], key: str) -> int | None:
    return _to_int(stats.get(key))


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result
