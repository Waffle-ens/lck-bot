from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import discord

from lck_bot.cooldown import CooldownHit, format_remaining
from lck_bot.formatting import format_game_clock, format_gold, format_kst
from lck_bot.match_data import (
    GameReport,
    build_game_report,
    match_title,
    max_sets,
    state_label,
)
from lck_bot.tracker import GameSnapshot

BLUE = discord.Color.from_rgb(57, 102, 255)
GREEN = discord.Color.from_rgb(35, 150, 95)
GOLD = discord.Color.from_rgb(218, 165, 32)
GRAY = discord.Color.from_rgb(116, 124, 140)


def render_roster(event: dict[str, Any], game: dict[str, Any], window: dict[str, Any]) -> discord.Embed:
    report = build_game_report(event, game, window)
    embed = discord.Embed(
        title=f"{match_title(event)}",
        description=f"{report.set_number}세트 / Bo{report.max_sets} 출전 로스터",
        color=BLUE,
    )

    for team in _teams(report):
        lines = [
            f"`{player.role}` {player.champion} - {player.player}"
            for player in report.players
            if player.team == team
        ]
        embed.add_field(name=team, value=_value(lines), inline=True)

    return embed


def render_live_status(snapshot: GameSnapshot) -> discord.Embed:
    diff = snapshot.gold_diff
    if diff > 0:
        gold_line = f"{snapshot.blue_name} +{format_gold(diff)}"
    elif diff < 0:
        gold_line = f"{snapshot.red_name} +{format_gold(abs(diff))}"
    else:
        gold_line = "동률"

    embed = discord.Embed(
        title=snapshot.event_name,
        description=f"{snapshot.set_number}세트 / Bo{snapshot.max_sets} | {_live_state_label(snapshot.state)}",
        color=GREEN,
    )
    embed.add_field(
        name="스코어",
        value=(
            f"{snapshot.blue_name} {snapshot.blue_kills}킬\n"
            f"{snapshot.red_name} {snapshot.red_kills}킬"
        ),
        inline=True,
    )
    embed.add_field(
        name="골드",
        value=(
            f"{snapshot.blue_name} {format_gold(snapshot.blue_gold)}\n"
            f"{snapshot.red_name} {format_gold(snapshot.red_gold)}\n"
            f"차이: {gold_line}"
        ),
        inline=True,
    )
    embed.add_field(
        name="오브젝트",
        value=(
            f"{snapshot.blue_name}: 바론 {snapshot.blue_barons}, 드래곤 {_dragon_summary(snapshot.blue_dragons)}\n"
            f"{snapshot.red_name}: 바론 {snapshot.red_barons}, 드래곤 {_dragon_summary(snapshot.red_dragons)}"
        ),
        inline=False,
    )

    for draft in snapshot.draft:
        picks = ", ".join(draft.picks) if draft.picks else "데이터 없음"
        embed.add_field(name=f"{draft.side} {draft.team} 픽", value=_clip(picks), inline=False)

    return embed


def render_upcoming_today(events: list[dict[str, Any]]) -> discord.Embed:
    embed = discord.Embed(
        title="오늘의 LCK 일정",
        description="오늘 예정된 LCK 경기가 아직 시작 전입니다.",
        color=GOLD,
    )
    lines = [f"`{_event_time(event)}` {_event_title(event)}" for event in events]
    embed.add_field(name="예정 경기", value=_value(lines), inline=False)
    return embed


def render_no_today_games(next_event: dict[str, Any] | None) -> discord.Embed:
    embed = discord.Embed(
        title="오늘 예정된 LCK 경기가 없습니다",
        color=GRAY,
    )
    if next_event:
        embed.add_field(
            name="다음 경기",
            value=f"`{_event_time(next_event, include_date=True)}` {_event_title(next_event)}",
            inline=False,
        )
    return embed


def render_weekly_schedule(
    events: list[dict[str, Any]],
    week_start: datetime,
    week_end: datetime,
) -> discord.Embed:
    embed = discord.Embed(
        title="이번 주 LCK 경기 일정",
        description=f"{week_start.strftime('%Y-%m-%d')} ~ {(week_end - timedelta(days=1)).strftime('%Y-%m-%d')}",
        color=BLUE,
    )

    grouped: dict[str, list[str]] = {}
    for event in events:
        parsed = _parse_event_time(event)
        if not parsed:
            label = "날짜 미정"
            time_text = "시간 미정"
        else:
            label = _date_label(parsed)
            time_text = parsed.strftime("%H:%M")
        grouped.setdefault(label, []).append(
            f"`{time_text}` {_event_title(event)} - {_event_state_label(event.get('state'))}"
        )

    for label, lines in grouped.items():
        embed.add_field(name=label, value=_value(lines), inline=False)
    return embed


def render_no_weekly_schedule(
    week_start: datetime,
    week_end: datetime,
    next_event: dict[str, Any] | None,
) -> discord.Embed:
    embed = discord.Embed(
        title="이번 주 예정된 LCK 경기가 없습니다",
        description=f"{week_start.strftime('%Y-%m-%d')} ~ {(week_end - timedelta(days=1)).strftime('%Y-%m-%d')}",
        color=GRAY,
    )
    if next_event:
        embed.add_field(
            name="다음 경기",
            value=f"`{_event_time(next_event, include_date=True)}` {_event_title(next_event)}",
            inline=False,
        )
    return embed


def render_no_completed_today(events: list[dict[str, Any]]) -> discord.Embed:
    embed = discord.Embed(
        title="오늘 완료된 LCK 경기가 없습니다",
        description="오늘 경기가 아직 진행 중이거나 시작 전입니다.",
        color=GOLD,
    )
    if events:
        lines = [
            f"`{_event_time(event)}` {_event_title(event)} - {_event_state_label(event.get('state'))}"
            for event in events
        ]
        embed.add_field(name="오늘 경기", value=_value(lines), inline=False)
    return embed


def render_match_selection_required(events: list[dict[str, Any]]) -> discord.Embed:
    embed = discord.Embed(
        title="결과를 볼 매치를 선택해주세요",
        description="`/경기결과` 명령어의 `매치` 옵션에서 오늘 경기 중 하나를 선택하면 됩니다.",
        color=GOLD,
    )
    lines = [
        f"`{_event_time(event)}` {_event_title(event)} - {_event_state_label(event.get('state'))}"
        for event in events
    ]
    embed.add_field(name="오늘 경기", value=_value(lines), inline=False)
    return embed


def render_match_not_found(match_name: str, events: list[dict[str, Any]]) -> discord.Embed:
    embed = discord.Embed(
        title="매치를 찾지 못했습니다",
        description=f"`{match_name}`와 일치하는 오늘 LCK 경기를 찾지 못했습니다.",
        color=GOLD,
    )
    if events:
        lines = [f"`{_event_time(event)}` {_event_title(event)}" for event in events]
        embed.add_field(name="선택 가능한 매치", value=_value(lines), inline=False)
    return embed


def render_yesterday_match_selection_required(events: list[dict[str, Any]]) -> discord.Embed:
    embed = discord.Embed(
        title="어제 경기 결과를 볼 매치를 선택해주세요",
        description="`/어제경기` 명령어의 `매치` 옵션에서 어제 경기 중 하나를 선택하면 됩니다.",
        color=GOLD,
    )
    lines = [
        f"`{_event_time(event)}` {_event_title(event)} - {_event_state_label(event.get('state'))}"
        for event in events
    ]
    embed.add_field(name="어제 경기", value=_value(lines), inline=False)
    return embed


def render_yesterday_match_not_found(match_name: str, events: list[dict[str, Any]]) -> discord.Embed:
    embed = discord.Embed(
        title="매치를 찾지 못했습니다",
        description=f"`{match_name}`와 일치하는 어제 LCK 경기를 찾지 못했습니다.",
        color=GOLD,
    )
    if events:
        lines = [f"`{_event_time(event)}` {_event_title(event)}" for event in events]
        embed.add_field(name="선택 가능한 매치", value=_value(lines), inline=False)
    return embed


def render_no_yesterday_games() -> discord.Embed:
    return discord.Embed(
        title="어제 진행된 LCK 경기가 없습니다",
        description="어제 날짜 기준으로 조회 가능한 LCK 경기를 찾지 못했습니다.",
        color=GRAY,
    )


def render_no_summary_today(events: list[dict[str, Any]]) -> discord.Embed:
    embed = discord.Embed(
        title="요약할 완료 경기가 없습니다",
        description="오늘 완료된 LCK 경기가 생기면 매치 요약을 보여드립니다.",
        color=GOLD,
    )
    if events:
        lines = [
            f"`{_event_time(event)}` {_event_title(event)} - {_event_state_label(event.get('state'))}"
            for event in events
        ]
        embed.add_field(name="오늘 경기", value=_value(lines), inline=False)
    return embed


def render_result_redirect_notice() -> discord.Embed:
    return discord.Embed(
        title="오늘 경기가 모두 종료되었습니다",
        description="혼동을 줄이기 위해 `/경기결과` 내용을 대신 출력합니다.",
        color=GRAY,
    )


def render_live_redirect_notice() -> discord.Embed:
    return discord.Embed(
        title="아직 경기가 진행 중입니다",
        description="결과가 확정되지 않아 `/경기상황` 내용을 대신 출력합니다.",
        color=GREEN,
    )


def render_live_pending(
    event: dict[str, Any],
    upcoming: list[dict[str, Any]],
    completed_reports: list[GameReport] | None = None,
    match_decided: bool = False,
) -> discord.Embed:
    title = "다음 매치 대기 중" if match_decided else "다음 세트 준비 중"
    description = (
        f"{_event_title(event)} 매치가 종료되었고 다음 매치를 기다리는 중입니다."
        if match_decided
        else f"{_event_title(event)} 경기가 곧 시작될 예정입니다."
    )
    embed = discord.Embed(
        title=title,
        description=description,
        color=GOLD,
    )
    if completed_reports:
        latest = max(completed_reports, key=lambda report: report.set_number)
        lines = [f"{latest.set_number}세트 | {state_label(latest.state)}"]
        if latest.winner and latest.loser:
            lines.append(f"승패: {latest.winner} 승 / {latest.loser} 패")
        lines.append(
            f"스코어: {latest.blue_name} {_number_or_dash(latest.blue_kills)}킬 / "
            f"{latest.red_name} {_number_or_dash(latest.red_kills)}킬"
        )
        lines.append(
            f"골드: {latest.blue_name} {_gold_or_dash(latest.blue_gold)} / "
            f"{latest.red_name} {_gold_or_dash(latest.red_gold)}"
        )
        lines.append(f"골드차: {_gold_diff(latest)}")
        embed.add_field(name="최근 세트 결과", value=_value(lines), inline=False)
    if upcoming:
        lines = [f"`{_event_time(item)}` {_event_title(item)}" for item in upcoming]
        field_name = "다음 매치" if match_decided else "오늘 남은 경기"
        embed.add_field(name=field_name, value=_value(lines), inline=False)
    return embed


def render_cooldown(command_name: str, hit: CooldownHit) -> discord.Embed:
    if hit.scope == "user":
        title = "잠시만요"
        description = f"`/{command_name}` 명령어는 사용자당 {format_remaining(hit.limit_seconds)}에 한 번 사용할 수 있습니다."
    else:
        title = "서버 요청이 잠시 몰렸습니다"
        description = f"이 서버에서 `/{command_name}` 명령어가 방금 호출되었습니다."

    embed = discord.Embed(
        title=title,
        description=description,
        color=GOLD,
    )
    embed.add_field(name="남은 시간", value=format_remaining(hit.remaining_seconds), inline=True)
    embed.set_footer(text="API 안정성을 위해 호출 간격을 제한하고 있습니다.")
    return embed


def render_command_help() -> discord.Embed:
    embed = discord.Embed(
        title="LCK Bot 명령어 안내",
        description="오늘의 LCK 경기 일정, 진행 상황, 결과를 Discord에서 바로 확인할 수 있습니다.",
        color=BLUE,
    )
    embed.add_field(
        name="/경기상황",
        value=(
            "오늘 경기 상태에 맞춰 현재 상황을 보여줍니다.\n"
            "진행 중이면 스코어, 골드, 오브젝트, 픽을 표시합니다."
        ),
        inline=False,
    )
    embed.add_field(
        name="/경기일정",
        value="이번 주 LCK 경기 일정을 날짜와 시간별로 보여줍니다.",
        inline=False,
    )
    embed.add_field(
        name="/로스터",
        value="현재 세트의 출전 로스터와 챔피언 픽을 팀별로 보여줍니다.",
        inline=False,
    )
    embed.add_field(
        name="/경기결과",
        value="오늘 매치를 선택해 세트별 스코어, 골드, 오브젝트, 픽을 보여줍니다.",
        inline=False,
    )
    embed.add_field(
        name="/경기요약",
        value="오늘 완료된 경기의 매치 승패와 세트별 진행시간을 요약합니다.",
        inline=False,
    )
    embed.add_field(
        name="/어제경기",
        value="어제 매치를 선택해 세트별 결과와 선수별 K/D/A, 딜 비중을 보여줍니다.",
        inline=False,
    )
    embed.add_field(
        name="/명령어",
        value="이 안내 메시지를 보여줍니다.",
        inline=False,
    )
    return embed


def render_result_summary(event: dict[str, Any], reports: list[GameReport]) -> discord.Embed:
    match = event.get("match", {})
    teams = match.get("teams", [])
    embed = discord.Embed(
        title=match_title(event),
        description=(
            f"시작: {format_kst(event.get('startTime'))}\n"
            f"진행 세트: {len([report for report in reports if report.state != 'unstarted'])} / Bo{max_sets(event)}"
        ),
        color=BLUE,
    )

    score_lines = []
    for team in teams:
        result = team.get("result", {}) or {}
        outcome = result.get("outcome")
        outcome_text = "승" if outcome == "win" else "패" if outcome == "loss" else "-"
        score_lines.append(f"{team.get('code') or team.get('name') or '-'}: {result.get('gameWins', 0)}세트승 ({outcome_text})")
    if score_lines:
        embed.add_field(name="매치 결과", value=_value(score_lines), inline=False)

    set_lines = [
        f"{report.set_number}세트: {state_label(report.state)} | {format_game_clock(report.duration_ms)}"
        for report in reports
    ]
    embed.add_field(name="세트 요약", value=_value(set_lines), inline=False)
    return embed


def render_result_game(report: GameReport) -> discord.Embed:
    embed = discord.Embed(
        title=f"{report.set_number}세트 결과",
        description=f"상태: {state_label(report.state)} | 시간: {format_game_clock(report.duration_ms)}",
        color=BLUE,
    )
    if report.winner and report.loser:
        embed.add_field(name="승패", value=f"{report.winner} 승 / {report.loser} 패", inline=False)
    embed.add_field(
        name="스코어",
        value=(
            f"{report.blue_name} {_number_or_dash(report.blue_kills)}킬\n"
            f"{report.red_name} {_number_or_dash(report.red_kills)}킬"
        ),
        inline=True,
    )
    embed.add_field(
        name="골드",
        value=(
            f"{report.blue_name} {_gold_or_dash(report.blue_gold)}\n"
            f"{report.red_name} {_gold_or_dash(report.red_gold)}\n"
            f"차이: {_gold_diff(report)}"
        ),
        inline=True,
    )
    embed.add_field(
        name="오브젝트",
        value=(
            f"{report.blue_name}: 바론 {report.blue_barons}, 드래곤 {_dragon_summary(report.blue_dragons)}\n"
            f"{report.red_name}: 바론 {report.red_barons}, 드래곤 {_dragon_summary(report.red_dragons)}"
        ),
        inline=False,
    )

    for draft in report.draft:
        picks = ", ".join(draft.picks) if draft.picks else "데이터 없음"
        embed.add_field(name=f"{draft.side} {draft.team} 픽", value=_clip(picks), inline=False)

    return embed


def render_yesterday_game(report: GameReport) -> discord.Embed:
    embed = render_result_game(report)
    embed.title = f"{report.set_number}세트 어제 경기 결과"

    for team in _teams(report):
        lines = [
            (
                f"`{player.role}` {player.player} ({player.champion}) "
                f"{_kda(player.kills, player.deaths, player.assists)} | 딜 비중 {_damage_share_or_dash(player.damage_share)}"
            )
            for player in report.players
            if player.team == team
        ]
        embed.add_field(name=f"{team} 선수 기록", value=_value(lines), inline=False)

    return embed


def render_pending_result_game(report: GameReport) -> discord.Embed:
    if report.state == "unstarted":
        description = "아직 진행 전입니다."
        color = GRAY
    else:
        description = "경기 진행중 또는 다음 세트 준비 중입니다."
        color = GOLD
    return discord.Embed(
        title=f"{report.set_number}세트",
        description=description,
        color=color,
    )


def _teams(report: GameReport) -> list[str]:
    teams: list[str] = []
    for player in report.players:
        if player.team not in teams:
            teams.append(player.team)
    return teams


def _kda(kills: int | None, deaths: int | None, assists: int | None) -> str:
    if kills is None and deaths is None and assists is None:
        return "-"
    return f"{kills or 0}/{deaths or 0}/{assists or 0}"


def _damage_share_or_dash(value: float | None) -> str:
    if value is None:
        return "-"
    percentage = value * 100 if value <= 1 else value
    text = f"{percentage:.1f}".rstrip("0").rstrip(".")
    return f"{text}%"


def _number_or_dash(value: int | None) -> str:
    return "-" if value is None else str(value)


def _gold_or_dash(value: int | None) -> str:
    return "-" if value is None else format_gold(value)


def _gold_diff(report: GameReport) -> str:
    if report.blue_gold is None or report.red_gold is None:
        return "-"
    diff = report.blue_gold - report.red_gold
    if diff > 0:
        return f"{report.blue_name} +{format_gold(diff)}"
    if diff < 0:
        return f"{report.red_name} +{format_gold(abs(diff))}"
    return "동률"


def _dragon_summary(dragons: list[str]) -> str:
    if not dragons:
        return "0"
    translated = [_dragon_name(dragon) for dragon in dragons]
    return f"{len(dragons)} ({', '.join(translated)})"


def _dragon_name(value: str) -> str:
    names = {
        "cloud": "바람",
        "chemtech": "화공",
        "hextech": "마법공학",
        "infernal": "화염",
        "mountain": "대지",
        "ocean": "바다",
        "elder": "장로",
    }
    return names.get(value.lower(), value)


def _event_title(event: dict[str, Any]) -> str:
    teams = event.get("match", {}).get("teams", [])
    if len(teams) >= 2:
        return f"{teams[0].get('code') or teams[0].get('name')} vs {teams[1].get('code') or teams[1].get('name')}"
    return event.get("blockName") or "LCK 경기"


def _event_time(event: dict[str, Any], include_date: bool = False) -> str:
    parsed = _parse_event_time(event)
    if not parsed:
        return "시간 미정"
    pattern = "%Y-%m-%d %H:%M" if include_date else "%H:%M"
    return parsed.strftime(pattern)


def _date_label(value: datetime) -> str:
    weekdays = ["월", "화", "수", "목", "금", "토", "일"]
    return f"{value.month}월 {value.day}일 ({weekdays[value.weekday()]})"


def _parse_event_time(event: dict[str, Any]) -> datetime | None:
    formatted = format_kst(event.get("startTime"))
    if formatted == "시간 미정":
        return None
    try:
        return datetime.strptime(formatted, "%Y-%m-%d %H:%M")
    except ValueError:
        return None


def _live_state_label(value: str) -> str:
    state = value.replace("-", "_").lower()
    if state in {"inprogress", "in_progress"}:
        return "진행중"
    if state == "completed":
        return "종료(다음 세트 대기중)"
    if state == "unstarted":
        return "예정"
    return state_label(value)


def _event_state_label(value: Any) -> str:
    state = str(value or "").replace("-", "_").lower()
    if state in {"inprogress", "in_progress"}:
        return "진행중"
    if state == "completed":
        return "종료"
    if state == "unstarted":
        return "예정"
    return state_label(value)


def _value(lines: list[str]) -> str:
    return _clip("\n".join(lines) if lines else "데이터 없음")


def _clip(value: str, limit: int = 1024) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 12].rstrip() + "\n...생략"
