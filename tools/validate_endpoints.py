from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from html import escape
from pathlib import Path
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

LOCALE = "ko-KR"
LCK_LEAGUE_ID = "98767991310872058"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lck_bot.match_data import build_game_report, game_state, match_title  # noqa: E402

API_KEY = "0TvQnueqKa5mxJntVWt0w4LpLfEkrV1Ta8rQBb9Z"
OUT_DIR = ROOT / "reports"
OUT_JSON = OUT_DIR / "endpoint-validation.json"
OUT_HTML = OUT_DIR / "endpoint-validation.html"
KST = ZoneInfo("Asia/Seoul")


@dataclass
class CheckResult:
    name: str
    url: str
    ok: bool
    status: int | None
    message: str
    fields: dict[str, Any]


def main() -> None:
    headers = {"x-api-key": API_KEY, "User-Agent": "lck-bot-endpoint-validator/0.1"}
    checks: list[CheckResult] = []
    latest = latest_starting_time()
    schedule_url = "https://esports-api.lolesports.com/persisted/gw/getSchedule"
    schedule = fetch(
        headers, "getSchedule", schedule_url, {"hl": LOCALE, "leagueId": LCK_LEAGUE_ID}, checks
    )

    event = pick_event(schedule)
    if event:
        event_id = str(event.get("id") or event.get("match", {}).get("id"))
        details_url = "https://esports-api.lolesports.com/persisted/gw/getEventDetails"
        event_details = fetch(headers, "getEventDetails", details_url, {"hl": LOCALE, "id": event_id}, checks)
        event = event_details.get("data", {}).get("event", event)
        games = event.get("match", {}).get("games", [])
        game = pick_game(games)
        if game and game.get("id"):
            game_id = str(game["id"])
            window_url = f"https://feed.lolesports.com/livestats/v1/window/{game_id}"
            window = fetch(headers, "livestats/window", window_url, {"startingTime": latest}, checks)
            details_feed_url = f"https://feed.lolesports.com/livestats/v1/details/{game_id}"
            live_details = fetch(
                headers, "livestats/details", details_feed_url, {"startingTime": latest}, checks
            )
            if window.get("frames"):
                add_report_checks(checks, "parser/current-or-live", event, game, window, live_details)
            else:
                checks.append(
                    CheckResult(
                        "parser/current-or-live",
                        "-",
                        True,
                        None,
                        "현재 진행 중으로 표시된 세트의 live feed가 비어 있어 곧 경기 시작 안내 대상입니다.",
                        {
                            "match": match_title(event),
                            "gameId": game_id,
                            "set": game.get("number"),
                            "state": game.get("state"),
                            "fallback": "live-pending",
                        },
                    )
                )
        else:
            checks.append(
                CheckResult(
                    "game selection",
                    "-",
                    False,
                    None,
                    "선택한 이벤트에 game id가 없습니다.",
                    {"eventId": event_id, "matchTitle": match_title(event)},
                )
            )
    else:
        checks.append(
            CheckResult(
                "event selection",
                "-",
                False,
                None,
                "LCK schedule에서 검사할 이벤트를 찾지 못했습니다.",
                {},
            )
        )

    completed = pick_completed_event(schedule)
    if completed:
        event_id = str(completed.get("id") or completed.get("match", {}).get("id"))
        details_url = "https://esports-api.lolesports.com/persisted/gw/getEventDetails"
        completed_details = fetch(
            headers, "getEventDetails/completed", details_url, {"hl": LOCALE, "id": event_id}, checks
        )
        completed_event = completed_details.get("data", {}).get("event", completed)
        completed_game = pick_completed_game(completed_event.get("match", {}).get("games", []))
        if completed_game and completed_game.get("id"):
            game_id = str(completed_game["id"])
            window = fetch(
                headers,
                "livestats/window/completed",
                f"https://feed.lolesports.com/livestats/v1/window/{game_id}",
                {"startingTime": latest},
                checks,
            )
            live_details = fetch(
                headers,
                "livestats/details/completed",
                f"https://feed.lolesports.com/livestats/v1/details/{game_id}",
                {"startingTime": latest},
                checks,
            )
            add_report_checks(
                checks,
                "parser/completed-game",
                completed_event,
                completed_game,
                window,
                live_details,
            )

    add_status_flow_checks(checks, schedule)

    OUT_DIR.mkdir(exist_ok=True)
    payload = {
        "generatedAt": datetime.now(UTC).isoformat(),
        "checks": [asdict(check) for check in checks],
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    OUT_HTML.write_text(render_html(payload), encoding="utf-8")
    print(f"Wrote {OUT_JSON}")
    print(f"Wrote {OUT_HTML}")
    for check in checks:
        status = "OK" if check.ok else "FAIL"
        print(f"[{status}] {check.name}: {check.message}")


def fetch(
    headers: dict[str, str],
    name: str,
    url: str,
    params: dict[str, Any] | None,
    checks: list[CheckResult],
) -> dict[str, Any]:
    full_url = url
    if params:
        full_url = f"{url}?{urlencode(params)}"
    request = Request(full_url, headers=headers)
    try:
        with urlopen(request, timeout=20) as response:
            text = response.read().decode("utf-8", errors="replace")
            status = response.status
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = {}
        ok = status < 400 and (bool(data) or status == 204)
        message = "응답 수신"
        if status == 204:
            message = "204 No Content: 해당 시간 윈도우의 라이브 데이터 없음"
        checks.append(
            CheckResult(
                name,
                full_url,
                ok,
                status,
                message if status < 400 else text[:200],
                summarize_payload(name, data),
            )
        )
        return data
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:200]
        checks.append(CheckResult(name, full_url, False, exc.code, body or str(exc), {}))
        return {}
    except URLError as exc:
        checks.append(CheckResult(name, full_url, False, None, str(exc.reason), {}))
        return {}
    except Exception as exc:
        checks.append(CheckResult(name, full_url, False, None, str(exc), {}))
        return {}


def pick_event(schedule: dict[str, Any]) -> dict[str, Any] | None:
    events = schedule.get("data", {}).get("schedule", {}).get("events", [])
    if not events:
        return None
    for state in ("inProgress", "inprogress", "completed", "unstarted"):
        for event in events:
            if str(event.get("state", "")).lower() == state.lower():
                return event
    return events[0]


def latest_starting_time() -> str:
    now = (datetime.now(UTC) - timedelta(seconds=60)).replace(microsecond=0)
    rounded = now.replace(second=now.second - (now.second % 10))
    return rounded.isoformat().replace("+00:00", "Z")


def pick_completed_event(schedule: dict[str, Any]) -> dict[str, Any] | None:
    events = schedule.get("data", {}).get("schedule", {}).get("events", [])
    for event in events:
        if str(event.get("state", "")).lower() == "completed":
            return event
    return None


def pick_game(games: list[dict[str, Any]]) -> dict[str, Any] | None:
    for state in ("inProgress", "inprogress", "completed"):
        for game in games:
            if game_state(game) == state.lower():
                return game
    return games[0] if games else None


def pick_completed_game(games: list[dict[str, Any]]) -> dict[str, Any] | None:
    for game in reversed(games):
        if game_state(game) == "completed":
            return game
    return pick_game(games)


def add_status_flow_checks(checks: list[CheckResult], schedule: dict[str, Any]) -> None:
    events = schedule.get("data", {}).get("schedule", {}).get("events", [])
    today = datetime.now(KST).date()
    todays = [event for event in events if event_date(event) == today]
    current_flow = classify_status_flow(todays)
    checks.append(
        CheckResult(
            "status-flow/current-api",
            "-",
            bool(current_flow),
            None,
            "현재 API 기준 /경기상황 분기 확인",
            {
                "today": str(today),
                "flow": current_flow,
                "todayEvents": [
                    {
                        "time": event_datetime(event).strftime("%Y-%m-%d %H:%M")
                        if event_datetime(event)
                        else None,
                        "state": event.get("state"),
                        "title": event_title(event),
                    }
                    for event in todays
                ],
            },
        )
    )

    samples = {
        "live": [
            sample_event("2026-05-02T08:00:00Z", "inProgress", "DK", "GEN"),
            sample_event("2026-05-02T10:00:00Z", "unstarted", "KRX", "NS"),
        ],
        "upcoming": [sample_event("2026-05-02T09:00:00Z", "unstarted", "GEN", "T1")],
        "all-completed": [sample_event("2026-05-02T08:00:00Z", "completed", "DK", "GEN")],
        "no-today-games": [],
    }
    expected = {
        "live": "live-status",
        "upcoming": "upcoming-today",
        "all-completed": "result-redirect",
        "no-today-games": "no-today-games",
    }
    sample_today = datetime(2026, 5, 2, tzinfo=KST).date()
    for name, sample_events in samples.items():
        flow = classify_status_flow(sample_events, sample_today)
        checks.append(
            CheckResult(
                f"status-flow/sample/{name}",
                "-",
                flow == expected[name],
                None,
                f"expected {expected[name]}, got {flow}",
                {"flow": flow, "events": sample_events},
            )
        )


def classify_status_flow(events: list[dict[str, Any]], today=None) -> str:
    if today is not None:
        events = [event for event in events if event_date(event) == today]
    states = [str(event.get("state", "")).replace("-", "_").lower() for event in events]
    if "inprogress" in states or "in_progress" in states:
        return "live-status"
    if "unstarted" in states:
        return "upcoming-today"
    if "completed" in states:
        return "result-redirect"
    return "no-today-games"


def sample_event(start_time: str, state: str, left: str, right: str) -> dict[str, Any]:
    return {
        "startTime": start_time,
        "state": state,
        "match": {
            "id": f"sample-{left}-{right}",
            "teams": [{"code": left}, {"code": right}],
        },
    }


def event_title(event: dict[str, Any]) -> str:
    teams = event.get("match", {}).get("teams", [])
    if len(teams) >= 2:
        return f"{teams[0].get('code') or teams[0].get('name')} vs {teams[1].get('code') or teams[1].get('name')}"
    return "LCK 경기"


def event_date(event: dict[str, Any]):
    value = event_datetime(event)
    return value.date() if value else None


def event_datetime(event: dict[str, Any]) -> datetime | None:
    start_time = event.get("startTime")
    if not start_time:
        return None
    try:
        return datetime.fromisoformat(start_time.replace("Z", "+00:00")).astimezone(KST)
    except ValueError:
        return None


def summarize_payload(name: str, data: dict[str, Any]) -> dict[str, Any]:
    if name == "getSchedule":
        events = data.get("data", {}).get("schedule", {}).get("events", [])
        return {
            "eventCount": len(events),
            "firstEventState": events[0].get("state") if events else None,
            "firstEventId": (events[0].get("id") or events[0].get("match", {}).get("id")) if events else None,
        }
    if name.startswith("getEventDetails"):
        event = data.get("data", {}).get("event", {})
        games = event.get("match", {}).get("games", [])
        return {
            "eventId": event.get("id"),
            "title": match_title(event) if event else None,
            "gameCount": len(games),
            "strategy": event.get("match", {}).get("strategy"),
        }
    if name.startswith("livestats/window"):
        metadata = data.get("gameMetadata", {})
        frames = data.get("frames", [])
        last_frame = frames[-1] if frames else {}
        return {
            "esportsGameId": data.get("esportsGameId"),
            "hasGameMetadata": bool(metadata),
            "frameCount": len(frames),
            "lastFrameKeys": sorted(last_frame.keys())[:30],
            "sampleBlueTeamKeys": sorted((last_frame.get("blueTeam") or {}).keys())[:30],
            "hasBlueParticipants": bool(metadata.get("blueTeamMetadata", {}).get("participantMetadata")),
            "hasRedParticipants": bool(metadata.get("redTeamMetadata", {}).get("participantMetadata")),
        }
    if name.startswith("livestats/details"):
        return {
            "topLevelKeys": sorted(data.keys())[:20],
            "dictCount": sum(1 for _ in walk_dicts(data)),
        }
    return {}


def add_report_checks(
    checks: list[CheckResult],
    name: str,
    event: dict[str, Any],
    game: dict[str, Any],
    window: dict[str, Any],
    details: dict[str, Any],
) -> None:
    report = build_game_report(event, game, window, details)
    damage_count = sum(1 for player in report.players if player.damage is not None)
    damage_share_count = sum(1 for player in report.players if player.damage_share is not None)
    ban_count = sum(len(draft.bans) for draft in report.draft)
    checks.append(
        CheckResult(
            name,
            "-",
            bool(report.players),
            None,
            "세트 리포트 생성",
            {
                "match": match_title(event),
                "gameId": report.game_id,
                "set": f"{report.set_number}/Bo{report.max_sets}",
                "state": report.state,
                "durationMs": report.duration_ms,
                "playerRows": len(report.players),
                "draftRows": len(report.draft),
                "excludedUnstableFields": {
                    "banCountSeen": ban_count,
                    "playersWithDamageSeen": damage_count,
                    "playersWithDamageShareSeen": damage_share_count,
                    "winnerSeen": report.winner,
                },
                "samplePlayers": [
                    {
                        "team": player.team,
                        "role": player.role,
                        "player": player.player,
                        "champion": player.champion,
                        "kda": [player.kills, player.deaths, player.assists],
                        "damage": player.damage,
                        "damageShare": player.damage_share,
                    }
                    for player in report.players[:4]
                ],
                "draft": [
                    {"team": draft.team, "side": draft.side, "picks": draft.picks, "bans": draft.bans}
                    for draft in report.draft
                ],
                "sampleRosterOutput": sample_roster(report),
                "sampleResultGameOutput": sample_result_game(report),
            },
        )
    )


def walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_dicts(child)


def sample_roster(report) -> str:
    rows = [
        f"{player.side}/{player.team} | {player.role} | {player.champion} | {player.player}"
        for player in report.players
    ]
    return "\n".join([f"Set {report.set_number} / Bo{report.max_sets}", *rows])


def sample_result_game(report) -> str:
    draft = [
        f"{line.side}/{line.team} | Picks: {', '.join(line.picks) or 'none'}"
        for line in report.draft
    ]
    players = [
        (
            f"{player.side}/{player.team} | {player.role} | {player.player} | {player.champion} | "
            f"{player.kills or 0}/{player.deaths or 0}/{player.assists or 0}"
        )
        for player in report.players
    ]
    return "\n".join(
        [
            f"Set {report.set_number} / Bo{report.max_sets}",
            f"State: {report.state} | Time(ms): {report.duration_ms}",
            "Picks",
            *draft,
            "Player stats",
            *players,
        ]
    )


def render_html(payload: dict[str, Any]) -> str:
    cards = []
    for check in payload["checks"]:
        cls = "ok" if check["ok"] else "fail"
        fields = escape(json.dumps(check["fields"], ensure_ascii=False, indent=2))
        cards.append(
            f"""
            <section class="card {cls}">
              <div class="row">
                <h2>{escape(check["name"])}</h2>
                <span>{'OK' if check["ok"] else 'FAIL'}</span>
              </div>
              <p>{escape(check["message"])}</p>
              <p class="url">{escape(check["url"])}</p>
              <pre>{fields}</pre>
            </section>
            """
        )
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <title>LCK Bot Endpoint Validation</title>
  <style>
    body {{ margin: 0; font-family: Arial, sans-serif; background: #f6f7f9; color: #16181d; }}
    main {{ max-width: 1120px; margin: 0 auto; padding: 32px 20px; }}
    h1 {{ font-size: 28px; margin: 0 0 8px; }}
    .meta {{ color: #59606c; margin-bottom: 24px; }}
    .card {{ background: white; border: 1px solid #d9dde5; border-left-width: 6px; border-radius: 8px; padding: 18px; margin: 14px 0; }}
    .ok {{ border-left-color: #1f8f55; }}
    .fail {{ border-left-color: #c43131; }}
    .row {{ display: flex; justify-content: space-between; gap: 16px; align-items: center; }}
    h2 {{ font-size: 18px; margin: 0; }}
    span {{ font-weight: 700; }}
    .url {{ color: #59606c; word-break: break-all; }}
    pre {{ white-space: pre-wrap; overflow-wrap: anywhere; background: #10141c; color: #edf2ff; padding: 14px; border-radius: 6px; }}
  </style>
</head>
<body>
  <main>
    <h1>LCK Bot Endpoint Validation</h1>
    <div class="meta">Generated at {escape(payload["generatedAt"])}</div>
    {''.join(cards)}
  </main>
</body>
</html>
"""


if __name__ == "__main__":
    main()
