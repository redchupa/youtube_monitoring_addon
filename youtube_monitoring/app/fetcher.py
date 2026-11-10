"""
YouTube 시청 기록 fetcher - 쿠키 기반 (HA 의존 없음).

YouTube /feed/history 페이지에서 ytInitialData JSON 파싱.
- lockupViewModel: 최신 UI 형식 (일반 영상)
- videoRenderer: 레거시 형식
- reelShelfRenderer + shortsLockupViewModel: Shorts

우선순위: lockup > videoRenderer > Shorts (일반 영상 우선)
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from typing import Any
from urllib.parse import unquote

import requests

from .cookie_manager import CookieManager

_LOGGER = logging.getLogger(__name__)

MAX_HISTORY_ITEMS = 20
MAX_RECOMMENDED_ITEMS = 3

# YouTube ytcfg에 LOGGED_IN 플래그가 매 응답마다 포함됨.
# 만료된 쿠키일 경우 "LOGGED_IN":false 로 응답하므로 신뢰 가능한 지표.
INNERTUBE_BROWSE_URL = "https://www.youtube.com/youtubei/v1/browse?prettyPrint=false"
DEFAULT_CLIENT_VERSION = "2.20261106.00.00"
CHANNEL_LIST_TTL = 6 * 3600
_CLIENT_VERSION_RE = re.compile(r'"INNERTUBE_CLIENT_VERSION"\s*:\s*"([0-9.]+)"')

_LOGGED_IN_RE = re.compile(r"""["']LOGGED_IN["']\s*:\s*(true|false)""")


def detect_logged_in(html: str) -> bool | None:
    """YouTube HTML에서 로그인 상태 추출. None이면 판별 불가."""
    if not html:
        return None
    m = _LOGGED_IN_RE.search(html)
    if m:
        return m.group(1) == "true"
    # 보조 지표: DELEGATED_SESSION_ID 가 빈 값이면 비로그인
    if '"DELEGATED_SESSION_ID":""' in html:
        return False
    return None


_check_html_logged_in = detect_logged_in  # 이전 이름 호환


def _parse_subscriber_count(text: str) -> int:
    """
    구독자 수 문자열을 정수로 변환. 정렬용.
    예: "구독자 17만명" -> 170000, "1.23M" -> 1230000, "500천" -> 500000
    """
    if not text or not isinstance(text, str):
        return 0
    text = text.strip()
    # 숫자(정수 또는 소수) + 만/천/억 또는 K/M
    match = re.search(r"([\d.,]+)\s*(만|천|억|K|M|만명|천명|억명)?", text, re.IGNORECASE)
    if not match:
        return 0
    try:
        num_str = match.group(1).replace(",", "")
        num = float(num_str) if "." in num_str else int(num_str)
    except (ValueError, TypeError):
        return 0
    unit = (match.group(2) or "").upper().replace("명", "").strip()
    if unit in ("만", "만명"):
        return int(num * 10_000)
    if unit in ("천", "천명"):
        return int(num * 1_000)
    if unit in ("억", "억명"):
        return int(num * 100_000_000)
    if unit == "K":
        return int(num * 1_000)
    if unit == "M":
        return int(num * 1_000_000)
    return int(num)


class YouTubeHistoryFetcher:
    """YouTube 시청 기록, 구독 채널, 추천 영상을 쿠키로 조회."""

    def __init__(self, cookies_path: str) -> None:
        self.cookies_path = cookies_path
        self.cookies = CookieManager(cookies_path)
        self.cookies_valid = False
        self.logged_out_detected = False  # YouTube가 로그아웃 상태로 응답했는지
        self.history_data: list[dict[str, Any]] = []
        self.subscriptions_data: dict[str, Any] | None = None  # {total_count, channels: [{channel_name}]}
        self.recommended_data: list[dict[str, Any]] | None = None  # 최대 3개 추천 영상
        # 계정의 모든 채널(본채널 + 브랜드 채널) 수집
        self.scan_all_channels = True
        self._thumb_cache: dict[str, str] = {}
        self._channels: list[dict[str, Any]] | None = None
        self._channels_at = 0.0
        self._client_version = DEFAULT_CLIENT_VERSION
        self.history_by_channel: dict[str, list[dict[str, Any]]] = {}
        self.subscription_channel_keys: list[str] | None = None  # 구독 목록을 만든 채널 구성 (None=이번 조회 실패)

    def _get_session(self) -> requests.Session | None:
        """쿠키 파일 기반 지속 세션 (응답 쿠키 자동 반영)."""
        session = self.cookies.session()
        if session is None:
            self.cookies_valid = False
        return session

    def _get(self, url: str) -> requests.Response | None:
        """세션으로 GET → 갱신된 쿠키 저장. 세션이 없으면 None."""
        with self.cookies.lock:
            session = self._get_session()
            if session is None:
                return None
            try:
                return session.get(url, timeout=10)
            finally:
                try:
                    self.cookies.save_if_changed()
                except Exception as err:  # 저장 실패가 조회를 막지 않게
                    _LOGGER.warning("[쿠키] 저장 실패: %s", err)

    def _apply_login_state(self, html: str) -> bool | None:
        """페이지의 LOGGED_IN 값으로 쿠키 유효성 갱신. 판단 불가 시 None."""
        state = detect_logged_in(html)
        if state is False:
            if self.cookies_valid or not self.logged_out_detected:
                _LOGGER.warning("[쿠키] YouTube가 로그아웃 상태로 응답 - 쿠키 만료")
            self.cookies_valid = False
            self.logged_out_detected = True
        elif state is True:
            self.cookies_valid = True
            self.logged_out_detected = False
        return state

    def cookie_problem(self) -> str | None:
        """확실한 쿠키 문제: 'missing'(파일 없음/깨짐), 'logged_out'(로그아웃 응답), 없으면 None."""
        if self.cookies.session() is None:
            return "missing"
        if self.logged_out_detected:
            return "logged_out"
        return None

    def rotate_cookies(self, force: bool = False) -> bool | None:
        """Google 쿠키 회전(자동 갱신). CookieManager.rotate 참조."""
        return self.cookies.rotate(force=force)

    def _get_best_thumbnail(self, video_id: str) -> str:
        """maxresdefault 있으면 사용, 없으면 0.jpg. 결과는 캐시 (매 주기 같은 영상을 다시 확인하지 않음)."""
        if not video_id or video_id == "N/A":
            return ""
        cached = self._thumb_cache.get(video_id)
        if cached:
            return cached
        url_base = f"https://img.youtube.com/vi/{video_id}"
        result = f"{url_base}/0.jpg"
        try:
            r = requests.head(f"{url_base}/maxresdefault.jpg", timeout=3, allow_redirects=True)
            if r.status_code == 200:
                result = f"{url_base}/maxresdefault.jpg"
        except requests.exceptions.RequestException:
            return result  # 네트워크 오류는 캐시하지 않음
        if len(self._thumb_cache) >= 5000:
            self._thumb_cache.clear()
        self._thumb_cache[video_id] = result
        return result

    def _extract_lockup_info(self, lockup: dict) -> dict[str, Any] | None:
        """
        lockupViewModel 파싱 (YouTube 최신 UI).
        contentId, title, channel, duration, thumbnail 추출.
        """
        try:
            video_id = lockup.get("contentId")
            if not video_id:
                return None

            metadata = lockup.get("metadata", {}).get("lockupMetadataViewModel", {})
            title = metadata.get("title", {}).get("content", "N/A")
            if title and isinstance(title, str):
                title = title.strip()
            else:
                title = "N/A"

            channel = "N/A"
            metadata_rows = metadata.get("metadata", {}).get("contentMetadataViewModel", {}).get("metadataRows", [])
            if metadata_rows:
                first_row = metadata_rows[0]
                parts = first_row.get("metadataParts", [])
                if parts:
                    channel = parts[0].get("text", {}).get("content", "N/A")
                    if channel and isinstance(channel, str):
                        channel = channel.strip()
                    else:
                        channel = "N/A"

            # duration: overlay badge 또는 metadata에서 추출
            duration = "N/A"
            thumbnail_vm = lockup.get("contentImage", {}).get("thumbnailViewModel", {})
            overlays = thumbnail_vm.get("overlays", [])
            for overlay in overlays:
                if "thumbnailOverlayBadgeViewModel" in overlay:
                    badge_vm = overlay["thumbnailOverlayBadgeViewModel"]
                    badges = badge_vm.get("thumbnailBadges", [])
                    for badge in badges:
                        if "thumbnailBadgeViewModel" in badge:
                            badge_data = badge["thumbnailBadgeViewModel"]
                            text = badge_data.get("text", "")
                            badge_style = badge_data.get("badgeStyle", "")
                            if badge_style == "THUMBNAIL_OVERLAY_BADGE_STYLE_LIVE" or text in ["라이브", "LIVE"]:
                                duration = "LIVE"
                                break
                            elif text and re.match(r"^\d{1,2}:\d{2}", text):
                                duration = text
                                break
                    if duration != "N/A":
                        break
                elif "thumbnailOverlayTimeStatusRenderer" in overlay:
                    time_status = overlay["thumbnailOverlayTimeStatusRenderer"]
                    text_obj = time_status.get("text", {})
                    if "simpleText" in text_obj:
                        duration = text_obj["simpleText"]
                        break
                    elif "accessibility" in text_obj:
                        duration = text_obj["accessibility"].get("accessibilityData", {}).get("label", "N/A")
                        break
                elif "thumbnailBottomOverlayViewModel" in overlay:
                    badges = overlay["thumbnailBottomOverlayViewModel"].get("badges", [])
                    for badge in badges:
                        if "thumbnailBadgeViewModel" in badge:
                            duration = badge["thumbnailBadgeViewModel"].get("text", "N/A")
                            break
                    if duration != "N/A":
                        break
            if duration == "N/A":
                for row in metadata_rows:
                    parts = row.get("metadataParts", [])
                    for part in parts:
                        text = part.get("text", {}).get("content", "")
                        if re.match(r"^\d{1,2}:\d{2}(:\d{2})?$", text):
                            duration = text
                            break
                    if duration != "N/A":
                        break

            return {
                "channel": channel,
                "title": title,
                "video_id": video_id,
                "duration": duration,
                "thumbnail": self._get_best_thumbnail(video_id),
                "url": f"https://www.youtube.com/watch?v={video_id}",
            }
        except (KeyError, TypeError, AttributeError) as err:
            _LOGGER.debug("Extract lockup error: %s", err)
            return None

    def _extract_video_renderer_info(self, vr: dict) -> dict[str, Any] | None:
        """videoRenderer 파싱 (레거시 YouTube UI)."""
        try:
            video_id = vr.get("videoId", "N/A")
            title = "N/A"
            if "title" in vr:
                td = vr["title"]
                if "runs" in td and td["runs"]:
                    title = td["runs"][0].get("text", "N/A")
                elif "simpleText" in td:
                    title = td["simpleText"]
                if isinstance(title, str):
                    title = title.strip()

            channel = "N/A"
            for key in ["longBylineText", "shortBylineText", "ownerText"]:
                if key in vr:
                    byline = vr[key]
                    if "runs" in byline and byline["runs"]:
                        channel = byline["runs"][0].get("text", "N/A")
                        break
                    elif "simpleText" in byline:
                        channel = byline["simpleText"]
                        break

            return {
                "channel": channel,
                "title": title,
                "video_id": video_id,
                "duration": vr.get("lengthText", {}).get("simpleText", "N/A"),
                "thumbnail": self._get_best_thumbnail(video_id),
                "url": f"https://www.youtube.com/watch?v={video_id}",
            }
        except (KeyError, TypeError, AttributeError) as err:
            _LOGGER.debug("Extract videoRenderer error: %s", err)
            return None

    def _extract_shorts_info(self, shorts: dict) -> dict[str, Any] | None:
        """shortsLockupViewModel 파싱 (YouTube Shorts)."""
        try:
            entity_id = shorts.get("entityId", "")
            video_id = entity_id.split("-")[-1] if entity_id else None
            if not video_id or video_id == "item":
                video_id = (
                    shorts.get("onTap", {})
                    .get("innertubeCommand", {})
                    .get("reelWatchEndpoint", {})
                    .get("videoId")
                )
            if not video_id:
                return None
            title = (
                shorts.get("overlayMetadata", {})
                .get("primaryText", {})
                .get("content", "YouTube Shorts")
            )
            return {
                "channel": "YouTube Shorts",
                "title": title or "YouTube Shorts",
                "video_id": video_id,
                "duration": "Shorts",
                "thumbnail": self._get_best_thumbnail(video_id),
                "url": f"https://www.youtube.com/shorts/{video_id}",
            }
        except (KeyError, TypeError, AttributeError) as err:
            _LOGGER.debug("Extract Shorts error: %s", err)
            return None

    # ── 계정의 모든 채널(본채널 + 브랜드 채널) ───────────────────
    def list_channels(self) -> list[dict[str, Any]]:
        """계정 채널 목록 [{name, page_id(None=본채널), selected}]. 6시간 캐시, 실패 시 []."""
        now = time.time()
        if self._channels is not None and now - self._channels_at < CHANNEL_LIST_TTL:
            return self._channels
        try:
            resp = self._get("https://www.youtube.com/getAccountSwitcherEndpoint")
            if resp is None or resp.status_code != 200:
                return self._channels or []
            text = resp.text
            data = json.loads(text[text.index("{"):])
        except (requests.exceptions.RequestException, ValueError) as err:
            _LOGGER.debug("[채널] 목록 조회 실패: %s", err)
            return self._channels or []

        found: list[dict[str, Any]] = []

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                item = node.get("accountItem")
                if isinstance(item, dict) and item.get("hasChannel", True):
                    page_id = None
                    tokens = (item.get("serviceEndpoint", {}).get("selectActiveIdentityEndpoint", {})
                              .get("supportedTokens", []))
                    for tok in tokens if isinstance(tokens, list) else []:
                        pid = (tok.get("pageIdToken") or {}).get("pageId") if isinstance(tok, dict) else None
                        if pid:
                            page_id = str(pid)
                    name = (item.get("accountName") or {}).get("simpleText") or "?"
                    found.append({"name": name, "page_id": page_id, "selected": bool(item.get("isSelected"))})
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(data)
        unique: dict[Any, dict[str, Any]] = {}
        for ch in found:
            unique.setdefault(ch["page_id"], ch)
        channels = list(unique.values())
        if channels:
            if self._channels is None or [c["page_id"] for c in channels] != [c["page_id"] for c in self._channels]:
                _LOGGER.info("[채널] 계정 채널 %d개: %s", len(channels), ", ".join(c["name"] for c in channels))
            self._channels, self._channels_at = channels, now
        return self._channels or []

    def _innertube_browse(self, browse_id: str, page_id: str | None) -> dict | None:
        """InnerTube browse 호출 (채널 지정 가능). 실패 시 None."""
        with self.cookies.lock:
            session = self._get_session()
            if session is None:
                return None
            sapisid = next((c.value for c in session.cookies
                            if c.name in ("SAPISID", "__Secure-3PAPISID") and c.value), None)
            if not sapisid:
                return None
            ts = int(time.time())
            digest = hashlib.sha1(f"{ts} {sapisid} https://www.youtube.com".encode()).hexdigest()
            context: dict[str, Any] = {"client": {"clientName": "WEB", "clientVersion": self._client_version,
                                                  "hl": "ko", "gl": "KR"}}
            headers = {
                "Authorization": f"SAPISIDHASH {ts}_{digest}",
                "X-Origin": "https://www.youtube.com",
                "Origin": "https://www.youtube.com",
                "X-Goog-AuthUser": "0",
                "Accept": "*/*",
                "Sec-Fetch-Mode": "cors",
            }
            if page_id:
                context["user"] = {"onBehalfOfUser": page_id}
                headers["X-Goog-PageId"] = page_id
            try:
                resp = session.post(INNERTUBE_BROWSE_URL, json={"context": context, "browseId": browse_id},
                                    headers=headers, timeout=15)
            except requests.exceptions.RequestException as err:
                _LOGGER.debug("[채널] browse %s 실패: %s", browse_id, err)
                return None
            finally:
                try:
                    self.cookies.save_if_changed()
                except Exception:
                    pass
        if resp.status_code != 200:
            _LOGGER.debug("[채널] browse %s HTTP %s", browse_id, resp.status_code)
            return None
        try:
            data = resp.json()
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def _multi_channels(self) -> list[dict[str, Any]]:
        """다채널 수집 대상. 옵션이 꺼져 있거나 채널이 1개면 []."""
        if not self.scan_all_channels:
            return []
        channels = self.list_channels()
        return channels if len(channels) > 1 else []

    def _merge_all_channels_history(self, default_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """모든 채널 시청 기록을 모은다. history_by_channel에 채널별 결과, 반환은 채널을 번갈아 섞은 목록."""
        channels = self._multi_channels()
        by_channel: dict[str, list[dict[str, Any]]] = {}
        for ch in channels:
            data = self._innertube_browse("FEhistory", ch["page_id"])
            if data is not None:
                by_channel[ch["page_id"] or "main"] = self._parse_history_data(data)
            time.sleep(1)
        if not by_channel:
            self.history_by_channel = {"default": default_list}
            return default_list
        self.history_by_channel = by_channel
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        lists = list(by_channel.values())
        for i in range(max(len(x) for x in lists)):
            for lst in lists:
                if i < len(lst) and lst[i].get("video_id") not in seen:
                    seen.add(lst[i].get("video_id"))
                    merged.append(lst[i])
        return merged

    def _merge_all_channels_subscriptions(self, default_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """모든 채널 구독의 합집합. 하나라도 실패하면 불완전한 목록 대신 None 표시용으로 기본 목록 반환."""
        channels = self._multi_channels()
        if not channels:
            self.subscription_channel_keys = ["default"]
            return default_list
        union: dict[str, dict[str, Any]] = {}
        for ch in channels:
            data = self._innertube_browse("FEchannels", ch["page_id"])
            time.sleep(1)
            if data is None:
                # 일부 채널만 실패하면 목록이 줄어 보이므로 이번 주기는 기본(선택 채널) 결과를 쓰지 않고 이전 값 유지
                self.subscription_channel_keys = None
                return []
            for c in self._parse_channels_data(data):
                union.setdefault(c.get("channel_id") or c.get("channel_name"), c)
        self.subscription_channel_keys = sorted(ch["page_id"] or "main" for ch in channels)
        merged = list(union.values())
        merged.sort(key=lambda c: (c.get("channel_name") or ""))
        return merged

    def _parse_history_data(self, data: dict) -> list[dict[str, Any]]:
        """ytInitialData / InnerTube browse(FEhistory) 응답에서 영상 목록 추출."""
        # 탭 → 섹션 → itemSectionRenderer.contents 경로 수집
        all_paths: list[list] = []
        try:
            tabs = data["contents"]["twoColumnBrowseResultsRenderer"]["tabs"]
            for tab in tabs:
                if "tabRenderer" not in tab:
                    continue
                content = tab["tabRenderer"].get("content", {})
                sections = content.get("sectionListRenderer", {}).get("contents", [])
                for section in sections:
                    if "itemSectionRenderer" in section:
                        contents = section["itemSectionRenderer"].get("contents", [])
                        if contents:
                            all_paths.append(contents)
        except (KeyError, TypeError):
            pass

        if not all_paths:
            return []

        # 우선순위별 수집: lockup(일반) > videoRenderer(일반) > Shorts
        lockups: list[dict[str, Any]] = []
        video_renderers: list[dict[str, Any]] = []
        shorts_list: list[dict[str, Any]] = []

        for path in all_paths:
            # messageRenderer = 빈 히스토리 또는 일시정지 메시지 → 스킵
            for item in path:
                if isinstance(item, dict) and "messageRenderer" in item:
                    break
            else:
                for item in path:
                    # 필요한 개수만 추출 (항목마다 썸네일 확인 요청이 나가므로 긴 페이지 전체를 처리하지 않음)
                    if len(lockups) + len(video_renderers) >= MAX_HISTORY_ITEMS:
                        break
                    if not isinstance(item, dict):
                        continue
                    if "lockupViewModel" in item:
                        lockup = item["lockupViewModel"]
                        if lockup.get("contentType") == "LOCKUP_CONTENT_TYPE_VIDEO":
                            v = self._extract_lockup_info(lockup)
                            if v:
                                lockups.append(v)
                    elif "videoRenderer" in item:
                        v = self._extract_video_renderer_info(item["videoRenderer"])
                        if v:
                            video_renderers.append(v)
                    elif "richItemRenderer" in item:
                        c = item["richItemRenderer"].get("content", {})
                        if "videoRenderer" in c:
                            v = self._extract_video_renderer_info(c["videoRenderer"])
                            if v:
                                video_renderers.append(v)
                    elif "reelShelfRenderer" in item:
                        for ri in item["reelShelfRenderer"].get("items", []):
                            if "shortsLockupViewModel" in ri:
                                v = self._extract_shorts_info(ri["shortsLockupViewModel"])
                                if v:
                                    shorts_list.append(v)
                                break

        history_list = lockups + video_renderers + shorts_list
        return history_list

    def _parse_channels_data(self, data: dict) -> list[dict[str, Any]]:
        """ytInitialData / InnerTube browse(FEchannels) 응답에서 구독 채널 목록 추출 (정렬 포함)."""
        channel_list = None
        try:
            tabs = data["contents"]["twoColumnBrowseResultsRenderer"]["tabs"]
            for tab in tabs:
                if "tabRenderer" not in tab:
                    continue
                tab_content = tab["tabRenderer"].get("content", {})
                if "sectionListRenderer" not in tab_content:
                    continue
                sections = tab_content["sectionListRenderer"].get("contents", [])
                for section in sections:
                    if "itemSectionRenderer" not in section:
                        continue
                    items = section["itemSectionRenderer"].get("contents", [])
                    for item in items:
                        if "shelfRenderer" in item:
                            shelf_content = item["shelfRenderer"].get("content", {})
                            if "expandedShelfContentsRenderer" in shelf_content:
                                channel_list = shelf_content["expandedShelfContentsRenderer"].get("items", [])
                                break
                    if channel_list:
                        break
                if channel_list:
                    break
        except (KeyError, TypeError):
            pass

        if not channel_list:
            return []

        channels = []
        for item in channel_list:
            if "channelRenderer" not in item:
                continue
            ch = item["channelRenderer"]
            title = ch.get("title", {}).get("simpleText", "")
            if not title or not isinstance(title, str):
                continue
            title = title.strip()
            if not title:
                continue

            subscriber_count_text = ""
            vct = ch.get("videoCountText", {})
            if isinstance(vct, dict):
                subscriber_count_text = vct.get("simpleText", "") or ""

            subscriber_count = _parse_subscriber_count(subscriber_count_text)

            handle = ""
            sct = ch.get("subscriberCountText", {})
            if isinstance(sct, dict):
                handle = sct.get("simpleText", "") or ""

            thumbnail_url = ""
            thumb = ch.get("thumbnail", {}).get("thumbnails", [])
            if thumb:
                best = thumb[-1] if thumb else {}
                url = best.get("url", "")
                if url and not url.startswith("http"):
                    url = "https:" + url
                thumbnail_url = url

            channel_url = ""
            try:
                base_url = ch.get("navigationEndpoint", {}).get("browseEndpoint", {}).get("canonicalBaseUrl", "")
                if base_url:
                    channel_url = "https://www.youtube.com" + unquote(base_url)
            except (TypeError, KeyError):
                pass

            description_snippet = ""
            runs = ch.get("descriptionSnippet", {}).get("runs", [])
            if runs and isinstance(runs[0], dict):
                description_snippet = runs[0].get("text", "") or ""

            channel_id = ch.get("channelId", "")

            channels.append({
                "channel_name": title,
                "channel_id": channel_id,
                "subscriber_count_text": subscriber_count_text,
                "subscriber_count": subscriber_count,
                "handle": handle,
                "thumbnail": thumbnail_url,
                "channel_url": channel_url,
                "description_snippet": description_snippet,
            })

        def _channel_sort_key(c: dict) -> tuple[int, str]:
            """한글(0) → 영어(1) → 특수문자/숫자(2) 순, 동일 그룹 내에서는 이름순."""
            name = c.get("channel_name", "") or ""
            if not name:
                return (2, name)
            o = ord(name[0])
            if 0xAC00 <= o <= 0xD7A3 or 0x3130 <= o <= 0x318F or 0x1100 <= o <= 0x11FF:
                return (0, name)  # 한글
            if (0x41 <= o <= 0x5A) or (0x61 <= o <= 0x7A):
                return (1, name.lower())  # 영어
            return (2, name)  # 숫자·특수문자 등

        channels.sort(key=_channel_sort_key)

        return channels

    def fetch_history(self) -> list[dict[str, Any]]:
        """
        /feed/history 페이지 조회 → ytInitialData 파싱 → 영상 목록 반환.
        우선순위: lockup > videoRenderer > Shorts.
        """
        try:
            response = self._get("https://www.youtube.com/feed/history")
            if response is None:
                self.history_data = []
                return []
            if response.status_code == 429:
                _LOGGER.warning("YouTube rate limit (429). scan_interval을 늘려주세요.")
                self.history_data = []
                return []
            response.raise_for_status()
        except requests.exceptions.RequestException as err:
            _LOGGER.error("YouTube request error: %s", err)
            self.history_data = []
            return []

        html = response.text
        if self._apply_login_state(html) is False:
            self.history_data = []
            return []

        # ytInitialData JSON 추출 (두 가지 패턴 지원)
        match = re.search(r"var ytInitialData\s*=\s*({.*?});", html, re.DOTALL)
        if not match:
            match = re.search(r"ytInitialData\s*=\s*({.*?});", html, re.DOTALL)
        if not match:
            _LOGGER.error("Cannot find ytInitialData in response")
            self.history_data = []
            return []

        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError as err:
            _LOGGER.error("Parse ytInitialData error: %s", err)
            self.history_data = []
            return []

        cv = _CLIENT_VERSION_RE.search(html)
        if cv:
            self._client_version = cv.group(1)
        history_list = self._parse_history_data(data)
        history_list = self._merge_all_channels_history(history_list)
        self.history_data = history_list[:MAX_HISTORY_ITEMS]
        # 로그인 마커가 명시적으로 발견된 경우에만 valid 갱신 (위에서 처리)
        # 그렇지 않으면 데이터 존재 여부를 보조 지표로 사용
        if history_list and not self.cookies_valid:
            # 데이터는 있는데 마커가 명시 안 됨 → 일단 유효로 간주 (보수적)
            self.cookies_valid = True
        return self.history_data

    def fetch_subscriptions(self) -> dict[str, Any] | None:
        """
        /feed/channels 페이지 조회 → 구독 채널 목록 반환.
        Returns: {total_count: int, channels: [{channel_name: str}, ...]} 또는 None
        """
        try:
            response = self._get("https://www.youtube.com/feed/channels")
            if response is None:
                self.subscriptions_data = None
                return None
            if response.status_code == 429:
                _LOGGER.warning("YouTube rate limit (429). scan_interval을 늘려주세요.")
                return self.subscriptions_data  # 기존 데이터 유지
            response.raise_for_status()
        except requests.exceptions.RequestException as err:
            _LOGGER.error("YouTube subscriptions request error: %s", err)
            self.subscriptions_data = None
            return None

        html = response.text
        if self._apply_login_state(html) is False:
            return self.subscriptions_data  # 로그아웃: 기존 데이터 유지 (변경 이력 오염 방지)
        match = re.search(r"var ytInitialData\s*=\s*({.*?});", html, re.DOTALL)
        if not match:
            match = re.search(r"ytInitialData\s*=\s*({.*?});", html, re.DOTALL)
        if not match:
            _LOGGER.debug("Cannot find ytInitialData in channels page")
            self.subscriptions_data = {"total_count": 0, "channels": []}
            return self.subscriptions_data

        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError as err:
            _LOGGER.error("Parse channels ytInitialData error: %s", err)
            self.subscriptions_data = {"total_count": 0, "channels": []}
            return self.subscriptions_data

        channels = self._parse_channels_data(data)
        channels = self._merge_all_channels_subscriptions(channels)
        if not channels:
            if self.subscription_channel_keys is None:
                return self.subscriptions_data  # 일부 채널 조회 실패: 이전 목록 유지
            self.subscriptions_data = {"total_count": 0, "channels": []}
            return self.subscriptions_data

        prev_total = (self.subscriptions_data or {}).get("total_count")
        if prev_total != len(channels):
            # 진단용: 목록이 일부만 내려오는 경우를 로그로 구분할 수 있게
            _LOGGER.info(
                "[구독] 채널 %d개 조회 (이전 %s개, continuation=%s, 응답 %dKB)",
                len(channels), prev_total, '"continuationItemRenderer"' in html, len(html) // 1024,
            )
        self.subscriptions_data = {"total_count": len(channels), "channels": channels}
        # cookies_valid는 위 LOGGED_IN 마커에서 이미 갱신됨.
        # 마커가 없는 경우 채널 데이터 존재로 보조 판정.
        if channels and not self.cookies_valid:
            self.cookies_valid = True
        return self.subscriptions_data

    def fetch_recommended(self) -> list[dict[str, Any]] | None:
        """
        YouTube 메인 페이지 조회 → 추천 영상 최대 3개 반환.
        여러 경로/구조 시도 (YouTube UI 변경 대응).
        """
        try:
            response = self._get("https://www.youtube.com")
            if response is None:
                self.recommended_data = None
                return None
            if response.status_code == 429:
                _LOGGER.warning("YouTube rate limit (429). scan_interval을 늘려주세요.")
                return self.recommended_data or []
            response.raise_for_status()
        except requests.exceptions.RequestException as err:
            _LOGGER.error("YouTube recommended request error: %s", err)
            self.recommended_data = None
            return None

        html = response.text
        if self._apply_login_state(html) is False:
            self.recommended_data = []  # 비로그인 추천은 개인화가 아니므로 비움 (MQTT 센서 포함)
            return []
        match = re.search(r"var ytInitialData\s*=\s*({.*?});", html, re.DOTALL)
        if not match:
            match = re.search(r"ytInitialData\s*=\s*({.*?});\s*(?:var |$)", html, re.DOTALL)
        if not match:
            _LOGGER.debug("Cannot find ytInitialData in YouTube main page")
            self.recommended_data = []
            return []

        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError as err:
            _LOGGER.debug("Parse ytInitialData error: %s", err)
            self.recommended_data = []
            return []

        videos = self._parse_recommended_from_data(data)
        self.recommended_data = videos[:MAX_RECOMMENDED_ITEMS]
        # cookies_valid는 위 LOGGED_IN 마커에서 이미 갱신됨.
        if videos and not self.cookies_valid:
            self.cookies_valid = True
        return self.recommended_data

    def _parse_recommended_from_data(self, data: dict) -> list[dict[str, Any]]:
        """ytInitialData에서 추천 영상 추출. 여러 경로 시도."""
        videos: list[dict[str, Any]] = []

        # 경로 1: twoColumnBrowseResultsRenderer.tabs → richGridRenderer.contents
        try:
            tabs = data.get("contents", {}).get("twoColumnBrowseResultsRenderer", {}).get("tabs", [])
            for tab in tabs:
                tr = tab.get("tabRenderer", {})
                content = tr.get("content", {})
                grid = content.get("richGridRenderer", {})
                items = grid.get("contents", [])

                for item in items:
                    v = self._extract_video_from_grid_item(item)
                    if v:
                        videos.append(v)
                        if len(videos) >= MAX_RECOMMENDED_ITEMS:
                            return videos
                if videos:
                    break
        except (KeyError, TypeError, AttributeError):
            pass

        # 경로 2: sectionListRenderer (섹션별 레이아웃)
        if len(videos) < MAX_RECOMMENDED_ITEMS:
            try:
                sections = data.get("contents", {}).get("sectionListRenderer", {}).get("contents", [])
                for section in sections:
                    for item in section.get("itemSectionRenderer", {}).get("contents", []):
                        v = self._extract_video_from_grid_item(item)
                        if v:
                            videos.append(v)
                            if len(videos) >= MAX_RECOMMENDED_ITEMS:
                                return videos
            except (KeyError, TypeError, AttributeError):
                pass

        # 경로 3: 전체 데이터에서 lockupViewModel/videoRenderer 재귀 검색
        if len(videos) < MAX_RECOMMENDED_ITEMS:
            found = self._find_videos_in_dict(data, max_count=MAX_RECOMMENDED_ITEMS)
            if found:
                return found

        return videos

    def _extract_video_from_grid_item(self, item: dict) -> dict[str, Any] | None:
        """그리드 아이템에서 영상 정보 추출 (다양한 래퍼 지원)."""
        content = item.get("richItemRenderer", {}).get("content", item)

        if "lockupViewModel" in content:
            lockup = content["lockupViewModel"]
            if lockup.get("contentType") == "LOCKUP_CONTENT_TYPE_VIDEO":
                return self._extract_lockup_info(lockup)
        if "videoRenderer" in content:
            return self._extract_video_renderer_info(content["videoRenderer"])
        return None

    def _find_videos_in_dict(self, obj: Any, max_count: int = 3, max_depth: int = 8) -> list[dict[str, Any]]:
        """딕셔너리/리스트를 재귀 탐색하여 lockupViewModel 또는 videoRenderer 추출 (깊이 제한)."""
        videos: list[dict[str, Any]] = []

        def _collect(node: Any, depth: int) -> None:
            if len(videos) >= max_count or depth > max_depth:
                return
            if isinstance(node, dict):
                if "lockupViewModel" in node:
                    lockup = node["lockupViewModel"]
                    if lockup.get("contentType") == "LOCKUP_CONTENT_TYPE_VIDEO":
                        v = self._extract_lockup_info(lockup)
                        if v:
                            videos.append(v)
                    return
                if "videoRenderer" in node:
                    v = self._extract_video_renderer_info(node["videoRenderer"])
                    if v:
                        videos.append(v)
                    return
                for k, v in node.items():
                    if k in ("continuationItemRenderer", "adSlotRenderer"):
                        continue
                    _collect(v, depth + 1)
            elif isinstance(node, list):
                for x in node:
                    _collect(x, depth + 1)
                    if len(videos) >= max_count:
                        return

        _collect(obj, 0)
        return videos
