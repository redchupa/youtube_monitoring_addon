"""쿠키 자동 유지 기능 테스트 (네트워크 없이). 실행: python -m unittest discover -s tests"""
from __future__ import annotations

import http.client
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.cookiejar import Cookie
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import cookie_manager  # noqa: E402
from app.cookie_manager import CookieFileError, CookieManager, parse_cookie_text  # noqa: E402
from app.fetcher import YouTubeHistoryFetcher, detect_logged_in  # noqa: E402

FAR = str(int(time.time()) + 365 * 86400)
COOKIES_TXT = "\n".join([
    "# Netscape HTTP Cookie File",
    f".youtube.com\tTRUE\t/\tTRUE\t{FAR}\t__Secure-1PSID\tpsid-value",
    f".youtube.com\tTRUE\t/\tTRUE\t{FAR}\t__Secure-1PSIDTS\tsidts-old",
    f"#HttpOnly_.youtube.com\tTRUE\t/\tFALSE\t{FAR}\tSID\tsid-value",
    f".youtube.com\tTRUE\t/\tTRUE\t{FAR}\tPREF\tf6=40000000",
    f".example.com\tTRUE\t/\tFALSE\t{FAR}\tother\tx",
    "",
])


def _cookie(name: str, value: str, domain: str = ".youtube.com") -> Cookie:
    return Cookie(0, name, value, None, False, domain, True, True, "/", True, True,
                  int(FAR), False, None, None, {})


class FakeResponse:
    def __init__(self, status: int = 200, text: str = "") -> None:
        self.status_code = status
        self.text = text

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests
            raise requests.exceptions.HTTPError(str(self.status_code))


class CookieTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "youtube_cookies.txt")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write_cookies(self, text: str = COOKIES_TXT) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text)


class ParseCookieTextTest(unittest.TestCase):
    def test_valid_file_keeps_only_youtube_google(self) -> None:
        jar = parse_cookie_text(COOKIES_TXT)
        names = {c.name for c in jar}
        self.assertIn("__Secure-1PSID", names)
        self.assertIn("SID", names)  # #HttpOnly_ 줄
        self.assertNotIn("other", names)

    def test_header_missing_and_crlf_bom_ok(self) -> None:
        body = "﻿" + "\r\n".join(COOKIES_TXT.split("\n")[1:])
        jar = parse_cookie_text(body)
        self.assertTrue(any(c.name == "__Secure-1PSID" for c in jar))

    def test_rejects_empty_garbage_and_logged_out(self) -> None:
        with self.assertRaises(CookieFileError):
            parse_cookie_text("   ")
        with self.assertRaises(CookieFileError):
            parse_cookie_text("not a cookie file at all")
        logged_out = f"# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t{FAR}\tPREF\tx\n"
        with self.assertRaises(CookieFileError):
            parse_cookie_text(logged_out)
        other_site = f"# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tTRUE\t{FAR}\tSID\tx\n"
        with self.assertRaises(CookieFileError):
            parse_cookie_text(other_site)


class CookieManagerTest(CookieTestBase):
    def test_missing_file_gives_no_session(self) -> None:
        self.assertIsNone(CookieManager(self.path).session())

    def test_response_cookies_are_persisted(self) -> None:
        self.write_cookies()
        cm = CookieManager(self.path)
        s = cm.session()
        self.assertIsNotNone(s)
        self.assertFalse(cm.save_if_changed())  # 변경 없음 → 저장 안 함
        s.cookies.set_cookie(_cookie("__Secure-1PSIDTS", "sidts-new"))  # Set-Cookie로 받은 것처럼
        self.assertTrue(cm.save_if_changed())
        # 새로 만든 매니저(=에드온 재시작)도 새 값을 읽어야 함
        cm2 = CookieManager(self.path)
        values = {c.name: c.value for c in cm2.session().cookies}
        self.assertEqual(values["__Secure-1PSIDTS"], "sidts-new")
        self.assertEqual(values["SID"], "sid-value")
        # 우리가 저장한 파일 때문에 다시 로드하지 않아야 함 (세션 유지)
        self.assertIs(cm.session(), s)

    def test_auth_cookie_deletion_is_not_persisted(self) -> None:
        self.write_cookies()
        cm = CookieManager(self.path)
        s = cm.session()
        s.cookies.clear(".youtube.com", "/", "SID")  # 로그아웃 응답의 Set-Cookie 삭제처럼
        s.cookies.clear(".youtube.com", "/", "__Secure-1PSID")
        self.assertFalse(cm.save_if_changed())  # 삭제만 있었으면 저장할 변화 없음
        names = {c.name for c in CookieManager(self.path).session().cookies}
        self.assertTrue({"SID", "__Secure-1PSID"} <= names)
        self.assertTrue(cm.has_auth_cookies())  # 메모리도 원래 쿠키로 복원 → 회전 복구 가능
        self.assertIs(cm.session(), s)  # 파일 재로드 없이 같은 세션 유지

        # 삭제와 함께 온 다른 갱신 쿠키는 저장돼야 함
        s.cookies.clear(".youtube.com", "/", "SID")
        s.cookies.set_cookie(_cookie("__Secure-1PSIDTS", "sidts-after-delete"))
        self.assertTrue(cm.save_if_changed())
        values = {c.name: c.value for c in CookieManager(self.path).session().cookies}
        self.assertEqual(values["__Secure-1PSIDTS"], "sidts-after-delete")
        self.assertEqual(values["SID"], "sid-value")

    def test_page_headers_match_original(self) -> None:
        self.write_cookies()
        h = CookieManager(self.path).session().headers
        self.assertEqual(h.get("Sec-Fetch-Mode"), "navigate")
        self.assertIn("Chrome/120.0.0.0", h.get("User-Agent"))
        self.assertNotIn("Accept-Encoding", h)  # requests 기본 헤더를 섞지 않음 (1.4.x와 동일)

    def test_external_file_change_reloads(self) -> None:
        self.write_cookies()
        cm = CookieManager(self.path)
        s1 = cm.session()
        time.sleep(0.01)
        self.write_cookies(COOKIES_TXT.replace("sidts-old", "sidts-from-user"))
        st = os.stat(self.path)
        os.utime(self.path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        s2 = cm.session()
        self.assertIsNot(s1, s2)
        self.assertEqual({c.name: c.value for c in s2.cookies}["__Secure-1PSIDTS"], "sidts-from-user")

    def test_replace_from_text_backs_up_old_file(self) -> None:
        self.write_cookies()
        cm = CookieManager(self.path)
        cm.session()
        n = cm.replace_from_text(COOKIES_TXT.replace("sidts-old", "sidts-uploaded"))
        self.assertEqual(n, 4)  # example.com 제외
        self.assertTrue(os.path.exists(self.path + ".bak"))
        self.assertEqual({c.name: c.value for c in cm.session().cookies}["__Secure-1PSIDTS"], "sidts-uploaded")

    def test_rotate_success_saves_new_sidts(self) -> None:
        self.write_cookies()
        cm = CookieManager(self.path)
        s = cm.session()
        calls = []

        def fake_post(url, **kw):
            calls.append((url, kw.get("data")))
            s.cookies.set_cookie(_cookie("__Secure-1PSIDTS", "sidts-rotated"))
            return FakeResponse(200)

        with mock.patch.object(s, "get", return_value=FakeResponse(200)), \
                mock.patch.object(s, "post", side_effect=fake_post):
            self.assertTrue(cm.rotate())
            self.assertIsNone(cm.rotate())  # 60초 안 재호출은 건너뜀
        self.assertEqual(calls, [(cookie_manager.ROTATE_URL, cookie_manager.ROTATE_BODY)])
        values = {c.name: c.value for c in CookieManager(self.path).session().cookies}
        self.assertEqual(values["__Secure-1PSIDTS"], "sidts-rotated")
        self.assertTrue(cm.status()["last_rotate_ok"])

    def test_rotate_rejected(self) -> None:
        self.write_cookies()
        cm = CookieManager(self.path)
        s = cm.session()
        with mock.patch.object(s, "get", return_value=FakeResponse(200)), \
                mock.patch.object(s, "post", return_value=FakeResponse(401)):
            self.assertFalse(cm.rotate(force=True))
        self.assertFalse(cm.status()["last_rotate_ok"])

    def test_rotate_skipped_without_auth_cookies(self) -> None:
        self.write_cookies(f"# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t{FAR}\tPREF\tx\n")
        cm = CookieManager(self.path)
        s = cm.session()
        with mock.patch.object(s, "post") as post:
            self.assertIsNone(cm.rotate(force=True))
            post.assert_not_called()


class LoginDetectionTest(CookieTestBase):
    def test_detect_logged_in(self) -> None:
        self.assertTrue(detect_logged_in('ytcfg.set({"LOGGED_IN":true,"x":1})'))
        self.assertFalse(detect_logged_in('ytcfg.set({"LOGGED_IN" : false})'))
        self.assertIsNone(detect_logged_in("<html></html>"))

    def test_logged_out_page_marks_cookies_invalid(self) -> None:
        self.write_cookies()
        f = YouTubeHistoryFetcher(self.path)
        f.cookies_valid = True
        with mock.patch.object(f.cookies.session(), "get",
                               return_value=FakeResponse(200, 'ytcfg.set({"LOGGED_IN":false});')):
            self.assertEqual(f.fetch_history(), [])
        self.assertFalse(f.cookies_valid)
        self.assertEqual(f.cookie_problem(), "logged_out")

    def test_logged_in_page_with_history(self) -> None:
        self.write_cookies()
        f = YouTubeHistoryFetcher(self.path)
        data = {"contents": {"twoColumnBrowseResultsRenderer": {"tabs": [{"tabRenderer": {"content": {
            "sectionListRenderer": {"contents": [{"itemSectionRenderer": {"contents": [
                {"videoRenderer": {"videoId": "abc123", "title": {"simpleText": "T"},
                                   "ownerText": {"simpleText": "C"}}}]}}]}}}}]}}}
        html = 'ytcfg.set({"LOGGED_IN":true});var ytInitialData = ' + json.dumps(data) + ";</script>"
        with mock.patch.object(f, "_get_best_thumbnail", return_value=""), \
                mock.patch.object(f.cookies.session(), "get", return_value=FakeResponse(200, html)):
            items = f.fetch_history()
        self.assertEqual([v["video_id"] for v in items], ["abc123"])
        self.assertTrue(f.cookies_valid)
        self.assertIsNone(f.cookie_problem())

    def test_missing_file_problem(self) -> None:
        f = YouTubeHistoryFetcher(self.path)
        self.assertEqual(f.fetch_history(), [])
        self.assertEqual(f.cookie_problem(), "missing")


class MaintainCookiesTest(CookieTestBase):
    def setUp(self) -> None:
        super().setUp()
        import main
        self.main = main
        main._cookie_invalid_since = None
        main._last_cookie_recovery = 0
        main._cookie_notified = False
        self.write_cookies()
        self.fetcher = YouTubeHistoryFetcher(self.path)
        main.fetcher = self.fetcher
        self.services: list[str] = []
        self.patches = [
            mock.patch.object(main, "_ha_service", side_effect=lambda svc, data: self.services.append(svc) or True),
            mock.patch.object(main.time, "sleep", lambda s: None),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        self.main.fetcher = None
        super().tearDown()

    def test_valid_cookies_rotate_periodically(self) -> None:
        self.fetcher.cookies_valid = True
        with mock.patch.object(self.fetcher, "rotate_cookies", return_value=True) as rot:
            self.main.maintain_cookies()
            rot.assert_called_once_with()
        self.assertEqual(self.services, [])

    def test_recovery_success_no_notification(self) -> None:
        self.fetcher.logged_out_detected = True

        def refetch():
            self.fetcher.cookies_valid = True
            self.fetcher.logged_out_detected = False
            return []

        with mock.patch.object(self.fetcher, "rotate_cookies", return_value=True), \
                mock.patch.object(self.fetcher, "fetch_history", side_effect=refetch):
            self.main.maintain_cookies()
        self.assertTrue(self.fetcher.cookies_valid)
        self.assertEqual(self.services, [])

    def test_recovery_failure_notifies_once_then_dismisses(self) -> None:
        self.fetcher.logged_out_detected = True
        with mock.patch.object(self.fetcher, "rotate_cookies", return_value=False), \
                mock.patch.object(self.fetcher, "fetch_history", return_value=[]):
            self.main.maintain_cookies()
            self.main.maintain_cookies()
        self.assertEqual(self.services, ["persistent_notification/create"])
        # 사용자가 새 쿠키를 넣어 다시 유효해지면 알림 제거
        self.fetcher.cookies_valid = True
        self.fetcher.logged_out_detected = False
        with mock.patch.object(self.fetcher, "rotate_cookies", return_value=True):
            self.main.maintain_cookies()
        self.assertEqual(self.services, ["persistent_notification/create", "persistent_notification/dismiss"])

    def test_unknown_state_does_nothing(self) -> None:
        self.fetcher.cookies_valid = False  # 네트워크 오류 등: logged_out 아님, 파일 있음
        with mock.patch.object(self.fetcher, "rotate_cookies") as rot:
            self.main.maintain_cookies()
            rot.assert_not_called()
        self.assertEqual(self.services, [])


class SubscriptionGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        from app import subscription_store as ss
        self.ss = ss
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "subs.json")
        self.p = mock.patch.object(ss, "_get_subs_path", return_value=self.path)
        self.p.start()

    def tearDown(self) -> None:
        self.p.stop()
        self.tmp.cleanup()

    def chans(self, names):
        return [{"channel_name": n} for n in names]

    def store(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def test_mass_drop_not_recorded_until_confirmed(self) -> None:
        full = [f"ch{i}" for i in range(300)]
        self.ss.update_subscription_changes(self.chans(full))
        self.ss.update_subscription_changes(self.chans(full[:4] + ["new"]))  # 일부만 내려온 응답
        st = self.store()
        self.assertEqual(st.get("monthly_changes"), {})
        self.assertEqual(len(st["last_snapshot"]["channels"]), 300)
        # 정상 응답 복귀 + 실제 변화 1건은 정상 기록
        self.ss.update_subscription_changes(self.chans(full[1:] + ["real_new"]))
        month = next(iter(self.store()["monthly_changes"].values()))
        self.assertEqual(month, {"added": ["real_new"], "removed": ["ch0"]})
        self.assertNotIn("pending_drop", self.store())

    def test_mass_drop_accepted_after_24h_without_records(self) -> None:
        full = [f"ch{i}" for i in range(50)]
        self.ss.update_subscription_changes(self.chans(full))
        small = self.chans(["a", "b"])
        with mock.patch.object(self.ss.time, "time", return_value=1000.0):
            self.ss.update_subscription_changes(small)
        with mock.patch.object(self.ss.time, "time", return_value=1000.0 + 25 * 3600):
            self.ss.update_subscription_changes(small)
        st = self.store()
        self.assertEqual(sorted(st["last_snapshot"]["channels"]), ["a", "b"])
        self.assertEqual(st.get("monthly_changes"), {})

    def test_small_changes_still_recorded(self) -> None:
        self.ss.update_subscription_changes(self.chans(["a", "b", "c"]))
        self.ss.update_subscription_changes(self.chans(["a"]))  # 2개 취소: 급감 기준(20개) 미만
        month = next(iter(self.store()["monthly_changes"].values()))
        self.assertEqual(sorted(month["removed"]), ["b", "c"])

    def test_channel_set_change_resets_baseline_without_records(self) -> None:
        self.ss.update_subscription_changes(self.chans(["a", "b", "c"]))  # 기존(단일 채널)
        self.ss.update_subscription_changes(self.chans(["a", "x", "y"]), ["999", "main"])  # 다채널 전환
        st = self.store()
        self.assertEqual(st.get("monthly_changes"), {})
        self.assertEqual(sorted(st["last_snapshot"]["channels"]), ["a", "x", "y"])
        self.ss.update_subscription_changes(self.chans(["a", "x"]), ["999", "main"])  # 이후 변화는 정상 기록
        month = next(iter(self.store()["monthly_changes"].values()))
        self.assertEqual(month["removed"], ["y"])

    def test_repair_mass_removals(self) -> None:
        removed = [f"ch{i}" for i in range(374)]
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({
                "last_snapshot": {"date": "2026-11-10", "channels": ["x", "y", "z", "w"]},
                "monthly_changes": {
                    "2026-11": {"added": ["x", "y"], "removed": removed},
                    "2026-10": {"added": ["k"], "removed": ["old1"]},
                },
            }, f)
        self.assertEqual(self.ss.repair_mass_removals(), 1)
        st = self.store()
        self.assertEqual(st["monthly_changes"]["2026-11"]["removed"], [])
        self.assertEqual(st["monthly_changes"]["2026-10"]["removed"], ["old1"])  # 정상 월은 그대로
        self.assertEqual(len(st["last_snapshot"]["channels"]), 378)
        self.assertEqual(self.ss.repair_mass_removals(), 0)  # 두 번째 실행은 아무것도 안 함


class MultiChannelTest(CookieTestBase):
    SWITCHER = ")]}'" + chr(10) + json.dumps({"data": {"actions": [{"getMultiPageMenuAction": {"menu": {"sections": [
        {"accountItem": {"accountName": {"simpleText": "Main"}, "hasChannel": True, "isSelected": False,
                         "serviceEndpoint": {"selectActiveIdentityEndpoint": {"supportedTokens": [
                             {"accountStateToken": {"obfuscatedGaiaId": "1"}}]}}}},
        {"accountItem": {"accountName": {"simpleText": "Brand"}, "hasChannel": True, "isSelected": True,
                         "serviceEndpoint": {"selectActiveIdentityEndpoint": {"supportedTokens": [
                             {"pageIdToken": {"pageId": "999"}}]}}}},
    ]}}}]}})

    @staticmethod
    def hist(*ids):
        return {"contents": {"twoColumnBrowseResultsRenderer": {"tabs": [{"tabRenderer": {"content": {
            "sectionListRenderer": {"contents": [{"itemSectionRenderer": {"contents": [
                {"videoRenderer": {"videoId": i, "title": {"simpleText": i}}} for i in ids]}}]}}}}]}}}

    def test_list_channels_parses_main_and_brand(self) -> None:
        self.write_cookies()
        f = YouTubeHistoryFetcher(self.path)
        with mock.patch.object(f, "_get", return_value=FakeResponse(200, self.SWITCHER)):
            chans = f.list_channels()
        self.assertEqual([(c["name"], c["page_id"], c["selected"]) for c in chans],
                         [("Main", None, False), ("Brand", "999", True)])

    def test_history_merged_from_all_channels(self) -> None:
        self.write_cookies()
        f = YouTubeHistoryFetcher(self.path)
        f._channels, f._channels_at = [{"name": "Main", "page_id": None}, {"name": "Brand", "page_id": "999"}], time.time()
        pages = {None: self.hist("mainAAAAAA1", "mainAAAAAA2"), "999": self.hist("brandBBBBB1")}
        with mock.patch.object(f, "_innertube_browse", side_effect=lambda bid, pid: pages[pid]),                 mock.patch.object(f, "_get_best_thumbnail", return_value=""),                 mock.patch("app.fetcher.time.sleep"):
            merged = f._merge_all_channels_history([])
        self.assertEqual([v["video_id"] for v in merged], ["mainAAAAAA1", "brandBBBBB1", "mainAAAAAA2"])
        self.assertEqual(sorted(f.history_by_channel), ["999", "main"])

    def test_single_channel_or_disabled_uses_default(self) -> None:
        self.write_cookies()
        f = YouTubeHistoryFetcher(self.path)
        f.scan_all_channels = False
        default = [{"video_id": "x"}]
        self.assertIs(f._merge_all_channels_history(default), default)
        self.assertEqual(f.history_by_channel, {"default": default})

    def test_subscription_failure_keeps_previous(self) -> None:
        self.write_cookies()
        f = YouTubeHistoryFetcher(self.path)
        f._channels, f._channels_at = [{"name": "Main", "page_id": None}, {"name": "Brand", "page_id": "999"}], time.time()
        with mock.patch.object(f, "_innertube_browse", return_value=None), mock.patch("app.fetcher.time.sleep"):
            self.assertEqual(f._merge_all_channels_subscriptions([{"channel_name": "a"}]), [])
        self.assertIsNone(f.subscription_channel_keys)


class CookieUploadApiTest(CookieTestBase):
    """실제 HTTP 서버로 POST /api/cookies 검증."""

    def setUp(self) -> None:
        super().setUp()
        import main
        from http.server import HTTPServer
        self.main = main
        main._cookie_invalid_since = None
        main._cookie_notified = False
        self.fetcher = YouTubeHistoryFetcher(self.path)  # 파일 없음 상태에서 시작
        main.fetcher = self.fetcher
        self.server = HTTPServer(("127.0.0.1", 0), main.YouTubeMonitoringHandler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.main.fetcher = None
        super().tearDown()

    def post(self, body: str, env: dict | None = None) -> tuple[int, dict]:
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/cookies",
                                     data=body.encode("utf-8"), method="POST",
                                     headers={"Content-Type": "text/plain"})
        with mock.patch.dict(os.environ, env or {}, clear=False):
            if not env:
                os.environ.pop("SUPERVISOR_TOKEN", None)
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read() or b"{}")

    def test_upload_valid_cookies(self) -> None:
        def fake_fetch():
            self.fetcher.cookies_valid = True
            return []

        with mock.patch.object(self.fetcher, "fetch_history", side_effect=fake_fetch), \
                mock.patch.object(self.fetcher, "rotate_cookies", return_value=True):
            status, data = self.post(COOKIES_TXT)
        self.assertEqual(status, 200, data)
        self.assertTrue(data["cookies_valid"])
        self.assertEqual(data["cookie_count"], 4)
        self.assertTrue(os.path.exists(self.path))

    def test_upload_invalid_cookies(self) -> None:
        status, data = self.post("hello")
        self.assertEqual(status, 400)
        self.assertEqual(data["error"], "invalid_cookies")
        self.assertFalse(os.path.exists(self.path))

    def test_upload_blocked_outside_ingress_on_ha(self) -> None:
        status, data = self.post(COOKIES_TXT, env={"SUPERVISOR_TOKEN": "x"})
        self.assertEqual(status, 403)
        self.assertFalse(os.path.exists(self.path))

    def _post_ingest(self, raw: bytes, headers: dict | None = None) -> tuple[int, dict]:
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/ingest", data=raw, method="POST",
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_ingest_sanitizes_untrusted_fields(self) -> None:
        saved = []
        evil = {
            "video_id": "abcDEF12345",
            "title": "<img src=x onerror=alert(1)>" + "A" * 1000,
            "channel": "  ch\n\tname  ",
            "url": "javascript:alert(document.cookie)",
            "thumbnail": "x\" onerror=\"alert(1)",
            "duration": "\"><script>alert(1)</script>",
        }
        with mock.patch.object(self.main, "on_video_change", side_effect=lambda v: saved.append(v) or True):
            status, data = self._post_ingest(json.dumps(evil).encode())
        self.assertEqual(status, 200, data)
        v = saved[0]
        self.assertEqual(v["url"], "https://www.youtube.com/watch?v=abcDEF12345")
        self.assertEqual(v["thumbnail"], "https://img.youtube.com/vi/abcDEF12345/0.jpg")
        self.assertEqual(v["duration"], "N/A")
        self.assertEqual(len(v["title"]), 300)
        self.assertEqual(v["channel"], "ch name")

    def test_ingest_rejects_bad_input(self) -> None:
        bad_id = json.dumps({"video_id": "../../etc<script>"}).encode()
        self.assertEqual(self._post_ingest(bad_id)[0], 400)
        self.assertEqual(self._post_ingest(b"[1,2,3]")[0], 400)  # 객체가 아닌 JSON
        self.assertEqual(self._post_ingest(b"{not json")[0], 400)
        try:  # 크기 제한: 413 또는 (본문을 다 받기 전에 응답해) 연결 종료
            self.assertEqual(self._post_ingest(b"x" * (70 * 1024))[0], 413)
        except (ConnectionError, urllib.error.URLError):
            pass

    def test_ingest_shorts_skipped(self) -> None:
        body = json.dumps({"video_id": "shortsID001", "url": "https://www.youtube.com/shorts/shortsID001"}).encode()
        status, data = self._post_ingest(body)
        self.assertEqual((status, data.get("reason")), (200, "shorts"))

    def test_ui_escapes_attributes(self) -> None:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=10) as r:
            html = r.read().decode("utf-8")
        self.assertIn("function safeUrl", html)
        self.assertIn("'\"': '&quot;'", html)
        self.assertNotIn('href="${v.url}"', html)
        self.assertNotIn('src="${v.thumbnail', html)

    def test_ui_has_cookie_button(self) -> None:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=10) as r:
            html = r.read().decode("utf-8")
        self.assertIn("btn-cookie-update", html)
        self.assertIn("api/cookies", html)


if __name__ == "__main__":
    unittest.main()
