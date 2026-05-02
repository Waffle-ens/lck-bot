from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

ROLE_LABELS = {
    "top": "탑",
    "jungle": "정글",
    "mid": "미드",
    "middle": "미드",
    "bottom": "원딜",
    "bot": "원딜",
    "adc": "원딜",
    "support": "서폿",
    "utility": "서폿",
}

ROLE_BY_PARTICIPANT = {
    1: "탑",
    2: "정글",
    3: "미드",
    4: "원딜",
    5: "서폿",
    6: "탑",
    7: "정글",
    8: "미드",
    9: "원딜",
    10: "서폿",
}


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def format_kst(value: str | None) -> str:
    dt = parse_time(value)
    if not dt:
        return "시간 미정"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(KST).strftime("%Y-%m-%d %H:%M")


def format_game_clock(milliseconds: int | None) -> str:
    if milliseconds is None:
        return "시간 미제공"
    seconds = max(0, milliseconds // 1000)
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def format_gold(value: int | None) -> str:
    if value is None:
        return "?"
    return f"{value / 1000:.1f}k"


def normalize_role(role: str | None, participant_id: int | None = None) -> str:
    if role:
        mapped = ROLE_LABELS.get(role.lower())
        if mapped:
            return mapped
    if participant_id is not None:
        return ROLE_BY_PARTICIPANT.get(participant_id, "?")
    return "?"


def table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "표시할 데이터가 없습니다."

    widths = [
        max(len(str(row[index])) for row in [headers, *rows])
        for index in range(len(headers))
    ]

    def render_row(row: list[str]) -> str:
        return " | ".join(str(value).ljust(widths[index]) for index, value in enumerate(row))

    divider = "-+-".join("-" * width for width in widths)
    return "\n".join([render_row(headers), divider, *(render_row(row) for row in rows)])


def truncate_discord(text: str, limit: int = 3900) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 20].rstrip() + "\n...생략됨"
