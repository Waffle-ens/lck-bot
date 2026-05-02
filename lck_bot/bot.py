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
    render_live_status,
    render_no_today_games,
    render_live_pending,
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
        description="현재 LCK 세트의 세트 상황, 밴픽, 킬 타임라인, 골드 차이를 보여줍니다.",
    )
    async def live_status(interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        if await _send_cooldown_if_needed(bot, interaction, "경기상황"):
            return
        try:
            todays_events = await _todays_events(bot)
            live_events = [event for event in todays_events if _event_state(event) == "inprogress"]
            if live_events:
                snapshot = await bot.tracker.get_snapshot()
                if not snapshot:
                    upcoming = [event for event in todays_events if _event_state(event) == "unstarted"]
                    upcoming.sort(key=_event_sort_key)
                    await interaction.followup.send(embed=render_live_pending(live_events[0], upcoming))
                    return
                await interaction.followup.send(embed=render_live_status(snapshot))
                return

            upcoming = [event for event in todays_events if _event_state(event) == "unstarted"]
            if upcoming:
                upcoming.sort(key=_event_sort_key)
                await interaction.followup.send(embed=render_upcoming_today(upcoming))
                return

            completed = [event for event in todays_events if _event_state(event) == "completed"]
            if completed:
                completed.sort(key=_event_sort_key, reverse=True)
                event = await _event_details(bot, completed[0])
                reports = await _build_reports(bot, event)
                if not reports:
                    await interaction.followup.send("오늘 경기는 종료되었지만 세트별 상세 데이터를 가져오지 못했습니다.")
                    return
                await interaction.followup.send(embed=render_result_redirect_notice())
                await interaction.followup.send(embed=render_result_summary(event, reports))
                for report in reports:
                    await interaction.followup.send(embed=render_result_game(report))
                return

            next_event = await _next_event(bot)
            await interaction.followup.send(embed=render_no_today_games(next_event))
        except LolesportsError as exc:
            await interaction.followup.send(f"LoL Esports 데이터를 가져오지 못했습니다: `{exc}`")

    @bot.tree.command(
        name="경기결과",
        description="최근 완료된 LCK 경기의 세트별 진행시간, 픽, 선수 K/D/A를 보여줍니다.",
    )
    async def result(interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        if await _send_cooldown_if_needed(bot, interaction, "경기결과"):
            return
        try:
            selected = await bot.lolesports.find_recent_completed_event()
            if not selected:
                await interaction.followup.send("최근 완료된 LCK 경기 결과를 찾지 못했습니다.")
                return

            reports = await _build_reports(bot, selected.event)
            if not reports:
                await interaction.followup.send("경기 결과는 찾았지만 세트별 상세 데이터를 가져오지 못했습니다.")
                return

            await interaction.followup.send(embed=render_result_summary(selected.event, reports))
            for report in reports:
                await interaction.followup.send(embed=render_result_game(report))
        except LolesportsError as exc:
            await interaction.followup.send(f"LoL Esports 데이터를 가져오지 못했습니다: `{exc}`")


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


async def _build_reports(bot: LckDiscordBot, event: dict[str, Any]) -> list[GameReport]:
    reports: list[GameReport] = []
    games = event.get("match", {}).get("games", [])
    for index, game in enumerate(games, start=1):
        if game_state(game) == "unstarted":
            continue
        game_id = str(game.get("id") or "")
        if not game_id:
            continue

        starting_time = latest_starting_time()
        window = await _optional_window(bot, game_id, starting_time)
        details = await _optional_details(bot, game_id, starting_time)
        reports.append(build_game_report(event, game, window, details, fallback_number=index))
    return reports


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
