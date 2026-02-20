"""
구독 채널 변경 추적 - /data/yt_subscriptions.json.

형식:
{
  "last_snapshot": { "date": "YYYY-MM-DD", "channels": ["채널1", "채널2"] },
  "monthly_changes": {
    "YYYY-MM": { "added": ["채널A"], "removed": ["채널B"] }
  }
}
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any

_LOGGER = logging.getLogger(__name__)

SUBS_FILE = "/data/yt_subscriptions.json"
FALLBACK_FILE = "yt_subscriptions.json"


def _get_subs_path() -> str:
    """쓰기 가능한 구독 파일 경로 반환."""
    candidates = [
        SUBS_FILE,
        "/share/yt_subscriptions.json",
        os.path.join(os.path.dirname(__file__), "..", FALLBACK_FILE),
    ]
    for path in candidates:
        dir_path = os.path.dirname(path) or "."
        if os.path.exists(dir_path) and os.access(dir_path, os.W_OK):
            return path
    return os.path.join(os.path.dirname(__file__), "..", FALLBACK_FILE)


def load_subscription_store() -> dict[str, Any]:
    """구독 저장소 로드."""
    path = _get_subs_path()
    if not os.path.exists(path):
        return {"last_snapshot": None, "monthly_changes": {}}

    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        return {"last_snapshot": None, "monthly_changes": {}}
    except (json.JSONDecodeError, OSError) as err:
        _LOGGER.warning("Failed to load subscription store: %s", err)
        return {"last_snapshot": None, "monthly_changes": {}}


def save_subscription_store(store: dict[str, Any]) -> None:
    """구독 저장소 저장."""
    path = _get_subs_path()
    dir_path = os.path.dirname(path) or "."
    try:
        os.makedirs(dir_path, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False, indent=2)
    except OSError as err:
        _LOGGER.error("Failed to save subscription store: %s", err)


def update_subscription_changes(
    current_channels: list[dict[str, str]],
) -> dict[str, dict[str, list[str]]]:
    """
    현재 구독 목록과 이전 스냅샷 비교 → 월별 변경 누적 후 저장.

    current_channels: [{ "channel_name": "..." }, ...]
    Returns: monthly_changes { "YYYY-MM": { "added": [...], "removed": [...] } }
    """
    store = load_subscription_store()
    current_names = {c.get("channel_name", "").strip() for c in current_channels if c.get("channel_name")}

    last = store.get("last_snapshot")
    monthly = store.get("monthly_changes", {})

    if last and last.get("channels"):
        prev_names = set(last.get("channels", []))
        added = list(current_names - prev_names)
        removed = list(prev_names - current_names)

        if added or removed:
            month = datetime.now().strftime("%Y-%m")
            if month not in monthly:
                monthly[month] = {"added": [], "removed": []}
            existing_added = set(monthly[month]["added"])
            existing_removed = set(monthly[month]["removed"])
            monthly[month]["added"] = list(existing_added | set(added))
            monthly[month]["removed"] = list(existing_removed | set(removed))
            store["monthly_changes"] = dict(sorted(monthly.items(), reverse=True))
            _LOGGER.info("Subscription changes: +%d -%d in %s", len(added), len(removed), month)

    store["last_snapshot"] = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "channels": list(current_names),
    }
    save_subscription_store(store)
    return store.get("monthly_changes", {})


def get_monthly_subscription_changes() -> dict[str, dict[str, list[str]]]:
    """월별 구독 변경 내역 반환. { "YYYY-MM": { "added": [...], "removed": [...] } } 최신순."""
    store = load_subscription_store()
    monthly = store.get("monthly_changes", {})
    return dict(sorted(monthly.items(), reverse=True))
