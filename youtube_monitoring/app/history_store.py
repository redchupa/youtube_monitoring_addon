"""
YouTube 시청 기록 영속화 - /data/yt_history.json.

형식: { "YYYY-MM-DD": [ { video_id, title, channel, thumbnail, url, duration, timestamp }, ... ] }
"""
from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from datetime import datetime
from typing import Any

_LOGGER = logging.getLogger(__name__)

HISTORY_FILE = "/data/yt_history.json"
FALLBACK_FILE = "yt_history.json"


def _get_history_path() -> str:
    """
    쓰기 가능한 히스토리 파일 경로 반환.
    우선순위: /data (에드온) > /share (HA) > 로컬 상대경로.
    """
    candidates = [
        HISTORY_FILE,
        "/share/yt_history.json",
        os.path.join(os.path.dirname(__file__), "..", FALLBACK_FILE),
    ]
    for path in candidates:
        dir_path = os.path.dirname(path) or "."
        if os.path.exists(dir_path) and os.access(dir_path, os.W_OK):
            return path
    return os.path.join(os.path.dirname(__file__), "..", FALLBACK_FILE)


def load_history() -> dict[str, list[dict[str, Any]]]:
    """파일에서 히스토리 로드. 형식 오류 시 빈 dict 반환."""
    path = _get_history_path()
    if not os.path.exists(path):
        return {}

    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        _LOGGER.warning("History file is not a dict, ignoring")
        return {}
    except json.JSONDecodeError as err:
        _LOGGER.warning("Invalid JSON in history file: %s", err)
        return {}
    except OSError as err:
        _LOGGER.warning("Failed to read history file: %s", err)
        return {}


def save_history(history: dict[str, list[dict[str, Any]]]) -> None:
    """히스토리를 파일에 저장. 디렉터리 없으면 생성."""
    path = _get_history_path()
    dir_path = os.path.dirname(path) or "."
    try:
        os.makedirs(dir_path, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except OSError as err:
        _LOGGER.error("Failed to save history: %s", err)


def add_entry(
    history: dict[str, list[dict[str, Any]]],
    video_id: str,
    title: str,
    channel: str,
    thumbnail: str,
    url: str,
    duration: str = "N/A",
    timestamp: datetime | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """새 항목 추가. 해당 날짜 리스트 맨 앞에 삽입."""
    ts = timestamp or datetime.now()
    date_str = ts.strftime("%Y-%m-%d")
    entry = {
        "video_id": video_id,
        "title": title,
        "channel": channel,
        "thumbnail": thumbnail,
        "url": url,
        "duration": duration,
        "timestamp": ts.isoformat(),
    }
    if date_str not in history:
        history[date_str] = []
    history[date_str].insert(0, entry)
    return history


def has_video_id(history: dict[str, list[dict[str, Any]]], video_id: str) -> bool:
    """히스토리에 video_id가 이미 존재하는지 확인 (중복 저장 방지)."""
    for entries in history.values():
        for e in entries:
            if e.get("video_id") == video_id:
                return True
    return False


def _is_shorts(entry: dict[str, Any]) -> bool:
    """Shorts 여부: duration 또는 channel로 판별."""
    return (
        entry.get("duration") == "Shorts"
        or entry.get("channel") == "YouTube Shorts"
    )


def get_monthly_stats(history: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
    """월별 시청 개수 (전체). { "YYYY-MM": count } 최신순. 하위 호환용."""
    monthly: dict[str, int] = defaultdict(int)
    for date_str, entries in history.items():
        if len(date_str) >= 7:
            month = date_str[:7]
            monthly[month] += len(entries)
    return dict(sorted(monthly.items(), reverse=True))


def get_monthly_breakdown(
    history: dict[str, list[dict[str, Any]]]
) -> dict[str, dict[str, int]]:
    """월별 동영상/쇼츠 구분. { "YYYY-MM": {"videos": n, "shorts": n} } 최신순."""
    monthly: dict[str, dict[str, int]] = {}
    for date_str, entries in history.items():
        if len(date_str) < 7:
            continue
        month = date_str[:7]
        if month not in monthly:
            monthly[month] = {"videos": 0, "shorts": 0}
        for e in entries:
            if _is_shorts(e):
                monthly[month]["shorts"] += 1
            else:
                monthly[month]["videos"] += 1
    return dict(sorted(monthly.items(), reverse=True))
