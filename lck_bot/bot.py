from __future__ import annotations

import logging
from datetime import datetime, time
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
    render_pending_result_game,
    render_result_game,
    render_result_redirect_notice,
    render_result_summary,
    render_roster,
    render_upcoming_today,
)
from lck_bot.tracker import LiveTracker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
LOGGER = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

COOLDOWNS = {
    "경기상황": CooldownRule(user_seconds=30, guild_seconds=10),
    "로스터": CooldownRule(user_seconds=120, guild_seconds=30),
    "경기결과": CooldownRule(user_seconds=180, guild_seconds=30),
    "경기요약": CooldownRule(user_seconds=180, guild_seconds=30),
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

    async def setup_hook(self) -> None:
        await self.lolesports.start()
        register_commands(self)

        if self.settings.discord_guild_id:
            guild = discord.Object(id=self.settings.discord_guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            LOGGER.info("Synced %d guild commands.", len(synced))
        else:
            synced = await self.tree.sync()
            LOGGER.info("Synced %d global commands.", len(synced))

        self.tracker.start()

    async def close(self) -> None:
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
            await interaction.followup.send(embed=render_live_pending(live_events[0], upcoming))
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
        if game_state(game) == "unstarted" and not include_unstarted:
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
    events = await bot.lolesports.get_schedule_events()
    today = datetime.now(KST).date()
    return [event for event in events if _event_date(event) == today]


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
    bot.run(bot.settings.discord_token)


if __name__ == "__main__":
    main()
