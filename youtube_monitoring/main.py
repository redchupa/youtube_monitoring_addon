#!/usr/bin/env python3
"""YouTube Monitoring Add-on - HTTP API (cookie-based history, REST + web UI)."""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from typing import Any

from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.fetcher import YouTubeHistoryFetcher
from app.history_store import (
    load_history,
    save_history,
    add_entry,
    get_monthly_stats,
    get_monthly_breakdown,
    has_video_id,
)
from app.subscription_store import (
    update_subscription_changes,
    get_monthly_subscription_changes,
)

_log_tz_cache: str | None = None


def _get_log_timezone() -> str:
    """로그용 타임존. options.json 또는 TZ 환경변수, 기본 Asia/Seoul."""
    global _log_tz_cache
    if _log_tz_cache is not None:
        return _log_tz_cache
    try:
        if os.path.exists("/data/options.json"):
            with open("/data/options.json", encoding="utf-8") as f:
                opts = json.load(f)
                if opts.get("timezone"):
                    _log_tz_cache = str(opts["timezone"])
                    return _log_tz_cache
    except (json.JSONDecodeError, OSError):
        pass
    _log_tz_cache = os.environ.get("TZ", "Asia/Seoul")
    return _log_tz_cache


def _local_time_converter(timestamp: float | None) -> time.struct_time:
    """로그 타임스탬프를 로컬 타임존으로 변환."""
    tz_name = _get_log_timezone()
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Asia/Seoul")
    dt = datetime.fromtimestamp(timestamp or time.time(), tz=ZoneInfo("UTC")).astimezone(tz)
    return dt.timetuple()


_fmt = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_fmt.converter = _local_time_converter  # type: ignore
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(_fmt)
logging.basicConfig(level=logging.INFO, handlers=[_handler])
_LOGGER = logging.getLogger(__name__)

fetcher: YouTubeHistoryFetcher | None = None
_history_lock = threading.Lock()
_subs_lock = threading.Lock()
_last_seen_video_id: str | None = None
_recent_added: dict[str, float] = {}  # video_id -> timestamp (5분 내 중복 방지)
_last_recommended_fetch: float = 0  # 추천 영상 마지막 fetch 시각
_last_manual_refresh_recommended: float = 0  # 수동 새로고침 마지막 시각 (10분 쿨다운)
_last_subscriptions_fetch: float = 0  # 구독 채널 마지막 fetch (2분 간격, 429 방지)

REFRESH_COOLDOWN_SEC = 600  # 수동 새로고침 쿨다운 10분


def _parse_bool(val: Any) -> bool:
    """옵션 값을 bool로 변환."""
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.lower() in ("true", "1", "yes", "on")
    return bool(val)


def load_options() -> dict:
    """에드온 옵션 로드. HA는 /data/options.json, 로컬은 환경변수 사용."""
    options_path = "/data/options.json"
    defaults = {
        "cookies_path": os.environ.get("COOKIES_PATH", "/config/youtube_cookies.txt"),
        "scan_interval": int(os.environ.get("SCAN_INTERVAL", "60")),
        "port": int(os.environ.get("PORT", "8765")),
        "duplicate_minutes": int(os.environ.get("DUPLICATE_MINUTES", "5")),
        "fetch_recommended": _parse_bool(os.environ.get("FETCH_RECOMMENDED", "true")),
        "scan_interval_recommended": int(os.environ.get("SCAN_INTERVAL_RECOMMENDED", "1800")),
        "timezone": os.environ.get("TZ", "Asia/Seoul"),
    }
    try:
        if os.path.exists(options_path):
            with open(options_path, encoding="utf-8") as f:
                opts = json.load(f)
            defaults.update(opts)
            if "fetch_recommended" in opts:
                defaults["fetch_recommended"] = _parse_bool(opts["fetch_recommended"])
            if "scan_interval_recommended" in opts:
                defaults["scan_interval_recommended"] = int(opts["scan_interval_recommended"])
    except (json.JSONDecodeError, OSError) as err:
        _LOGGER.warning("Failed to load options.json: %s, using env defaults", err)

    return defaults


def _is_shorts(video_data: dict) -> bool:
    """Shorts 영상 여부: 기록/통계에서 제외."""
    if video_data.get("duration") == "Shorts":
        return True
    if video_data.get("channel") == "YouTube Shorts":
        return True
    url = video_data.get("url") or ""
    if "/shorts/" in url:
        return True
    return False


def _filter_shorts_from_history(history: dict) -> dict:
    """Shorts 항목을 제거한 히스토리 복사본 (통계/표시용)."""
    filtered: dict[str, list] = {}
    for date_str, entries in history.items():
        filtered[date_str] = [e for e in entries if not _is_shorts(e)]
    return {k: v for k, v in filtered.items() if v}


def _now_in_user_tz() -> datetime:
    """설정된 timezone 기준 현재 시각 (시청 기록 날짜용)."""
    opts = load_options()
    tz_name = opts.get("timezone", "Asia/Seoul")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Asia/Seoul")
    return datetime.now(tz)


def on_video_change(video_data: dict) -> bool:
    """
    video_id 변경 시 yt_history.json에 추가.
    Shorts는 기록하지 않음.
    Returns: True=저장됨, False=중복/Shorts/유효하지 않음.
    """
    if _is_shorts(video_data):
        return False
    with _history_lock:
        history = load_history()
        vid = video_data.get("video_id")

        if not vid or vid == "N/A":
            return False
        if has_video_id(history, vid):
            return False

        add_entry(
            history,
            video_id=video_data["video_id"],
            title=video_data.get("title", "N/A"),
            channel=video_data.get("channel", "N/A"),
            thumbnail=video_data.get("thumbnail", ""),
            url=video_data.get("url", ""),
            duration=video_data.get("duration", "N/A"),
            timestamp=_now_in_user_tz(),
        )
        save_history(history)
        _LOGGER.info("[기록] 저장: %s", video_data.get("title", video_data.get("video_id")))
        return True


def fetch_loop() -> None:
    """
    백그라운드 루프: YouTube 시청 기록 폴링 + video_id 변경 시 저장.
    시청 기록: 매 루프. 구독 채널: 2분 간격. 추천 영상: 60초 간격 (옵션).
    429 rate limit 방지를 위해 요청 간격 조절.
    """
    global fetcher, _last_seen_video_id, _recent_added, _last_recommended_fetch, _last_subscriptions_fetch
    if not fetcher:
        return

    opts = load_options()
    interval = opts.get("scan_interval", 60)
    duplicate_sec = opts.get("duplicate_minutes", 5) * 60
    recommended_interval = opts.get("scan_interval_recommended", 600)
    subscriptions_interval = 120  # 구독 채널 2분 간격 (429 방지)
    fetch_recommended = opts.get("fetch_recommended", True)

    while True:
        try:
            now = time.time()
            fetcher.fetch_history()
            time.sleep(2)  # 요청 간 2초 대기 (429 방지)

            if now - _last_subscriptions_fetch >= subscriptions_interval:
                fetcher.fetch_subscriptions()
                _last_subscriptions_fetch = time.time()
                sub_data = fetcher.subscriptions_data
                if sub_data and sub_data.get("channels"):
                    with _subs_lock:
                        update_subscription_changes(sub_data["channels"])
                time.sleep(2)

            if fetch_recommended:
                if now - _last_recommended_fetch >= recommended_interval:
                    fetcher.fetch_recommended()
                    _last_recommended_fetch = now

            videos = fetcher.history_data or []

            if videos:
                most_recent = videos[0]
                video_id = most_recent.get("video_id")
                if video_id and video_id != "N/A":
                    now = time.time()
                    if video_id != _last_seen_video_id:
                        last_added = _recent_added.get(video_id, 0)
                        if now - last_added >= duplicate_sec:
                            if on_video_change(most_recent):
                                _recent_added[video_id] = now
                        _last_seen_video_id = video_id
                    _recent_added = {k: v for k, v in _recent_added.items() if now - v < duplicate_sec * 2}

            if not fetcher.cookies_valid:
                _LOGGER.warning("[폴링] 쿠키 무효 | 시청 기록/구독 조회 불가, 쿠키 파일 갱신 필요")
            else:
                _LOGGER.debug("[폴링] 시청 기록 %d건 조회", len(videos))
        except Exception as err:
            _LOGGER.error("[폴링] 오류: %s", err)
        time.sleep(interval)


class YouTubeMonitoringHandler(BaseHTTPRequestHandler):
    """HTTP 요청 핸들러: REST API + 웹 UI."""

    def log_message(self, format, *args):
        _LOGGER.debug("%s - %s", self.address_string(), format % args)

    def send_json(self, data: dict, status: int = 200) -> None:
        """JSON 응답 전송 (CORS 포함)."""
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def send_html(self, html: str, status: int = 200) -> None:
        """HTML 응답 전송."""
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def do_OPTIONS(self) -> None:
        """CORS preflight (POST /api/ingest용)."""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self) -> None:
        """POST /api/ingest, POST /api/refresh/recommended."""
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/api/ingest":
            self._handle_ingest()
            return
        if path == "/api/refresh/recommended":
            self._handle_refresh_recommended()
            return
        self.send_response(404)
        self.end_headers()

    def _handle_ingest(self) -> None:
        """실시간 video_id 수신 → yt_history.json에 즉시 저장."""
        global _last_seen_video_id, _recent_added

        content_len = int(self.headers.get("Content-Length", 0))
        try:
            body = self.rfile.read(content_len).decode("utf-8")
            data = json.loads(body) if body else {}
        except (json.JSONDecodeError, ValueError):
            self.send_json({"error": "Invalid JSON"}, status=400)
            return

        video_id = data.get("video_id") or data.get("videoId")
        if not video_id or video_id == "N/A":
            self.send_json({"error": "video_id required"}, status=400)
            return

        opts = load_options()
        duplicate_sec = opts.get("duplicate_minutes", 5) * 60
        now = time.time()
        if now - _recent_added.get(video_id, 0) < duplicate_sec:
            self.send_json({"status": "skipped", "reason": "duplicate"})
            return

        url = data.get("url", f"https://www.youtube.com/watch?v={video_id}")
        if "/shorts/" in url or data.get("duration") == "Shorts":
            self.send_json({"status": "skipped", "reason": "shorts"})
            return
        video_data = {
            "video_id": video_id,
            "title": data.get("title", "N/A"),
            "channel": data.get("channel", "N/A"),
            "thumbnail": data.get("thumbnail", "") or f"https://img.youtube.com/vi/{video_id}/0.jpg",
            "url": url,
            "duration": data.get("duration", "N/A"),
        }
        if not on_video_change(video_data):
            self.send_json({"status": "skipped", "reason": "duplicate"})
            return

        _last_seen_video_id = video_id
        _recent_added[video_id] = now
        self.send_json({"status": "ok", "video_id": video_id})

    def _handle_refresh_recommended(self) -> None:
        """수동 추천 영상 새로고침. 10분 쿨다운."""
        global fetcher, _last_manual_refresh_recommended
        now = time.time()
        elapsed = now - _last_manual_refresh_recommended
        if elapsed < REFRESH_COOLDOWN_SEC:
            retry_after = int(REFRESH_COOLDOWN_SEC - elapsed)
            self.send_json({
                "error": "cooldown",
                "retry_after": retry_after,
                "message": f"{retry_after}초 후 다시 시도하세요.",
            }, status=429)
            return
        if not fetcher:
            self.send_json({"error": "fetcher not ready"}, status=503)
            return
        fetcher.fetch_recommended()
        _last_manual_refresh_recommended = now
        self.send_json({
            "status": "ok",
            "recommended": fetcher.recommended_data or [],
            "next_refresh_at": int(now + REFRESH_COOLDOWN_SEC),
        })

    def do_GET(self) -> None:
        """GET 라우팅: /, /api/history, /api/stats, /api/health."""
        global fetcher
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            self._serve_ui()
            return
        if path == "/api/history" or path == "/history":
            self._serve_history(query)
            return
        if path == "/api/stats":
            self._serve_stats()
            return
        if path == "/api/health":
            self.send_json({
                "status": "ok",
                "cookies_valid": fetcher.cookies_valid if fetcher else False,
            })
            return
        self.send_response(404)
        self.end_headers()

    def _serve_history(self, query: dict) -> None:
        """누적 기록(yt_history.json) + 실시간 조회(fetcher) 병합 응답. Shorts 제외."""
        global fetcher, _last_manual_refresh_recommended
        with _history_lock:
            accumulated = load_history()

        accumulated = _filter_shorts_from_history(accumulated)
        live_videos = fetcher.history_data if (fetcher and fetcher.history_data) else []
        live_videos = [v for v in live_videos if not _is_shorts(v)]
        by_date: dict[str, list] = dict(accumulated)
        monthly = get_monthly_stats(accumulated)
        monthly_breakdown = get_monthly_breakdown(accumulated)

        opts = load_options()
        with _subs_lock:
            monthly_subs = get_monthly_subscription_changes()

        now = time.time()
        elapsed = now - _last_manual_refresh_recommended
        recommended_refresh_available_at = int(now + max(0, REFRESH_COOLDOWN_SEC - elapsed))
        recommended_refresh_retry_after = int(max(0, REFRESH_COOLDOWN_SEC - elapsed))

        self.send_json({
            "cookies_valid": fetcher.cookies_valid if fetcher else False,
            "by_date": by_date,
            "monthly_stats": monthly,
            "monthly_breakdown": monthly_breakdown,
            "live": live_videos,
            "subscriptions": fetcher.subscriptions_data if fetcher else None,
            "monthly_subscription_changes": monthly_subs,
            "recommended": fetcher.recommended_data if fetcher else None,
            "fetch_recommended": opts.get("fetch_recommended", True),
            "recommended_refresh_available_at": recommended_refresh_available_at,
            "recommended_refresh_retry_after": recommended_refresh_retry_after,
        })

    def _serve_stats(self) -> None:
        """월별 통계 반환 (Shorts 제외)."""
        with _history_lock:
            history = load_history()
        history = _filter_shorts_from_history(history)
        monthly = get_monthly_stats(history)
        monthly_breakdown = get_monthly_breakdown(history)
        self.send_json({
            "monthly_stats": monthly,
            "monthly_breakdown": monthly_breakdown,
        })

    def _serve_ui(self) -> None:
        """웹 UI HTML 응답."""
        html = _get_ui_html()
        self.send_html(html)


def _get_ui_html() -> str:
    """웹 UI HTML (인라인). Ingress 시 상대 경로로 /api/history 호출."""
    return """<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>YouTube 시청 기록</title>
    <style>
        * { box-sizing: border-box; }
        :root { --ha-primary: #03a9f4; --ha-bg: #111; --ha-card: #1c1c1c; --ha-text: #e1e1e1; --ha-text-secondary: #9e9e9e; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 0; padding: 0; background: var(--ha-bg); color: var(--ha-text); min-height: 100vh; }
        .container { max-width: 900px; margin: 0 auto; padding: 20px; }
        h1 { color: #ff0000; font-size: 1.5rem; margin-bottom: 8px; display: flex; align-items: center; gap: 8px; }
        h1 .icon { font-size: 1.2em; }
        .tabs { display: flex; gap: 8px; margin-bottom: 20px; }
        .tab { padding: 10px 20px; background: var(--ha-card); border: none; border-radius: 8px; color: var(--ha-text); cursor: pointer; font-size: 0.95rem; }
        .tab:hover { background: #2a2a2a; }
        .tab:disabled { opacity: 0.5; cursor: not-allowed; }
        .tab:disabled:hover { background: var(--ha-card); }
        .tab.active { background: var(--ha-primary); color: #fff; }
        .panel { display: none; }
        .panel.active { display: block; }
        .stats-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(120px, 1fr)); gap: 12px; margin-bottom: 24px; }
        .stat-card { background: var(--ha-card); border-radius: 8px; padding: 16px; text-align: center; }
        .stat-card .value { font-size: 1.5rem; font-weight: 700; color: var(--ha-primary); }
        .stat-card .label { font-size: 0.8rem; color: var(--ha-text-secondary); margin-top: 4px; }
        .stat-card .breakdown { font-size: 0.75rem; color: var(--ha-text-secondary); margin-top: 2px; }
        .date-group { margin-bottom: 16px; }
        .date-header { display: flex; align-items: center; justify-content: space-between; padding: 12px 16px; background: var(--ha-card); border-radius: 8px; cursor: pointer; user-select: none; }
        .date-header:hover { background: #2a2a2a; }
        .date-header .date { font-weight: 600; }
        .date-header .count { color: var(--ha-text-secondary); font-size: 0.9rem; }
        .date-header .chevron { transition: transform 0.2s; }
        .date-group.collapsed .chevron { transform: rotate(-90deg); }
        .date-content { padding: 8px 0 0 0; }
        .date-group.collapsed .date-content { display: none; }
        .video { display: flex; gap: 16px; padding: 12px; margin: 4px 0; background: var(--ha-card); border-radius: 8px; align-items: center; transition: background 0.2s; }
        .video:hover { background: #2a2a2a; }
        .video img { width: 120px; height: 68px; object-fit: cover; border-radius: 4px; flex-shrink: 0; }
        .video .info { flex: 1; min-width: 0; }
        .video .title { font-weight: 600; margin-bottom: 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .video .channel { color: var(--ha-text-secondary); font-size: 0.9em; }
        .video .meta { color: var(--ha-text-secondary); font-size: 0.85em; margin-top: 4px; }
        .video a { color: var(--ha-primary); text-decoration: none; }
        .video a:hover { text-decoration: underline; }
        .status { padding: 8px 12px; border-radius: 6px; margin-bottom: 16px; font-size: 0.9rem; }
        .status.ok { background: rgba(0,200,83,0.2); color: #4caf50; }
        .status.error { background: rgba(244,67,54,0.2); color: #f44336; }
        .cookie-status { display: inline-flex; align-items: center; gap: 6px; padding: 6px 12px; border-radius: 20px; font-size: 0.85rem; font-weight: 500; margin-left: 12px; }
        .cookie-status.connected { background: rgba(0,200,83,0.25); color: #4caf50; }
        .cookie-status.disconnected { background: rgba(244,67,54,0.25); color: #f44336; }
        .cookie-status.loading { background: rgba(158,158,158,0.25); color: var(--ha-text-secondary); }
        .cookie-status::before { content: ''; width: 8px; height: 8px; border-radius: 50%; background: currentColor; }
        .empty { text-align: center; padding: 40px; color: var(--ha-text-secondary); }
        .calendar-wrap { margin-top: 12px; }
        .calendar-month-row { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
        .calendar-month-row select { padding: 8px 12px; border-radius: 8px; background: var(--ha-card); color: var(--ha-text); border: 1px solid #333; font-size: 0.95rem; min-width: 140px; }
        .calendar-month-title { font-weight: 600; font-size: 1.1rem; }
        .calendar-grid { display: grid; grid-template-columns: repeat(7, 1fr); gap: 4px; max-width: 400px; }
        .calendar-weekday { text-align: center; font-size: 0.75rem; color: var(--ha-text-secondary); padding: 6px 0; font-weight: 500; }
        .calendar-day { background: var(--ha-card); border-radius: 6px; min-height: 44px; padding: 4px; display: flex; flex-direction: column; align-items: center; justify-content: center; cursor: default; }
        .calendar-day-empty { background: transparent; opacity: 0.3; }
        .calendar-day-num { font-size: 0.8rem; color: var(--ha-text-secondary); }
        .calendar-day-count { font-size: 1rem; font-weight: 700; color: var(--ha-primary); }
        .calendar-day.has-count { cursor: pointer; }
        .calendar-day.has-count:hover { background: #2a2a2a; }
        .calendar-day-detail { margin-top: 20px; padding-top: 16px; border-top: 1px solid #333; }
        .calendar-day-detail h4 { margin: 0 0 12px 0; font-size: 0.95rem; color: var(--ha-text-secondary); }
    </style>
</head>
<body>
    <div class="container">
        <h1 style="display:flex; align-items:center; flex-wrap:wrap;">
            <span class="icon">▶</span> YouTube 시청 기록
            <span id="cookie-status" class="cookie-status loading">확인 중...</span>
        </h1>
        <div class="tabs">
            <button class="tab active" data-tab="daily">일별 기록</button>
            <button class="tab" data-tab="monthly">월별 통계</button>
            <button class="tab" data-tab="subscriptions">구독 채널</button>
            <button class="tab" data-tab="monthly-subs">월별 구독</button>
            <button class="tab tab-recommended" data-tab="recommended" style="display:none;">추천 영상</button>
        </div>

        <div id="panel-daily" class="panel active">
            <div id="status" class="status">로딩 중...</div>
            <div id="daily-content"></div>
        </div>

        <div id="panel-monthly" class="panel">
            <div class="calendar-wrap">
                <div class="calendar-month-row">
                    <label for="month-select" class="calendar-month-title">월 선택</label>
                    <select id="month-select"></select>
                </div>
                <div id="calendar-grid" class="calendar-grid"></div>
                <div id="calendar-day-detail" class="calendar-day-detail" style="display:none;"></div>
            </div>
        </div>

        <div id="panel-subscriptions" class="panel">
            <div class="panel-header" style="display:flex; align-items:center; justify-content:space-between; margin-bottom:12px;">
                <span>정렬</span>
                <button id="btn-subscription-sort" class="tab" style="padding:6px 14px; font-size:0.85rem;">기본 순</button>
            </div>
            <div id="subscriptions-content" class="empty">로딩 중...</div>
        </div>

        <div id="panel-monthly-subs" class="panel">
            <div id="monthly-subs-content" class="empty">로딩 중...</div>
        </div>

        <div id="panel-recommended" class="panel">
            <div class="panel-header" style="display:flex; align-items:center; justify-content:space-between; margin-bottom:12px;">
                <span>추천 영상</span>
                <div>
                    <button id="btn-refresh-recommended" class="tab" style="padding:6px 14px; font-size:0.85rem;">새로고침</button>
                    <span id="recommended-cooldown" style="font-size:0.8rem; color:var(--ha-text-secondary); margin-left:8px;"></span>
                </div>
            </div>
            <div id="recommended-content" class="empty">로딩 중...</div>
        </div>
    </div>

    <script>
        let historyData = { by_date: {}, monthly_stats: {}, monthly_breakdown: {}, subscriptions: null, monthly_subscription_changes: {}, recommended: null, fetch_recommended: true, recommended_refresh_available_at: 0, recommended_refresh_retry_after: 0 };
        let subscriptionSortBy = 'subscribers';

        async function load() {
            try {
                const base = window.location.pathname.endsWith('/') ? window.location.pathname : window.location.pathname + '/';
                const r = await fetch(base + 'api/history');
                historyData = await r.json();
                renderCookieStatus();
                const recTab = document.querySelector('.tab-recommended');
                if (recTab) recTab.style.display = (historyData.fetch_recommended === true) ? '' : 'none';
                renderDaily();
                renderMonthly();
                renderSubscriptions();
                renderMonthlySubscriptions();
                renderRecommended();
            } catch (e) {
                document.getElementById('cookie-status').className = 'cookie-status disconnected';
                document.getElementById('cookie-status').textContent = '연결 오류';
                document.getElementById('status').className = 'status error';
                document.getElementById('status').textContent = '오류: ' + e.message;
            }
        }

        function renderCookieStatus() {
            const el = document.getElementById('cookie-status');
            if (!el) return;
            const valid = historyData.cookies_valid;
            if (valid === true) {
                el.className = 'cookie-status connected';
                el.textContent = '쿠키 연결됨';
            } else if (valid === false) {
                el.className = 'cookie-status disconnected';
                el.textContent = '쿠키 사용 불가';
            } else {
                el.className = 'cookie-status loading';
                el.textContent = '확인 중...';
            }
        }

        function renderDaily() {
            const status = document.getElementById('status');
            const content = document.getElementById('daily-content');
            const dates = Object.keys(historyData.by_date || {}).sort().reverse();

            if (historyData.cookies_valid === false && dates.length === 0) {
                status.className = 'status error';
                status.textContent = '쿠키가 유효하지 않습니다. /config/youtube_cookies.txt 파일을 확인하세요.';
            } else {
                status.className = 'status ok';
                status.textContent = dates.length ? dates.length + '일치 기록 (' + dates.reduce((s,d) => s + (historyData.by_date[d]?.length||0), 0) + '개 영상)' : '기록이 없습니다. YouTube 시청 후 잠시 기다려 주세요.';
            }

            if (dates.length === 0) {
                content.innerHTML = '<div class="empty">아직 시청 기록이 없습니다.</div>';
                return;
            }

            content.innerHTML = dates.map(date => {
                const entries = historyData.by_date[date] || [];
                return `
                    <div class="date-group" data-date="${date}">
                        <div class="date-header">
                            <span class="date">${date}</span>
                            <span class="count">${entries.length}개</span>
                            <span class="chevron">▼</span>
                        </div>
                        <div class="date-content">
                            ${entries.map(v => `
                                <a href="${v.url}" target="_blank" class="video" style="text-decoration:none;color:inherit;">
                                    <img src="${v.thumbnail || ''}" alt="">
                                    <div class="info">
                                        <div class="title">${escapeHtml(v.title)}</div>
                                        <div class="channel">${escapeHtml(v.channel)}</div>
                                        <div class="meta">${v.duration} · ${formatTime(v.timestamp)}</div>
                                    </div>
                                </a>
                            `).join('')}
                        </div>
                    </div>
                `;
            }).join('');

            content.querySelectorAll('.date-header').forEach(el => {
                el.addEventListener('click', () => {
                    el.closest('.date-group').classList.toggle('collapsed');
                });
            });
        }

        function getMonthsFromByDate() {
            const byDate = historyData.by_date || {};
            const set = new Set();
            Object.keys(byDate).forEach(d => { if (d.length >= 7) set.add(d.substring(0, 7)); });
            return Array.from(set).sort().reverse();
        }

        function renderMonthly() {
            const wrap = document.getElementById('calendar-grid');
            const selectEl = document.getElementById('month-select');
            const dayDetailEl = document.getElementById('calendar-day-detail');
            if (!wrap || !selectEl) return;

            const months = getMonthsFromByDate();
            if (months.length === 0) {
                wrap.innerHTML = '';
                selectEl.innerHTML = '<option value="">기록 없음</option>';
                dayDetailEl.style.display = 'none';
                return;
            }

            const byDate = historyData.by_date || {};
            selectEl.innerHTML = months.map(m => {
                const [y, mo] = m.split('-');
                const label = y + '년 ' + parseInt(mo, 10) + '월';
                return '<option value="' + m + '">' + label + '</option>';
            }).join('');

            const selectedMonth = selectEl.value || months[0];
            selectEl.value = selectedMonth;
            renderCalendarForMonth(selectedMonth, byDate, wrap, dayDetailEl);

            selectEl.onchange = () => {
                dayDetailEl.style.display = 'none';
                renderCalendarForMonth(selectEl.value, historyData.by_date || {}, wrap, dayDetailEl);
            };
        }

        function renderCalendarForMonth(monthStr, byDate, gridEl, dayDetailEl) {
            const weekdays = ['일', '월', '화', '수', '목', '금', '토'];
            const [y, m] = monthStr.split('-').map(Number);
            const first = new Date(y, m - 1, 1);
            const last = new Date(y, m, 0);
            const firstDay = first.getDay();
            const daysInMonth = last.getDate();

            let html = weekdays.map(w => '<div class="calendar-weekday">' + w + '</div>').join('');

            const pad = n => (n < 10 ? '0' + n : '' + n);
            const emptyCells = firstDay;
            for (let i = 0; i < emptyCells; i++) html += '<div class="calendar-day calendar-day-empty"></div>';

            for (let d = 1; d <= daysInMonth; d++) {
                const dateKey = y + '-' + pad(m) + '-' + pad(d);
                const entries = byDate[dateKey] || [];
                const count = entries.length;
                const hasCount = count > 0;
                const cls = 'calendar-day' + (hasCount ? ' has-count' : '');
                html += '<div class="' + cls + '" data-date="' + dateKey + '" data-count="' + count + '">';
                html += '<span class="calendar-day-num">' + d + '</span>';
                html += '<span class="calendar-day-count">' + (count || '') + '</span>';
                html += '</div>';
            }

            const totalCells = 7 * 6;
            const filled = emptyCells + daysInMonth;
            for (let i = filled; i < totalCells; i++) html += '<div class="calendar-day calendar-day-empty"></div>';

            gridEl.innerHTML = html;

            gridEl.querySelectorAll('.calendar-day.has-count').forEach(cell => {
                cell.addEventListener('click', () => {
                    const dateKey = cell.dataset.date;
                    const entries = byDate[dateKey] || [];
                    if (!dayDetailEl) return;
                    dayDetailEl.style.display = 'block';
                    dayDetailEl.innerHTML = '<h4>' + dateKey + ' · ' + entries.length + '개</h4>' +
                        entries.map(v => `
                            <a href="${v.url}" target="_blank" class="video" style="text-decoration:none;color:inherit;display:block;margin-bottom:8px;">
                                <div class="info">
                                    <div class="title">${escapeHtml(v.title)}</div>
                                    <div class="channel">${escapeHtml(v.channel)}</div>
                                </div>
                            </a>
                        `).join('');
                });
            });
        }

        function renderSubscriptions() {
            const el = document.getElementById('subscriptions-content');
            const btn = document.getElementById('btn-subscription-sort');
            const sub = historyData.subscriptions;
            if (!sub) {
                el.innerHTML = '<div class="empty">구독 채널 정보를 불러오는 중...</div>';
                return;
            }
            const total = sub.total_count || 0;
            let channels = sub.channels || [];
            if (total === 0 && channels.length === 0) {
                el.innerHTML = '<div class="empty">구독 채널이 없거나 쿠키를 확인해 주세요.</div>';
                return;
            }
            if (subscriptionSortBy === 'subscribers') {
                channels = [...channels].sort((a, b) => (b.subscriber_count || 0) - (a.subscriber_count || 0));
                if (btn) btn.textContent = '기본 순';
            } else {
                if (btn) btn.textContent = '구독자 순';
            }
            const sortLabel = subscriptionSortBy === 'subscribers' ? '구독자 많은 순' : '한글·영어·특수문자 순';
            el.innerHTML = `
                <div class="stat-card" style="margin-bottom:16px; text-align:left;">
                    <div class="value">${total}</div>
                    <div class="label">구독 중인 채널 (${sortLabel})</div>
                </div>
                <div class="channel-list" style="display:grid; gap:8px; text-align:left;">
                    ${channels.map(c => {
                        const url = c.channel_url || '#';
                        const thumb = c.thumbnail || '';
                        const name = escapeHtml(c.channel_name || '');
                        const handle = c.handle ? escapeHtml(c.handle) : '';
                        const subs = c.subscriber_count_text ? escapeHtml(c.subscriber_count_text) : '';
                        const meta = [handle, subs].filter(Boolean).join(' · ');
                        const desc = c.description_snippet ? escapeHtml(c.description_snippet).substring(0, 80) : '';
                        return `
                        <a href="${url}" target="_blank" class="video channel-item" style="text-decoration:none; color:inherit; cursor:pointer;">
                            <img src="${thumb}" alt="" style="width:56px; height:56px; object-fit:cover; border-radius:50%; flex-shrink:0;">
                            <div class="info" style="flex:1; min-width:0;">
                                <div class="title">${name}</div>
                                ${meta ? `<div class="channel-meta" style="color:var(--ha-text-secondary); font-size:0.85em;">${meta}</div>` : ''}
                                ${desc ? `<div class="channel-desc" style="color:var(--ha-text-secondary); font-size:0.8em; margin-top:2px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">${desc}</div>` : ''}
                            </div>
                        </a>
                    `}).join('')}
                </div>
            `;
        }

        function renderMonthlySubscriptions() {
            const el = document.getElementById('monthly-subs-content');
            const changes = historyData.monthly_subscription_changes || {};
            const entries = Object.entries(changes);
            if (entries.length === 0) {
                el.innerHTML = '<div class="empty">월별 구독 변경 내역이 없습니다. 구독 추가/해지 시 자동 기록됩니다.</div>';
                return;
            }
            el.innerHTML = entries.map(([month, data]) => {
                const added = data.added || [];
                const removed = data.removed || [];
                return `
                <div class="date-group" style="margin-bottom:20px;">
                    <div class="date-header">
                        <span class="date">${month}</span>
                        <span class="count">+${added.length} / -${removed.length}</span>
                        <span class="chevron">▼</span>
                    </div>
                    <div class="date-content">
                        ${added.length ? `
                        <div style="margin-bottom:12px;">
                            <div style="font-size:0.85rem; color:#4caf50; margin-bottom:6px;">신규 구독 (${added.length})</div>
                            ${added.map(c => `<div class="video" style="cursor:default; padding:8px 12px;"><div class="info"><div class="title">${escapeHtml(c)}</div></div></div>`).join('')}
                        </div>
                        ` : ''}
                        ${removed.length ? `
                        <div>
                            <div style="font-size:0.85rem; color:#f44336; margin-bottom:6px;">구독 취소 (${removed.length})</div>
                            ${removed.map(c => `<div class="video" style="cursor:default; padding:8px 12px; opacity:0.8;"><div class="info"><div class="title">${escapeHtml(c)}</div></div></div>`).join('')}
                        </div>
                        ` : ''}
                    </div>
                </div>
                `;
            }).join('');

            el.querySelectorAll('.date-header').forEach(h => {
                h.addEventListener('click', () => h.closest('.date-group').classList.toggle('collapsed'));
            });
        }

        function updateRecommendedCooldown() {
            const btn = document.getElementById('btn-refresh-recommended');
            const span = document.getElementById('recommended-cooldown');
            if (!btn || !span) return;
            const now = Math.floor(Date.now() / 1000);
            const availableAt = historyData.recommended_refresh_available_at || 0;
            const remaining = availableAt - now;
            if (remaining > 0) {
                btn.disabled = true;
                const mins = Math.floor(remaining / 60);
                const secs = remaining % 60;
                span.textContent = mins > 0 ? mins + '분 후 사용 가능' : secs + '초 후 사용 가능';
            } else {
                btn.disabled = false;
                span.textContent = '';
            }
        }

        async function refreshRecommended() {
            const btn = document.getElementById('btn-refresh-recommended');
            if (btn && btn.disabled) return;
            try {
                const base = window.location.pathname.endsWith('/') ? window.location.pathname : window.location.pathname + '/';
                const r = await fetch(base + 'api/refresh/recommended', { method: 'POST' });
                const data = await r.json();
                if (r.ok && data.status === 'ok') {
                    historyData.recommended = data.recommended || [];
                    historyData.recommended_refresh_available_at = data.next_refresh_at || (Math.floor(Date.now()/1000) + 600);
                    historyData.recommended_refresh_retry_after = 600;
                    renderRecommended();
                    updateRecommendedCooldown();
                } else if (data.error === 'cooldown') {
                    historyData.recommended_refresh_available_at = Math.floor(Date.now()/1000) + (data.retry_after || 600);
                    historyData.recommended_refresh_retry_after = data.retry_after || 600;
                    updateRecommendedCooldown();
                }
            } catch (e) {
                console.error('Refresh failed:', e);
            }
        }

        function renderRecommended() {
            const el = document.getElementById('recommended-content');
            const videos = historyData.recommended || [];
            updateRecommendedCooldown();
            if (!videos.length) {
                el.innerHTML = '<div class="empty">추천 영상 정보를 불러오는 중...</div>';
                return;
            }
            el.innerHTML = videos.map(v => `
                <a href="${v.url || '#'}" target="_blank" class="video" style="text-decoration:none; color:inherit; display:block; margin-bottom:12px;">
                    <img src="${v.thumbnail || ''}" alt="">
                    <div class="info">
                        <div class="title">${escapeHtml(v.title)}</div>
                        <div class="channel">${escapeHtml(v.channel)}</div>
                        <div class="meta">${v.duration || ''}</div>
                    </div>
                </a>
            `).join('');
        }

        function escapeHtml(s) {
            const d = document.createElement('div');
            d.textContent = s || '';
            return d.innerHTML;
        }

        function formatTime(iso) {
            if (!iso) return '';
            try {
                const d = new Date(iso);
                return d.toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit' });
            } catch { return ''; }
        }

        document.querySelectorAll('.tab').forEach(tab => {
            tab.addEventListener('click', () => {
                if (!tab.dataset.tab) return;
                document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
                document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
                tab.classList.add('active');
                document.getElementById('panel-' + tab.dataset.tab).classList.add('active');
            });
        });

        document.getElementById('btn-refresh-recommended')?.addEventListener('click', refreshRecommended);
        document.getElementById('btn-subscription-sort')?.addEventListener('click', async () => {
            subscriptionSortBy = subscriptionSortBy === 'default' ? 'subscribers' : 'default';
            const el = document.getElementById('subscriptions-content');
            if (el) el.innerHTML = '<div class="empty">새로고침 중...</div>';
            await load();
        });
        load();
        setInterval(load, 60000);
        setInterval(updateRecommendedCooldown, 1000);
    </script>
</body>
</html>"""


def main() -> None:
    """진입점: HTTP 서버 시작 + fetch_loop 백그라운드 스레드."""
    global fetcher, _last_recommended_fetch, _last_subscriptions_fetch
    _LOGGER.info("[%s] YouTube Monitoring Add-on 시작 중...", "0%")
    opts = load_options()
    cookies_path = opts.get("cookies_path", "/config/youtube_cookies.txt")
    port = opts.get("port", 8765)
    interval = opts.get("scan_interval", 60)
    _LOGGER.info("[%s] 설정 로드 완료 | cookies_path=%s | port=%s | scan_interval=%ds", "10%", cookies_path, port, interval)

    fetcher = YouTubeHistoryFetcher(cookies_path)
    _LOGGER.info("[%s] 시청 기록 조회 중... (약 1분 소요 됩니다)", "20%")
    fetcher.fetch_history()
    time.sleep(2)
    cookies_ok = fetcher.cookies_valid
    n_history = len(fetcher.history_data or [])
    _LOGGER.info("[%s] 시청 기록 조회 완료 | 쿠키=%s | 최근 %d건", "30%", "유효" if cookies_ok else "무효", n_history)

    _LOGGER.info("[%s] 구독 채널 조회 중...", "40%")
    fetcher.fetch_subscriptions()
    _last_subscriptions_fetch = time.time()
    sub_data = fetcher.subscriptions_data
    n_subs = len(sub_data.get("channels", [])) if sub_data else 0
    if sub_data and sub_data.get("channels"):
        with _subs_lock:
            update_subscription_changes(sub_data["channels"])
    _LOGGER.info("[%s] 구독 채널 조회 완료 | %d개 채널 반영", "55%", n_subs)

    if opts.get("fetch_recommended", True):
        time.sleep(2)
        _LOGGER.info("[%s] 추천 영상 조회 중...", "65%")
        fetcher.fetch_recommended()
        _last_recommended_fetch = time.time()
        n_rec = len(fetcher.recommended_data or [])
        _LOGGER.info("[%s] 추천 영상 조회 완료 | %d건", "75%", n_rec)
    else:
        _LOGGER.info("[%s] 추천 영상 조회 비활성화(건너뜀)", "70%")

    _LOGGER.info("[%s] 백그라운드 갱신 스레드 시작 (interval=%ds)", "85%", interval)
    thread = threading.Thread(target=fetch_loop, daemon=True)
    thread.start()

    server = HTTPServer(("0.0.0.0", port), YouTubeMonitoringHandler)
    _LOGGER.info("[%s] HTTP 서버 대기 중 | port=%s", "95%", port)
    _LOGGER.info("[%s] 에드온 정상 실행 | 쿠키=%s | http://0.0.0.0:%s", "100%", "유효" if cookies_ok else "무효", port)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _LOGGER.info("Shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
