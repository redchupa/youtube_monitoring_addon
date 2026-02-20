"""
YouTube 시청 기록 fetcher - 쿠키 기반 (HA 의존 없음).

YouTube /feed/history 페이지에서 ytInitialData JSON 파싱.
- lockupViewModel: 최신 UI 형식 (일반 영상)
- videoRenderer: 레거시 형식
- reelShelfRenderer + shortsLockupViewModel: Shorts

우선순위: lockup > videoRenderer > Shorts (일반 영상 우선)
"""
from __future__ import annotations

import json
import logging
import os
import re
from http.cookiejar import MozillaCookieJar
from typing import Any
from urllib.parse import unquote

import requests

_LOGGER = logging.getLogger(__name__)

MAX_HISTORY_ITEMS = 20
MAX_RECOMMENDED_ITEMS = 3


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
        self.cookies_valid = False
        self.history_data: list[dict[str, Any]] = []
        self.subscriptions_data: dict[str, Any] | None = None  # {total_count, channels: [{channel_name}]}
        self.recommended_data: list[dict[str, Any]] | None = None  # 최대 3개 추천 영상

    def _get_session(self) -> requests.Session | None:
        """Netscape 형식 쿠키 파일로 세션 생성."""
        if not os.path.exists(self.cookies_path):
            _LOGGER.error("Cookies file not found: %s", self.cookies_path)
            self.cookies_valid = False
            return None

        cookie_jar = MozillaCookieJar(self.cookies_path)
        try:
            cookie_jar.load(ignore_discard=True, ignore_expires=True)
        except OSError as err:
            _LOGGER.error("Failed to load cookies: %s", err)
            self.cookies_valid = False
            return None
        except Exception as err:
            _LOGGER.error("Unexpected cookie load error: %s", err)
            self.cookies_valid = False
            return None

        if len(cookie_jar) == 0:
            _LOGGER.error("Cookies file is empty")
            self.cookies_valid = False
            return None

        session = requests.Session()
        session.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-us,en;q=0.5",
            "Sec-Fetch-Mode": "navigate",
        }
        session.cookies = cookie_jar
        return session

    def _get_best_thumbnail(self, video_id: str) -> str:
        """maxresdefault 있으면 사용, 없으면 0.jpg."""
        if not video_id or video_id == "N/A":
            return ""
        url_base = f"https://img.youtube.com/vi/{video_id}"
        try:
            r = requests.get(f"{url_base}/maxresdefault.jpg", timeout=3)
            if r.status_code == 200:
                return f"{url_base}/maxresdefault.jpg"
        except requests.exceptions.RequestException:
            pass
        return f"{url_base}/0.jpg"

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

    def fetch_history(self) -> list[dict[str, Any]]:
        """
        /feed/history 페이지 조회 → ytInitialData 파싱 → 영상 목록 반환.
        우선순위: lockup > videoRenderer > Shorts.
        """
        session = self._get_session()
        if session is None:
            self.history_data = []
            return []

        try:
            response = session.get("https://www.youtube.com/feed/history", timeout=10)
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
            self.history_data = []
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
        self.history_data = history_list[:MAX_HISTORY_ITEMS]
        self.cookies_valid = True
        return self.history_data

    def fetch_subscriptions(self) -> dict[str, Any] | None:
        """
        /feed/channels 페이지 조회 → 구독 채널 목록 반환.
        Returns: {total_count: int, channels: [{channel_name: str}, ...]} 또는 None
        """
        session = self._get_session()
        if session is None:
            self.subscriptions_data = None
            return None

        try:
            response = session.get("https://www.youtube.com/feed/channels", timeout=10)
            if response.status_code == 429:
                _LOGGER.warning("YouTube rate limit (429). scan_interval을 늘려주세요.")
                return self.subscriptions_data  # 기존 데이터 유지
            response.raise_for_status()
        except requests.exceptions.RequestException as err:
            _LOGGER.error("YouTube subscriptions request error: %s", err)
            self.subscriptions_data = None
            return None

        html = response.text
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
            self.subscriptions_data = {"total_count": 0, "channels": []}
            return self.subscriptions_data

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

        self.subscriptions_data = {"total_count": len(channels), "channels": channels}
        if channels:
            self.cookies_valid = True
        return self.subscriptions_data

    def fetch_recommended(self) -> list[dict[str, Any]] | None:
        """
        YouTube 메인 페이지 조회 → 추천 영상 최대 3개 반환.
        여러 경로/구조 시도 (YouTube UI 변경 대응).
        """
        session = self._get_session()
        if session is None:
            self.recommended_data = None
            return None

        try:
            response = session.get("https://www.youtube.com", timeout=10)
            if response.status_code == 429:
                _LOGGER.warning("YouTube rate limit (429). scan_interval을 늘려주세요.")
                return self.recommended_data or []
            response.raise_for_status()
        except requests.exceptions.RequestException as err:
            _LOGGER.error("YouTube recommended request error: %s", err)
            self.recommended_data = None
            return None

        html = response.text
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
        if videos:
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
