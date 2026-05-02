from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

from lck_bot.match_data import DraftLine, GameReport, PlayerLine


class YesterdayMatchCache:
    def __init__(self, cache_dir: str) -> None:
        self.cache_dir = Path(cache_dir)

    def load_day(self, target_date: date) -> dict[str, Any] | None:
        path = self._path(target_date)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if payload.get("date") != target_date.isoformat():
            return None
        return payload

    def is_complete(self, target_date: date) -> bool:
        payload = self.load_day(target_date)
        return bool(payload and payload.get("complete"))

    def match_reports(self, target_date: date, match_name: str) -> list[GameReport] | None:
        payload = self.load_day(target_date)
        if not payload or not payload.get("complete"):
            return None

        normalized = match_name.strip().lower()
        for match in payload.get("matches", []):
            if str(match.get("matchName", "")).lower() != normalized:
                continue
            return [_report_from_dict(item) for item in match.get("reports", [])]
        return None

    def save_day(
        self,
        target_date: date,
        matches: list[dict[str, Any]],
        complete: bool,
        reasons: list[str] | None = None,
    ) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "date": target_date.isoformat(),
            "complete": complete,
            "generatedAt": datetime.now().isoformat(timespec="seconds"),
            "reasons": reasons or [],
            "matches": matches,
        }
        path = self._path(target_date)
        temp_path = path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(path)

    def _path(self, target_date: date) -> Path:
        return self.cache_dir / f"yesterday-{target_date.isoformat()}.json"


def cached_match_payload(
    match_name: str,
    start_time: str | None,
    state: str,
    reports: list[GameReport],
) -> dict[str, Any]:
    return {
        "matchName": match_name,
        "startTime": start_time,
        "state": state,
        "reports": [asdict(report) for report in reports],
    }


def _report_from_dict(data: dict[str, Any]) -> GameReport:
    payload = dict(data)
    payload["draft"] = [DraftLine(**item) for item in payload.get("draft", [])]
    payload["players"] = [PlayerLine(**item) for item in payload.get("players", [])]
    return GameReport(**payload)
