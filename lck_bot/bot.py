from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import discord
from discord import app_commands

from lck_bot.config import load_settings
from lck_bot.cooldown import CooldownManager, CooldownRule
from lck_bot.formatting import parse_time
from lck_bot.lolesports import LolesportsClient, LolesportsError, latest_starting_time
from lck_bot.match_data import GameReport, build_game_report, game_state
from lck_bot.presentation import (
    render_cooldown,
    render_command_help,
    render_live_pending,
    render_live_redirect_notice,
    render_live_status,
    render_match_not_found,
    render_match_selection_required,
    render_no_completed_today,
    render_no_summary_today,
    render_no_today_games,
    render_no_weekly_schedule,
    render_no_yesterday_games,
    render_pending_result_game,
    render_result_game,
    render_result_redirect_notice,
    render_result_summary,
    render_roster,
    render_upcoming_today,
    render_weekly_schedule,
    render_yesterday_game,
    render_yesterday_match_not_found,
    render_yesterday_match_selection_required,
)
from lck_bot.report_cache import YesterdayMatchCache, cached_match_payload
from lck_bot.tracker import LiveTracker

logging.raiseExceptions = False
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
LOGGER = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")
OBSERVABILITY_LOG_SECONDS = 10 * 60


class DiscordLogFloodFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = str(record.msg)
        if record.name == "discord.gateway" and "heartbeat blocked" in message:
            return False
        if record.name == "discord.client" and message.startswith("Attempting a reconnect in"):
            return False
        return True


for discord_logger_name in ("discord.client", "discord.gateway"):
    logging.getLogger(discord_logger_name).addFilter(DiscordLogFloodFilter())

COOLDOWNS = {
    "경기상황": CooldownRule(user_seconds=30, guild_seconds=10),
    "로스터": CooldownRule(user_seconds=120, guild_seconds=30),
    "경기결과": CooldownRule(user_seconds=180, guild_seconds=30),
    "경기요약": CooldownRule(user_seconds=180, guild_seconds=30),
    "어제경기": CooldownRule(user_seconds=180, guild_seconds=30),
    "경기일정": CooldownRule(user_seconds=60, guild_seconds=20),
    "명령어": CooldownRule(user_seconds=30, guild_seconds=10),
}


class LckDiscordBot(discord.Client):
    def __init__(self) -> None:
        self.settings = load_settings()
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.lolesports = LolesportsClient(
            api_key=self.settings.api_key,
            locale=self.settings.locale,
            league_id=self.settings.league_id,
        )
        self.tracker = LiveTracker(self.lolesports, self.settings.poll_seconds)
        self.cooldowns = CooldownManager()
        self.yesterday_cache = YesterdayMatchCache(self.settings.cache_dir)
        self._yesterday_cache_task: asyncio.Task[None] | None = None
        self._observability_task: asyncio.Task[None] | None = None

    async def setup_hook(self) -> None:
        await self.lolesports.start()
        register_commands(self)

        global_synced = await self.tree.sync()
        LOGGER.info("Synced %d global commands.", len(global_synced))

        if self.settings.discord_guild_id:
            guild = discord.Object(id=self.settings.discord_guild_id)
            self.tree.copy_global_to(guild=guild)
            guild_synced = await self.tree.sync(guild=guild)
            LOGGER.info("Synced %d guild commands.", len(guild_synced))

        self.tracker.start()
        self._yesterday_cache_task = asyncio.create_task(_yesterday_cache_worker(self))
        self._observability_task = asyncio.create_task(
            _observability_worker(self), name="lck-observability"
        )

    async def close(self) -> None:
        if self._yesterday_cache_task:
            self._yesterday_cache_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._yesterday_cache_task
        if self._observability_task:
            self._observability_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._observability_task
        await self.tracker.stop()
        await self.lolesports.close()
        await super().close()

    async def on_ready(self) -> None:
        LOGGER.info("Logged in as %s.", self.user)


def register_commands(bot: LckDiscordBot) -> None:
    @bot.tree.command(name="명령어", description="LCK Bot에서 사용할 수 있는 명령어를 안내합니다.")
    async def help_command(interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        if await _send_cooldown_if_needed(bot, interaction, "명령어"):
            return
        await interaction.followup.send(embed=render_command_help())

    @bot.tree.command(name="로스터", description="현재 LCK 세트의 출전 로스터와 챔피언 픽을 보여줍니다.")
    async def roster(interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        if await _send_cooldown_if_needed(bot, interaction, "로스터"):
            return
        try:
            selected = await bot.lolesports.find_current_event()
            if not selected or not selected.game:
                await interaction.followup.send("가져올 수 있는 LCK 경기 정보를 찾지 못했습니다.")
                return

            game_id = str(selected.game.get("id"))
            window = await bot.lolesports.get_live_window(game_id, latest_starting_time())
            await interaction.followup.send(embed=render_roster(selected.event, selected.game, window))
        except LolesportsError as exc:
            await interaction.followup.send(f"LoL Esports 데이터를 가져오지 못했습니다: `{exc}`")

    @bot.tree.command(
        name="경기상황",
        description="오늘 LCK 경기의 예정, 진행, 종료 상태에 맞춰 현재 상황을 보여줍니다.",
    )
    async def live_status(interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        if await _send_cooldown_if_needed(bot, interaction, "경기상황"):
            return
        try:
            await _send_today_status(bot, interaction)
        except LolesportsError as exc:
            await interaction.followup.send(f"LoL Esports 데이터를 가져오지 못했습니다: `{exc}`")

    @bot.tree.command(
        name="경기일정",
        description="이번 주 LCK 경기 일정을 날짜와 시간별로 보여줍니다.",
    )
    async def weekly_schedule(interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        if await _send_cooldown_if_needed(bot, interaction, "경기일정"):
            return
        try:
            week_start, week_end = _current_week_range()
            events = await _events_between(bot, week_start, week_end)
            events.sort(key=_event_sort_key)
            if not events:
                next_event = await _next_event(bot)
                await interaction.followup.send(embed=render_no_weekly_schedule(week_start, week_end, next_event))
                return
            await interaction.followup.send(embed=render_weekly_schedule(events, week_start, week_end))
        except LolesportsError as exc:
            await interaction.followup.send(f"LoL Esports 데이터를 가져오지 못했습니다: `{exc}`")

    @bot.tree.command(
        name="경기결과",
        description="오늘 완료된 LCK 경기의 세트별 스코어, 골드, 오브젝트, 픽을 보여줍니다.",
    )
    async def result(interaction: discord.Interaction, 매치: str | None = None) -> None:
        await interaction.response.defer(thinking=True)
        if await _send_cooldown_if_needed(bot, interaction, "경기결과"):
            return
        try:
            todays_events = await _todays_events(bot)
            todays_events.sort(key=_event_sort_key)
            if not todays_events:
                next_event = await _next_event(bot)
                await interaction.followup.send(embed=render_no_today_games(next_event))
                return

            if not 매치:
                await interaction.followup.send(embed=render_match_selection_required(todays_events))
                return

            selected_event = _find_event_by_match_name(todays_events, 매치)
            if not selected_event:
                await interaction.followup.send(embed=render_match_not_found(매치, todays_events))
                return

            event = await _event_details(bot, selected_event)
            reports = await _build_reports(bot, event, include_unstarted=True)
            if not reports:
                await interaction.followup.send("선택한 매치의 세트별 데이터를 가져오지 못했습니다.")
                return
            for report in reports:
                if report.state == "completed":
                    await interaction.followup.send(embed=render_result_game(report))
                else:
                    await interaction.followup.send(embed=render_pending_result_game(report))
        except LolesportsError as exc:
            await interaction.followup.send(f"LoL Esports 데이터를 가져오지 못했습니다: `{exc}`")

    @result.autocomplete("매치")
    async def result_match_autocomplete(
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        try:
            events = await _todays_events(bot)
        except LolesportsError:
            return []
        current_lower = current.lower()
        choices: list[app_commands.Choice[str]] = []
        for event in sorted(events, key=_event_sort_key):
            label = _match_choice_label(event)
            if current_lower and current_lower not in label.lower():
                continue
            choices.append(app_commands.Choice(name=label, value=_event_match_name(event)))
        return choices[:25]

    @bot.tree.command(
        name="어제경기",
        description="어제 진행된 LCK 경기의 세트별 결과와 선수별 K/D/A, 딜 비중을 보여줍니다.",
    )
    async def yesterday_result(interaction: discord.Interaction, 매치: str | None = None) -> None:
        await interaction.response.defer(thinking=True)
        if await _send_cooldown_if_needed(bot, interaction, "어제경기"):
            return
        try:
            yesterday_events = await _yesterdays_events(bot)
            yesterday_events.sort(key=_event_sort_key)
            if not yesterday_events:
                await interaction.followup.send(embed=render_no_yesterday_games())
                return

            if not 매치:
                await interaction.followup.send(embed=render_yesterday_match_selection_required(yesterday_events))
                return

            selected_event = _find_event_by_match_name(yesterday_events, 매치)
            if not selected_event:
                await interaction.followup.send(embed=render_yesterday_match_not_found(매치, yesterday_events))
                return

            target_date = _yesterday_date()
            cached_reports = bot.yesterday_cache.match_reports(target_date, _event_match_name(selected_event))
            if cached_reports:
                for report in cached_reports:
                    await interaction.followup.send(embed=render_yesterday_game(report))
                return

            event = await _event_details(bot, selected_event)
            reports = [
                report
                for report in await _build_reports(bot, event)
                if report.state == "completed"
            ]
            if not reports:
                await interaction.followup.send("선택한 어제 매치의 완료 세트 데이터를 가져오지 못했습니다.")
                return
            for report in reports:
                await interaction.followup.send(embed=render_yesterday_game(report))
        except LolesportsError as exc:
            await interaction.followup.send(f"LoL Esports 데이터를 가져오지 못했습니다: `{exc}`")

    @yesterday_result.autocomplete("매치")
    async def yesterday_match_autocomplete(
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        try:
            events = await _yesterdays_events(bot)
        except LolesportsError:
            return []
        current_lower = current.lower()
        choices: list[app_commands.Choice[str]] = []
        for event in sorted(events, key=_event_sort_key):
            label = _match_choice_label(event)
            if current_lower and current_lower not in label.lower():
                continue
            choices.append(app_commands.Choice(name=label, value=_event_match_name(event)))
        return choices[:25]

    @bot.tree.command(
        name="경기요약",
        description="오늘 완료된 LCK 경기의 매치 승패와 세트 요약을 보여줍니다.",
    )
    async def summary(interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        if await _send_cooldown_if_needed(bot, interaction, "경기요약"):
            return
        try:
            todays_events = await _todays_events(bot)
            completed = [event for event in todays_events if _event_state(event) == "completed"]
            if not completed:
                todays_events.sort(key=_event_sort_key)
                await interaction.followup.send(embed=render_no_summary_today(todays_events))
                return

            event, reports = await _latest_completed_reports(bot, completed)
            if not reports:
                await interaction.followup.send("오늘 완료된 경기는 찾았지만 요약 데이터를 가져오지 못했습니다.")
                return
            await interaction.followup.send(embed=render_result_summary(event, reports))
        except LolesportsError as exc:
            await interaction.followup.send(f"LoL Esports 데이터를 가져오지 못했습니다: `{exc}`")


async def _send_today_status(bot: LckDiscordBot, interaction: discord.Interaction) -> None:
    todays_events = await _todays_events(bot)
    live_events = [event for event in todays_events if _event_state(event) == "inprogress"]
    if live_events:
        snapshot = await bot.tracker.get_snapshot()
        if snapshot:
            await interaction.followup.send(embed=render_live_status(snapshot))
        else:
            upcoming = [event for event in todays_events if _event_state(event) == "unstarted"]
            upcoming.sort(key=_event_sort_key)
            completed_reports = await _completed_reports_for_event(bot, live_events[0])
            await interaction.followup.send(
                embed=render_live_pending(
                    live_events[0],
                    upcoming,
                    completed_reports,
                    match_decided=_match_is_decided(live_events[0], completed_reports),
                )
            )
        return

    upcoming = [event for event in todays_events if _event_state(event) == "unstarted"]
    if upcoming:
        upcoming.sort(key=_event_sort_key)
        await interaction.followup.send(embed=render_upcoming_today(upcoming))
        return

    completed = [event for event in todays_events if _event_state(event) == "completed"]
    if completed:
        event, reports = await _latest_completed_reports(bot, completed)
        if not reports:
            await interaction.followup.send("오늘 경기는 종료되었지만 세트별 상세 데이터를 가져오지 못했습니다.")
            return
        await interaction.followup.send(embed=render_result_redirect_notice())
        await interaction.followup.send(embed=render_result_summary(event, reports))
        return

    next_event = await _next_event(bot)
    await interaction.followup.send(embed=render_no_today_games(next_event))


async def _completed_reports_for_event(
    bot: LckDiscordBot,
    event_summary: dict[str, Any],
) -> list[GameReport]:
    try:
        event = await _event_details(bot, event_summary)
        return [
            report
            for report in await _build_reports(bot, event)
            if report.state == "completed"
        ]
    except LolesportsError as exc:
        LOGGER.info("Could not fetch completed set reports for live pending event: %s", exc)
        return []


def _match_is_decided(event: dict[str, Any], reports: list[GameReport]) -> bool:
    if not reports:
        return False

    required_wins = _required_match_wins(event, reports)
    for team in event.get("match", {}).get("teams", []):
        wins = _to_int((team.get("result") or {}).get("gameWins")) or 0
        if wins >= required_wins:
            return True

    wins: dict[str, int] = {}
    for report in reports:
        if not report.winner:
            continue
        wins[report.winner] = wins.get(report.winner, 0) + 1
    return any(count >= required_wins for count in wins.values())


def _required_match_wins(event: dict[str, Any], reports: list[GameReport]) -> int:
    if reports:
        return reports[0].max_sets // 2 + 1
    count = _to_int(event.get("match", {}).get("strategy", {}).get("count"))
    if count:
        return count // 2 + 1
    return 2


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def _observability_worker(bot: LckDiscordBot) -> None:
    while True:
        try:
            _log_runtime_stats(bot)
        except Exception:
            LOGGER.exception("Could not log runtime stats")
        await asyncio.sleep(OBSERVABILITY_LOG_SECONDS)


def _log_runtime_stats(bot: LckDiscordBot) -> None:
    stats: dict[str, int | float] = {}
    rss_mb = _process_rss_mb()
    if rss_mb is not None:
        stats["rss_mb"] = round(rss_mb, 1)
    stats.update(bot.lolesports.stats())
    stats.update(bot.cooldowns.stats())
    stats.update(bot.tracker.stats())
    LOGGER.info("Runtime stats: %s", " ".join(f"{key}={value}" for key, value in stats.items()))


def _process_rss_mb() -> float | None:
    try:
        with open("/proc/self/status", encoding="utf-8") as handle:
            for line in handle:
                if not line.startswith("VmRSS:"):
                    continue
                parts = line.split()
                if len(parts) >= 2:
                    return int(parts[1]) / 1024
    except OSError:
        return None
    return None


async def _yesterday_cache_worker(bot: LckDiscordBot) -> None:
    while True:
        now = datetime.now(KST)
        ready_at = datetime.combine(now.date(), time(hour=0, minute=10), tzinfo=KST)
        if now < ready_at:
            await asyncio.sleep((ready_at - now).total_seconds())
            continue

        target_date = now.date() - timedelta(days=1)
        if bot.yesterday_cache.is_complete(target_date):
            await asyncio.sleep(_seconds_until_next_cache_window())
            continue

        try:
            complete = await _refresh_yesterday_cache(bot, target_date)
        except LolesportsError as exc:
            LOGGER.info("Could not refresh yesterday cache for %s: %s", target_date, exc)
            complete = False

        await asyncio.sleep(_seconds_until_next_cache_window() if complete else 15 * 60)


async def _refresh_yesterday_cache(bot: LckDiscordBot, target_date: date) -> bool:
    events = await _events_on_date(bot, target_date)
    events.sort(key=_event_sort_key)
    if not events:
        bot.yesterday_cache.save_day(target_date, [], complete=True)
        LOGGER.info("Cached empty yesterday match list for %s.", target_date)
        return True

    matches: list[dict[str, Any]] = []
    reasons: list[str] = []
    for event_summary in events:
        event = await _event_details(bot, event_summary)
        reports = [
            report
            for report in await _build_reports(bot, event)
            if report.state == "completed"
        ]
        match_name = _event_match_name(event)
        reasons.extend(_report_missing_reasons(match_name, reports))
        matches.append(
            cached_match_payload(
                match_name=match_name,
                start_time=event.get("startTime"),
                state=_event_state(event),
                reports=reports,
            )
        )

    complete = not reasons
    bot.yesterday_cache.save_day(target_date, matches, complete=complete, reasons=reasons)
    if complete:
        LOGGER.info("Cached yesterday matches for %s.", target_date)
    else:
        LOGGER.info("Yesterday cache for %s is incomplete: %s", target_date, "; ".join(reasons[:5]))
    return complete


def _report_missing_reasons(match_name: str, reports: list[GameReport]) -> list[str]:
    if not reports:
        return [f"{match_name}: completed set data is missing"]

    reasons: list[str] = []
    for report in reports:
        prefix = f"{match_name} {report.set_number}세트"
        if report.blue_kills is None or report.red_kills is None:
            reasons.append(f"{prefix}: team kills are missing")
        if report.blue_gold is None or report.red_gold is None:
            reasons.append(f"{prefix}: team gold is missing")
        if len(report.draft) < 2 or any(not draft.picks for draft in report.draft):
            reasons.append(f"{prefix}: champion picks are missing")
        if len(report.players) < 10:
            reasons.append(f"{prefix}: player rows are missing")
            continue
        for player in report.players:
            if player.kills is None or player.deaths is None or player.assists is None:
                reasons.append(f"{prefix}: {player.player} K/D/A is missing")
            if player.damage_share is None:
                reasons.append(f"{prefix}: {player.player} damage share is missing")
    return reasons


def _seconds_until_next_cache_window() -> float:
    now = datetime.now(KST)
    next_ready = datetime.combine(now.date() + timedelta(days=1), time(hour=0, minute=10), tzinfo=KST)
    return max(60.0, (next_ready - now).total_seconds())


async def _send_cooldown_if_needed(
    bot: LckDiscordBot,
    interaction: discord.Interaction,
    command_name: str,
) -> bool:
    hit = bot.cooldowns.check(
        command_name=command_name,
        user_id=interaction.user.id,
        guild_id=interaction.guild_id,
        rule=COOLDOWNS[command_name],
    )
    if not hit:
        return False
    await interaction.followup.send(embed=render_cooldown(command_name, hit), ephemeral=True)
    return True


async def _latest_completed_reports(
    bot: LckDiscordBot,
    completed_events: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[GameReport]]:
    completed_events.sort(key=_event_sort_key, reverse=True)
    event = await _event_details(bot, completed_events[0])
    return event, await _build_reports(bot, event)


async def _build_reports(
    bot: LckDiscordBot,
    event: dict[str, Any],
    include_unstarted: bool = False,
) -> list[GameReport]:
    reports: list[GameReport] = []
    games = event.get("match", {}).get("games", [])
    for index, game in enumerate(games, start=1):
        state = game_state(game)
        if state == "unneeded":
            continue
        if state == "unstarted" and not include_unstarted:
            continue
        game_id = str(game.get("id") or "")
        if not game_id:
            continue

        starting_time = latest_starting_time()
        window = await _optional_window(bot, game_id, starting_time)
        details = await _optional_details(bot, game_id, starting_time)
        reports.append(build_game_report(event, game, window, details, fallback_number=index))
    return reports


def _find_event_by_match_name(events: list[dict[str, Any]], match_name: str) -> dict[str, Any] | None:
    normalized = match_name.strip().lower()
    for event in events:
        if _event_match_name(event).lower() == normalized or _match_choice_label(event).lower() == normalized:
            return event
    return None


def _event_match_name(event: dict[str, Any]) -> str:
    teams = event.get("match", {}).get("teams", [])
    if len(teams) >= 2:
        return f"{teams[0].get('code') or teams[0].get('name')} vs {teams[1].get('code') or teams[1].get('name')}"
    return event.get("blockName") or "LCK 경기"


def _match_choice_label(event: dict[str, Any]) -> str:
    event_time = _event_datetime(event)
    time_text = event_time.strftime("%H:%M") if event_time else "시간 미정"
    return f"{time_text} {_event_match_name(event)}"


async def _todays_events(bot: LckDiscordBot) -> list[dict[str, Any]]:
    return await _events_on_date(bot, datetime.now(KST).date())


async def _yesterdays_events(bot: LckDiscordBot) -> list[dict[str, Any]]:
    return await _events_on_date(bot, _yesterday_date())


async def _events_on_date(bot: LckDiscordBot, target_date: date) -> list[dict[str, Any]]:
    events = await bot.lolesports.get_schedule_events()
    return [event for event in events if _event_date(event) == target_date]


async def _events_between(
    bot: LckDiscordBot,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    events = await bot.lolesports.get_schedule_events()
    return [
        event
        for event in events
        if (event_time := _event_datetime(event)) and start <= event_time < end
    ]


def _current_week_range() -> tuple[datetime, datetime]:
    today = datetime.now(KST).date()
    week_start_date = today - timedelta(days=today.weekday())
    week_start = datetime.combine(week_start_date, time.min, tzinfo=KST)
    return week_start, week_start + timedelta(days=7)


def _yesterday_date() -> date:
    return datetime.now(KST).date() - timedelta(days=1)


async def _next_event(bot: LckDiscordBot) -> dict[str, Any] | None:
    now = datetime.now(KST)
    future = [
        event
        for event in await bot.lolesports.get_schedule_events()
        if (_event_datetime(event) and _event_datetime(event) >= now)
    ]
    future.sort(key=_event_sort_key)
    return future[0] if future else None


async def _event_details(bot: LckDiscordBot, event: dict[str, Any]) -> dict[str, Any]:
    details = await bot.lolesports.get_event_details(bot.lolesports.event_id(event))
    return details.get("data", {}).get("event", event)


def _event_state(event: dict[str, Any]) -> str:
    return str(event.get("state", "")).replace("-", "_").lower()


def _event_date(event: dict[str, Any]):
    event_datetime = _event_datetime(event)
    return event_datetime.date() if event_datetime else None


def _event_datetime(event: dict[str, Any]) -> datetime | None:
    parsed = parse_time(event.get("startTime"))
    if not parsed:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    return parsed.astimezone(KST)


def _event_sort_key(event: dict[str, Any]) -> datetime:
    return _event_datetime(event) or datetime.combine(datetime.max.date(), time.max, tzinfo=KST)


async def _optional_window(
    bot: LckDiscordBot, game_id: str, starting_time: str | None = None
) -> dict[str, Any] | None:
    try:
        return await bot.lolesports.get_live_window(game_id, starting_time)
    except LolesportsError as exc:
        LOGGER.info("Could not fetch window for game %s: %s", game_id, exc)
        return None


async def _optional_details(
    bot: LckDiscordBot, game_id: str, starting_time: str | None = None
) -> dict[str, Any] | None:
    try:
        return await bot.lolesports.get_live_details(game_id, starting_time)
    except LolesportsError as exc:
        LOGGER.info("Could not fetch details for game %s: %s", game_id, exc)
        return None


def main() -> None:
    bot = LckDiscordBot()
    bot.run(bot.settings.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
