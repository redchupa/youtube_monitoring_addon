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

from app.cookie_manager import CookieFileError
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
    repair_mass_removals,
)
from app.mqtt_publisher import MqttPublisher, get_supervisor_mqtt

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
mqtt_pub: MqttPublisher | None = None
_history_lock = threading.Lock()
_subs_lock = threading.Lock()
_last_seen_video_id: str | None = None
_recent_added: dict[str, float] = {}  # video_id -> timestamp (5분 내 중복 방지)
_last_recommended_fetch: float = 0  # 추천 영상 마지막 fetch 시각
_last_manual_refresh_recommended: float = 0  # 수동 새로고침 마지막 시각 (10분 쿨다운)
_last_subscriptions_fetch: float = 0  # 구독 채널 마지막 fetch (2분 간격, 429 방지)
_last_published_cookies_valid: bool | None = None  # MQTT 중복 발행 방지

REFRESH_COOLDOWN_SEC = 600  # 수동 새로고침 쿨다운 10분

# 쿠키 자동 유지
COOKIE_ROTATE_INTERVAL = 600  # 정상일 때 약 10분마다 쿠키 회전 (브라우저와 비슷한 주기)
COOKIE_RECOVERY_RETRY_SEC = 1800  # 만료 상태에서 자동 복구 재시도 간격
COOKIE_NOTIFICATION_ID = "youtube_monitoring_cookie"
INGRESS_PROXY_IP = "172.30.32.2"  # HA Supervisor Ingress 프록시 주소
MAX_COOKIE_UPLOAD_BYTES = 1_000_000

_cookie_invalid_since: float | None = None
_last_cookie_recovery: float = 0
_cookie_notified = False
_cookie_state_lock = threading.Lock()


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
        "notify_cookie_invalid": _parse_bool(os.environ.get("NOTIFY_COOKIE_INVALID", "true")),
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
            if "notify_cookie_invalid" in opts:
                defaults["notify_cookie_invalid"] = _parse_bool(opts["notify_cookie_invalid"])
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


def _ha_service(service: str, data: dict) -> bool:
    """Supervisor 경유 HA 서비스 호출 (persistent_notification 등). HA 밖에서는 False."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return False
    try:
        import requests  # 지연 import (fetcher와 동일 의존성)

        r = requests.post(
            f"http://supervisor/core/api/services/{service}",
            headers={"Authorization": f"Bearer {token}"},
            json=data,
            timeout=10,
        )
        if r.status_code >= 300:
            _LOGGER.warning("[알림] HA 서비스 %s 실패: HTTP %s", service, r.status_code)
            return False
        return True
    except Exception as err:
        _LOGGER.warning("[알림] HA 서비스 %s 호출 오류: %s", service, err)
        return False


def _notify_cookie_invalid(problem: str) -> None:
    if not load_options().get("notify_cookie_invalid", True):
        return
    if problem == "missing":
        reason = "쿠키 파일이 없거나 읽을 수 없습니다."
    else:
        reason = "YouTube 로그인 세션이 만료됐고, 자동 갱신으로 복구하지 못했습니다."
    _ha_service("persistent_notification/create", {
        "notification_id": COOKIE_NOTIFICATION_ID,
        "title": "YouTube Monitoring: 쿠키 갱신 필요",
        "message": (
            f"{reason}\n\n"
            "에드온 웹 UI(YouTube 시청 기록 패널)의 **쿠키 업데이트** 버튼으로 새 쿠키를 넣어 주세요.\n\n"
            "오래 유지되게 하려면: 브라우저 **시크릿 창**에서 YouTube 로그인 → "
            "'Get cookies.txt LOCALLY'로 내보내기 → **시크릿 창을 바로 닫기**."
        ),
    })


def _dismiss_cookie_notification() -> None:
    _ha_service("persistent_notification/dismiss", {"notification_id": COOKIE_NOTIFICATION_ID})


def _reset_cookie_state() -> None:
    """쿠키가 다시 유효해졌을 때 상태/알림 정리."""
    global _cookie_invalid_since, _cookie_notified, _last_cookie_recovery
    global _last_subscriptions_fetch, _last_recommended_fetch
    with _cookie_state_lock:
        was_invalid = _cookie_invalid_since is not None
        notified = _cookie_notified
        _cookie_invalid_since = None
        _cookie_notified = False
        _last_cookie_recovery = 0
    if was_invalid:
        # 만료 동안 비어 있던 구독/추천을 다음 루프에서 바로 다시 조회
        _last_subscriptions_fetch = 0
        _last_recommended_fetch = 0
        _LOGGER.info("[쿠키] 다시 유효함")
    if notified:
        _dismiss_cookie_notification()


def maintain_cookies() -> None:
    """
    쿠키 자동 유지 (fetch_history 직후 호출).
    - 유효: 약 10분마다 Google 쿠키 회전 → 새 쿠키를 파일에 저장 (브라우저처럼 세션 유지).
    - 만료: 회전으로 복구를 시도하고 다시 조회. 그래도 실패하면 HA 알림 1회.
    """
    global _cookie_invalid_since, _last_cookie_recovery, _cookie_notified
    if not fetcher:
        return
    now = time.time()

    if fetcher.cookies_valid:
        if _cookie_invalid_since is not None or _cookie_notified:
            _reset_cookie_state()
        if now - fetcher.cookies.last_rotate_at >= COOKIE_ROTATE_INTERVAL:
            fetcher.rotate_cookies()
        return

    problem = fetcher.cookie_problem()
    if problem is None:
        return  # 네트워크 오류 등 판단 불가 → 다음 루프에서 다시 확인
    with _cookie_state_lock:
        if _cookie_invalid_since is None:
            _cookie_invalid_since = now
        try_recovery = problem == "logged_out" and now - _last_cookie_recovery >= COOKIE_RECOVERY_RETRY_SEC
        if try_recovery:
            _last_cookie_recovery = now

    if try_recovery:
        _LOGGER.info("[쿠키] 만료 감지 → 자동 갱신으로 복구 시도")
        result = fetcher.rotate_cookies(force=True)
        if result is not False:
            time.sleep(2)
            fetcher.fetch_history()
            if fetcher.cookies_valid:
                _LOGGER.info("[쿠키] 자동 복구 성공")
                _reset_cookie_state()
                return
        _LOGGER.warning("[쿠키] 자동 복구 실패 | 웹 UI의 '쿠키 업데이트'로 새 쿠키를 넣어 주세요")

    with _cookie_state_lock:
        should_notify = not _cookie_notified
        _cookie_notified = True
    if should_notify:
        _notify_cookie_invalid(problem)


def fetch_loop() -> None:
    """
    백그라운드 루프: YouTube 시청 기록 폴링 + video_id 변경 시 저장.
    시청 기록: 매 루프. 구독 채널: 2분 간격. 추천 영상: 60초 간격 (옵션).
    429 rate limit 방지를 위해 요청 간격 조절.
    """
    global fetcher, _last_seen_video_id, _recent_added, _last_recommended_fetch, _last_subscriptions_fetch, _last_published_cookies_valid
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
            try:
                maintain_cookies()
            except Exception as err:
                _LOGGER.error("[쿠키] 자동 유지 오류: %s", err)

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
                    # MQTT 추천 영상 발행
                    if mqtt_pub and mqtt_pub.is_connected():
                        mqtt_pub.publish_recommended(fetcher.recommended_data or [])

            # 쿠키 유효성 변화 시에만 MQTT 발행 (트래픽 최소화)
            if mqtt_pub and mqtt_pub.is_connected():
                if fetcher.cookies_valid != _last_published_cookies_valid:
                    mqtt_pub.publish_cookies_valid(fetcher.cookies_valid)
                    _last_published_cookies_valid = fetcher.cookies_valid

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
                        # 최근 시청 영상 변경 → MQTT 발행
                        if mqtt_pub and mqtt_pub.is_connected():
                            mqtt_pub.publish_recent_watched(videos)
                    _recent_added = {k: v for k, v in _recent_added.items() if now - v < duplicate_sec * 2}

            if not fetcher.cookies_valid:
                _LOGGER.warning("[폴링] 쿠키 무효 | 시청 기록/구독 조회 불가 (자동 복구 실패 시 웹 UI '쿠키 업데이트' 사용)")
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
        if path == "/api/cookies":
            self._handle_cookie_upload()
            return
        self.send_response(404)
        self.end_headers()

    def _is_trusted_request(self) -> bool:
        """쿠키 교체는 HA Ingress(로그인된 HA 사용자) 경유만 허용. HA 밖(로컬 실행)은 허용."""
        if not os.environ.get("SUPERVISOR_TOKEN"):
            return True
        return self.client_address[0] == INGRESS_PROXY_IP

    def _handle_cookie_upload(self) -> None:
        """웹 UI에서 붙여넣은/선택한 cookies.txt로 쿠키 교체 → 즉시 검증."""
        global fetcher
        if not self._is_trusted_request():
            self.send_json({"error": "forbidden", "message": "HA 웹 UI(Ingress)에서만 쿠키를 바꿀 수 있습니다."}, status=403)
            return
        if not fetcher:
            self.send_json({"error": "fetcher not ready"}, status=503)
            return
        try:
            content_len = int(self.headers.get("Content-Length", 0))
        except ValueError:
            content_len = 0
        if content_len <= 0 or content_len > MAX_COOKIE_UPLOAD_BYTES:
            self.send_json({"error": "invalid_size", "message": "쿠키 내용이 비었거나 너무 큽니다 (최대 1MB)."}, status=400)
            return
        raw = self.rfile.read(content_len)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            self.send_json({"error": "invalid_encoding", "message": "UTF-8 텍스트 파일이 아닙니다."}, status=400)
            return
        if "json" in (self.headers.get("Content-Type") or ""):
            try:
                text = str(json.loads(text).get("cookies", ""))
            except (json.JSONDecodeError, AttributeError):
                self.send_json({"error": "invalid_json"}, status=400)
                return
        try:
            count = fetcher.cookies.replace_from_text(text)
        except CookieFileError as err:
            self.send_json({"error": "invalid_cookies", "message": str(err)}, status=400)
            return
        except OSError as err:
            _LOGGER.error("[쿠키] 저장 실패: %s", err)
            self.send_json({"error": "save_failed", "message": f"쿠키 파일 저장 실패: {err}"}, status=500)
            return

        _LOGGER.info("[쿠키] 웹 UI에서 새 쿠키 등록 (%d개) → 검증 중", count)
        fetcher.logged_out_detected = False
        fetcher.cookies_valid = False
        fetcher.fetch_history()
        if fetcher.cookies_valid:
            _reset_cookie_state()
            fetcher.rotate_cookies(force=True)
            message = "새 쿠키가 적용됐습니다. 이제 자동으로 갱신됩니다."
        elif fetcher.logged_out_detected:
            message = "쿠키는 저장됐지만 YouTube가 로그아웃 상태로 응답합니다. 로그인한 상태에서 다시 내보내 주세요."
        else:
            message = "쿠키는 저장됐지만 아직 확인하지 못했습니다. 잠시 후 상태를 확인해 주세요."
        self.send_json({
            "status": "ok",
            "cookie_count": count,
            "cookies_valid": fetcher.cookies_valid,
            "message": message,
        })

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
                "cookie_problem": fetcher.cookie_problem() if fetcher else "missing",
                "cookie_maintenance": fetcher.cookies.status() if fetcher else None,
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
            "cookie_maintenance": fetcher.cookies.status() if fetcher else None,
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
        .cookie-btn { margin-left: 8px; padding: 6px 12px; border-radius: 20px; border: 1px solid #444; background: transparent; color: var(--ha-text-secondary); font-size: 0.8rem; cursor: pointer; }
        .cookie-btn:hover { background: #2a2a2a; color: var(--ha-text); }
        .cookie-btn.attention { border-color: #f44336; color: #f44336; }
        .modal-backdrop { position: fixed; inset: 0; background: rgba(0,0,0,0.6); display: none; align-items: center; justify-content: center; z-index: 100; padding: 16px; }
        .modal-backdrop.open { display: flex; }
        .modal { background: var(--ha-card); border-radius: 12px; padding: 20px; width: 100%; max-width: 560px; max-height: 90vh; overflow-y: auto; }
        .modal h3 { margin: 0 0 8px 0; font-size: 1.1rem; }
        .modal ol { margin: 8px 0 12px 0; padding-left: 20px; font-size: 0.85rem; color: var(--ha-text-secondary); line-height: 1.6; }
        .modal textarea { width: 100%; min-height: 140px; box-sizing: border-box; background: #111; color: var(--ha-text); border: 1px solid #444; border-radius: 8px; padding: 8px; font-family: monospace; font-size: 0.75rem; }
        .modal-actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 12px; flex-wrap: wrap; align-items: center; }
        .modal-actions input[type=file] { margin-right: auto; font-size: 0.8rem; color: var(--ha-text-secondary); max-width: 100%; }
        .modal-msg { margin-top: 10px; font-size: 0.85rem; min-height: 1.2em; }
        .modal-msg.ok { color: #4caf50; }
        .modal-msg.err { color: #f44336; }
        .modal-meta { font-size: 0.75rem; color: var(--ha-text-secondary); margin-top: 8px; }
    </style>
</head>
<body>
    <div class="container">
        <h1 style="display:flex; align-items:center; flex-wrap:wrap;">
            <span class="icon">▶</span> YouTube 시청 기록
            <span id="cookie-status" class="cookie-status loading">확인 중...</span>
            <button id="btn-cookie-update" class="cookie-btn" type="button">쿠키 업데이트</button>
        </h1>
        <div id="cookie-modal" class="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="cookie-modal-title">
            <div class="modal">
                <h3 id="cookie-modal-title">YouTube 쿠키 업데이트</h3>
                <div style="font-size:0.85rem; color:var(--ha-text-secondary);">한 번 넣으면 에드온이 약 10분마다 스스로 갱신합니다. 오래 유지하려면 아래 순서를 지켜 주세요.</div>
                <ol>
                    <li>브라우저 <b>시크릿 창</b>을 열고 YouTube에 로그인</li>
                    <li>확장 프로그램 <b>Get cookies.txt LOCALLY</b>로 youtube.com 쿠키 내보내기</li>
                    <li><b>시크릿 창을 바로 닫기</b> (같은 세션을 브라우저가 계속 쓰면 쿠키가 빨리 무효화됩니다)</li>
                    <li>파일을 선택하거나 내용을 아래에 붙여넣고 저장</li>
                </ol>
                <textarea id="cookie-text" placeholder="# Netscape HTTP Cookie File&#10;.youtube.com	TRUE	/	TRUE	..." spellcheck="false" autocomplete="off"></textarea>
                <div class="modal-actions">
                    <input type="file" id="cookie-file" accept=".txt,text/plain">
                    <button id="btn-cookie-cancel" class="tab" type="button">닫기</button>
                    <button id="btn-cookie-save" class="tab active" type="button">저장 및 확인</button>
                </div>
                <div id="cookie-msg" class="modal-msg"></div>
                <div id="cookie-meta" class="modal-meta"></div>
            </div>
        </div>
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
            const btn = document.getElementById('btn-cookie-update');
            if (btn) btn.classList.toggle('attention', valid === false);
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
                status.textContent = '쿠키가 유효하지 않습니다. 오른쪽 위 [쿠키 업데이트] 버튼으로 새 쿠키를 넣어 주세요.';
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
        function apiBase() {
            return window.location.pathname.endsWith('/') ? window.location.pathname : window.location.pathname + '/';
        }

        function renderCookieMeta() {
            const el = document.getElementById('cookie-meta');
            const m = historyData.cookie_maintenance;
            if (!el || !m) return;
            const fmt = ts => ts ? new Date(ts * 1000).toLocaleString('ko-KR') : '-';
            const ok = m.last_rotate_ok === true ? '성공' : (m.last_rotate_ok === false ? '거부됨' : '-');
            el.textContent = '마지막 자동 갱신: ' + fmt(m.last_rotate_at) + ' (' + ok + ') · 마지막 저장: ' + fmt(m.last_saved_at);
        }

        function openCookieModal() {
            document.getElementById('cookie-msg').textContent = '';
            document.getElementById('cookie-msg').className = 'modal-msg';
            renderCookieMeta();
            document.getElementById('cookie-modal').classList.add('open');
        }

        function closeCookieModal() {
            document.getElementById('cookie-modal').classList.remove('open');
            document.getElementById('cookie-text').value = '';
            document.getElementById('cookie-file').value = '';
        }

        async function saveCookies() {
            const msg = document.getElementById('cookie-msg');
            const btn = document.getElementById('btn-cookie-save');
            const text = document.getElementById('cookie-text').value;
            if (!text.trim()) {
                msg.className = 'modal-msg err';
                msg.textContent = '쿠키 파일을 선택하거나 내용을 붙여넣어 주세요.';
                return;
            }
            btn.disabled = true;
            msg.className = 'modal-msg';
            msg.textContent = '저장 후 YouTube 로그인 상태를 확인하는 중... (최대 1분)';
            try {
                const r = await fetch(apiBase() + 'api/cookies', {
                    method: 'POST',
                    headers: { 'Content-Type': 'text/plain; charset=utf-8' },
                    body: text,
                });
                const data = await r.json().catch(() => ({}));
                if (!r.ok) {
                    msg.className = 'modal-msg err';
                    msg.textContent = data.message || ('저장 실패 (HTTP ' + r.status + ')');
                    return;
                }
                msg.className = 'modal-msg ' + (data.cookies_valid ? 'ok' : 'err');
                msg.textContent = data.message + ' (' + data.cookie_count + '개 쿠키)';
                document.getElementById('cookie-text').value = '';
                document.getElementById('cookie-file').value = '';
                await load();
                renderCookieMeta();
            } catch (e) {
                msg.className = 'modal-msg err';
                msg.textContent = '오류: ' + e.message;
            } finally {
                btn.disabled = false;
            }
        }

        document.getElementById('btn-cookie-update').addEventListener('click', openCookieModal);
        document.getElementById('cookie-status').addEventListener('click', openCookieModal);
        document.getElementById('cookie-status').style.cursor = 'pointer';
        document.getElementById('btn-cookie-cancel').addEventListener('click', closeCookieModal);
        document.getElementById('btn-cookie-save').addEventListener('click', saveCookies);
        document.getElementById('cookie-modal').addEventListener('click', e => { if (e.target.id === 'cookie-modal') closeCookieModal(); });
        document.addEventListener('keydown', e => { if (e.key === 'Escape') closeCookieModal(); });
        document.getElementById('cookie-file').addEventListener('change', e => {
            const f = e.target.files && e.target.files[0];
            if (!f) return;
            const reader = new FileReader();
            reader.onload = () => { document.getElementById('cookie-text').value = reader.result || ''; };
            reader.readAsText(f);
        });

        load();
        setInterval(load, 60000);
        setInterval(updateRecommendedCooldown, 1000);
    </script>
</body>
</html>"""


def init_mqtt() -> None:
    """HA Supervisor에서 MQTT 정보 받아 연결, Discovery 발행."""
    global mqtt_pub
    info = get_supervisor_mqtt()
    if not info:
        _LOGGER.info("MQTT 통합이 HA에 추가되지 않음 (선택사항, 건너뜀)")
        return
    mqtt_pub = MqttPublisher(info["host"], info["port"], info["username"], info["password"])
    if mqtt_pub.connect():
        time.sleep(1)  # 연결 안정화
        if mqtt_pub.is_connected():
            mqtt_pub.publish_discovery()
            _LOGGER.info("MQTT Discovery 발행 완료 - HA에 sensor 자동 등록됨")
        else:
            _LOGGER.warning("MQTT 연결되지 않음 - 발행 보류")
    else:
        mqtt_pub = None


def main() -> None:
    """진입점: HTTP 서버 시작 + fetch_loop 백그라운드 스레드."""
    global fetcher, _last_recommended_fetch, _last_subscriptions_fetch, _last_published_cookies_valid
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

    try:
        with _subs_lock:
            repair_mass_removals()
    except Exception as err:
        _LOGGER.warning("[구독] 오기록 복구 실패: %s", err)
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

    # MQTT 초기화 (HA에 MQTT 통합 있을 때만)
    _LOGGER.info("[%s] MQTT 초기화 중...", "80%")
    init_mqtt()
    if mqtt_pub and mqtt_pub.is_connected():
        mqtt_pub.publish_recommended(fetcher.recommended_data or [])
        mqtt_pub.publish_cookies_valid(fetcher.cookies_valid)
        mqtt_pub.publish_recent_watched(fetcher.history_data or [])
        _last_published_cookies_valid = fetcher.cookies_valid

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
