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
import time
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


MASS_DROP_MIN = 20  # 이 개수 이상이 한 번에 빠지고
MASS_DROP_RATIO = 0.5  # 이전 목록의 절반 이상이 빠지면 '급감'으로 판단
MASS_DROP_CONFIRM_SEC = 24 * 3600
MASS_REMOVAL_REPAIR_MIN = 100  # 한 달 '구독 취소'가 이 이상이면 오기록으로 보고 복구
REPAIR_ID = "1.5.5-mass-removal"  # 복구는 설치당 한 번만 (이후 데이터는 급감 가드가 보호)


def _is_mass_drop(prev_count: int, removed_count: int) -> bool:
    return removed_count >= MASS_DROP_MIN and removed_count >= prev_count * MASS_DROP_RATIO


def _confirm_mass_drop(store: dict[str, Any], current_names: set[str]) -> bool:
    """같은 급감 목록이 24시간 이상 유지됐는지. 목록이 바뀌면 처음부터 다시 셈."""
    signature = "\n".join(sorted(current_names))
    now = time.time()
    pending = store.get("pending_drop")
    if not isinstance(pending, dict) or pending.get("signature") != signature:
        store["pending_drop"] = {"signature": signature, "first_seen": now}
        return False
    return now - float(pending.get("first_seen", now)) >= MASS_DROP_CONFIRM_SEC


def repair_mass_removals() -> int:
    """
    과거 대량 '구독 취소' 오기록 복구 (1.5.4 회귀: 목록 일부만 받은 응답이 그대로 기록됨).
    해당 월의 removed를 비우고, 빠졌던 채널을 기준 목록에 되돌린다. 복구한 월 수 반환.
    """
    store = load_subscription_store()
    done = store.get("repairs_done") or []
    if REPAIR_ID in done:
        return 0
    monthly = store.get("monthly_changes") or {}
    snapshot = store.get("last_snapshot") or {}
    names = set(snapshot.get("channels") or [])
    repaired = []
    for month, change in monthly.items():
        removed = change.get("removed") or []
        if len(removed) >= MASS_REMOVAL_REPAIR_MIN:
            names |= set(removed)
            change["removed"] = []
            repaired.append((month, len(removed)))
    store["repairs_done"] = list(done) + [REPAIR_ID]
    if not repaired:
        save_subscription_store(store)
        return 0
    store["last_snapshot"] = {
        "date": snapshot.get("date") or datetime.now().strftime("%Y-%m-%d"),
        "channels": list(names),
    }
    store.pop("pending_drop", None)
    save_subscription_store(store)
    for month, n in repaired:
        _LOGGER.warning("[구독] %s 대량 구독 취소 오기록 %d건 복구 (기준 목록 %d개로 복원)", month, n, len(names))
    return len(repaired)


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

        if _is_mass_drop(len(prev_names), len(removed)):
            # 한 번에 대량 '구독 취소'는 실제 변화보다 YouTube가 목록 일부만 준 경우가 대부분.
            # 같은 목록이 오래(24시간) 계속되면 계정 변경 등으로 보고 기록 없이 기준만 교체.
            if _confirm_mass_drop(store, current_names):
                _LOGGER.warning("[구독] 목록 급감 상태가 24시간 지속 → 변경 기록 없이 기준 목록 교체 (%d → %d개)",
                                len(prev_names), len(current_names))
                store.pop("pending_drop", None)
                store["last_snapshot"] = {
                    "date": datetime.now().strftime("%Y-%m-%d"),
                    "channels": list(current_names),
                }
            else:
                _LOGGER.warning("[구독] 목록이 %d → %d개로 급감 - 일시적 응답 이상으로 보고 기록하지 않음",
                                len(prev_names), len(current_names))
            save_subscription_store(store)
            return store.get("monthly_changes", {})
        store.pop("pending_drop", None)

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
