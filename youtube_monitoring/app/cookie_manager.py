"""
YouTube 쿠키 자동 유지 관리.

왜 필요한가: 예전에는 조회할 때마다 쿠키 파일을 새로 읽고, YouTube/Google이
응답(Set-Cookie)으로 보내 준 갱신 쿠키(__Secure-*PSIDTS, SIDCC 등)는 버렸다.
그래서 처음 넣은 쿠키가 낡으면 그대로 만료됐고, 사용자가 브라우저에서 다시
내보내야 했다.

여기서는 브라우저가 하는 일을 그대로 흉내 낸다.
  1. 하나의 세션(쿠키 jar)을 계속 유지하고, 응답으로 받은 새 쿠키를 파일에 저장.
  2. 약 10분마다 Google 쿠키 회전 엔드포인트(RotateCookies)를 호출해
     __Secure-1PSIDTS / __Secure-3PSIDTS를 새 값으로 받아 둔다.
  3. 사용자가 파일을 직접 바꾸면(mtime 변경) 자동으로 다시 읽는다.

아이디/비밀번호 자동 로그인은 쓰지 않는다. Google 로그인은 봇 탐지·CAPTCHA·
2단계 인증 때문에 자동화가 사실상 불가능하고, 시도 자체가 계정 보안 경고나
잠금을 부를 수 있다.
"""
from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
from http.cookiejar import LoadError, MozillaCookieJar
from typing import Any

import requests

_LOGGER = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
NETSCAPE_HEADER = "# Netscape HTTP Cookie File\n"

ROTATE_PAGE_URL = "https://accounts.youtube.com/RotateCookiesPage?origin=https://www.youtube.com&yt_pid=1"
ROTATE_URL = "https://accounts.youtube.com/RotateCookies"
ROTATE_BODY = '[000,"-0000000000000000000"]'
ROTATE_MIN_GAP_SEC = 60  # 회전 요청 최소 간격 (연속 호출 방지)

# 로그인 세션을 나타내는 쿠키. 이 중 하나는 있어야 로그인 쿠키로 본다.
AUTH_COOKIE_NAMES = ("SID", "__Secure-1PSID", "__Secure-3PSID", "SAPISID", "LOGIN_INFO")
ALLOWED_DOMAIN_SUFFIXES = ("youtube.com", "google.com")
MAX_UPLOAD_BYTES = 1_000_000


class CookieFileError(ValueError):
    """업로드/입력된 쿠키 텍스트가 쓸 수 없는 형식일 때."""


def _domain_ok(domain: str) -> bool:
    d = (domain or "").lstrip(".").lower()
    return any(d == s or d.endswith("." + s) for s in ALLOWED_DOMAIN_SUFFIXES)


def _snapshot(jar: MozillaCookieJar) -> frozenset[tuple[str, str, str, str | None]]:
    return frozenset((c.domain, c.path, c.name, c.value) for c in jar)


def _auth_names(snap: frozenset | None) -> set[str]:
    return {name for (_d, _p, name, _v) in (snap or ()) if name in AUTH_COOKIE_NAMES}


def parse_cookie_text(text: str) -> MozillaCookieJar:
    """Netscape cookies.txt 텍스트를 검증하고 jar로 변환. 실패 시 CookieFileError."""
    if not isinstance(text, str) or not text.strip():
        raise CookieFileError("쿠키 내용이 비어 있습니다.")
    if len(text.encode("utf-8")) > MAX_UPLOAD_BYTES:
        raise CookieFileError("쿠키 파일이 너무 큽니다 (최대 1MB).")
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    first = text.split("\n", 1)[0]
    if "HTTP Cookie File" not in first:
        text = NETSCAPE_HEADER + text

    for line in text.split("\n")[1:]:
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif not line.strip() or line.lstrip().startswith(("#", "$")):
            continue
        if len(line.split("\t")) != 7:
            raise CookieFileError(
                "Netscape 형식(cookies.txt)이 아닙니다. 'Get cookies.txt LOCALLY' 확장으로 내보낸 파일을 사용하세요."
            )

    fd, tmp = tempfile.mkstemp(suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        jar = MozillaCookieJar(tmp)
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
        except (LoadError, OSError, ValueError) as err:
            raise CookieFileError(
                "Netscape 형식(cookies.txt)이 아닙니다. 'Get cookies.txt LOCALLY' 확장으로 내보낸 파일을 사용하세요."
            ) from err
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

    yt = [c for c in jar if _domain_ok(c.domain)]
    if not yt:
        raise CookieFileError("youtube.com 쿠키가 없습니다. YouTube 페이지에서 쿠키를 내보내 주세요.")
    if not any(c.name in AUTH_COOKIE_NAMES for c in yt):
        raise CookieFileError("로그인 쿠키(SID 등)가 없습니다. YouTube에 로그인한 상태에서 내보내 주세요.")
    # 다른 사이트 쿠키는 저장하지 않는다.
    for c in list(jar):
        if not _domain_ok(c.domain):
            jar.clear(c.domain, c.path, c.name)
    return jar


class CookieManager:
    """쿠키 파일 ↔ 지속 세션 동기화, 갱신 쿠키 저장, 쿠키 회전."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.lock = threading.RLock()
        self._session: requests.Session | None = None
        self._jar: MozillaCookieJar | None = None
        self._loaded_mtime: int | None = None
        self._saved_snapshot: frozenset | None = None
        self._load_error: str | None = None
        self.last_rotate_at: float = 0.0
        self.last_rotate_ok: bool | None = None
        self.last_rotate_message: str = ""
        self.last_saved_at: float = 0.0

    # ── 로드 / 저장 ─────────────────────────────────────
    def _file_mtime(self) -> int | None:
        try:
            return os.stat(self.path).st_mtime_ns
        except OSError:
            return None

    def _fail(self, mtime: int | None, message: str) -> bool:
        if message != self._load_error:  # 같은 오류는 상태가 바뀔 때만 한 번 기록
            _LOGGER.error(message)
            self._load_error = message
        self._session = self._jar = None
        self._loaded_mtime = mtime
        return False

    def _load(self) -> bool:
        mtime = self._file_mtime()
        if mtime is None:
            return self._fail(None, f"Cookies file not found: {self.path}")
        jar = MozillaCookieJar(self.path)
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
        except Exception as err:  # LoadError, OSError, 깨진 줄 등
            return self._fail(mtime, f"Failed to load cookies: {err}")
        if len(jar) == 0:
            return self._fail(mtime, "Cookies file is empty")
        self._load_error = None

        session = requests.Session()
        session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-us,en;q=0.5",
        })
        session.cookies = jar  # type: ignore[assignment]
        self._session, self._jar = session, jar
        self._loaded_mtime = mtime
        self._saved_snapshot = _snapshot(jar)
        _LOGGER.info("[쿠키] 파일 로드: %d개 쿠키", len(jar))
        return True

    def session(self) -> requests.Session | None:
        """현재 세션. 파일이 바깥에서 바뀌었으면 다시 읽는다. 쓸 수 없으면 None."""
        with self.lock:
            mtime = self._file_mtime()
            if self._session is None or mtime != self._loaded_mtime:
                if mtime != self._loaded_mtime and self._loaded_mtime is not None and mtime is not None:
                    _LOGGER.info("[쿠키] 파일 변경 감지 → 다시 읽음")
                self._load()
            return self._session

    def save_if_changed(self) -> bool:
        """응답으로 받은 쿠키가 달라졌으면 파일에 원자적으로 저장."""
        with self.lock:
            if self._jar is None:
                return False
            snap = _snapshot(self._jar)
            if snap == self._saved_snapshot:
                return False
            lost = _auth_names(self._saved_snapshot) - _auth_names(snap)
            if lost:
                # YouTube가 로그아웃 응답과 함께 로그인 쿠키 삭제를 보낸 경우.
                # 일시적 오류일 수도 있고 복구(회전)에 필요하므로 파일은 지키고 메모리도 되돌린다.
                _LOGGER.warning("[쿠키] 서버가 로그인 쿠키 삭제 요청(%s) - 파일은 보존", ", ".join(sorted(lost)))
                self._load()
                return False
            self._write_jar(self._jar)
            self._saved_snapshot = snap
            return True

    def _write_jar(self, jar: MozillaCookieJar) -> None:
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".yt_cookies_", suffix=".tmp", dir=directory)
        os.close(fd)
        try:
            jar.save(tmp, ignore_discard=True, ignore_expires=True)
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        self._loaded_mtime = self._file_mtime()
        self.last_saved_at = time.time()
        _LOGGER.debug("[쿠키] 갱신된 쿠키 저장 (%d개)", len(jar))

    def replace_from_text(self, text: str) -> int:
        """새 cookies.txt 내용으로 교체 (기존 파일은 .bak으로 보관). 쿠키 개수 반환."""
        jar = parse_cookie_text(text)
        with self.lock:
            if os.path.exists(self.path):
                try:
                    with open(self.path, "rb") as src, open(self.path + ".bak", "wb") as dst:
                        dst.write(src.read())
                except OSError as err:
                    _LOGGER.warning("[쿠키] 백업 실패: %s", err)
            self._write_jar(jar)
            self._session = None  # 다음 session() 호출 때 새로 로드
            self._loaded_mtime = None
            self.last_rotate_at = 0.0
            self.last_rotate_ok = None
            self.last_rotate_message = ""
            self.session()
            return len(jar)

    # ── 회전 ───────────────────────────────────────────
    def has_auth_cookies(self) -> bool:
        with self.lock:
            return self._jar is not None and any(
                c.name in ("__Secure-1PSID", "__Secure-3PSID", "SID") for c in self._jar
            )

    def rotate(self, force: bool = False) -> bool | None:
        """
        Google 쿠키 회전 요청 (__Secure-*PSIDTS 갱신).
        Returns True=성공, False=거부(로그인 만료 가능성), None=건너뜀/일시적 오류.
        """
        with self.lock:
            now = time.time()
            if not force and now - self.last_rotate_at < ROTATE_MIN_GAP_SEC:
                return None
            session = self.session()
            if session is None or not self.has_auth_cookies():
                return None
            self.last_rotate_at = now
            try:
                # 브라우저처럼 회전 페이지를 먼저 열고(결과는 참고용), 회전 API를 호출.
                try:
                    session.get(ROTATE_PAGE_URL, timeout=10)
                except requests.exceptions.RequestException:
                    pass
                resp = session.post(
                    ROTATE_URL,
                    data=ROTATE_BODY,
                    headers={
                        "Content-Type": "application/json",
                        "Origin": "https://accounts.youtube.com",
                        "Referer": ROTATE_PAGE_URL,
                    },
                    timeout=10,
                )
            except requests.exceptions.RequestException as err:
                self.last_rotate_ok = None
                self.last_rotate_message = f"network: {err}"
                _LOGGER.debug("[쿠키] 회전 요청 실패(네트워크): %s", err)
                return None

            status = getattr(resp, "status_code", 0)
            changed = self.save_if_changed()
            if status == 200:
                self.last_rotate_ok = True
                self.last_rotate_message = "ok" + (" (updated)" if changed else "")
                _LOGGER.info("[쿠키] 자동 갱신 성공%s", " · 새 쿠키 저장" if changed else "")
                return True
            if status in (401, 403):
                self.last_rotate_ok = False
                self.last_rotate_message = f"HTTP {status}"
                _LOGGER.warning("[쿠키] 자동 갱신 거부 (HTTP %s) - 로그인 세션이 만료됐을 수 있음", status)
                return False
            self.last_rotate_ok = None
            self.last_rotate_message = f"HTTP {status}"
            _LOGGER.debug("[쿠키] 회전 응답 HTTP %s", status)
            return None

    def status(self) -> dict[str, Any]:
        with self.lock:
            return {
                "cookies_file_exists": os.path.exists(self.path),
                "cookie_count": len(self._jar) if self._jar is not None else 0,
                "last_rotate_at": int(self.last_rotate_at) or None,
                "last_rotate_ok": self.last_rotate_ok,
                "last_rotate_message": self.last_rotate_message,
                "last_saved_at": int(self.last_saved_at) or None,
            }
