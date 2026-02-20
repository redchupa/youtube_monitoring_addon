#!/usr/bin/env python3
"""
일회성: /feed/channels 응답에서 ytInitialData 구조 확인.
채널별 사용 가능한 키(구독일, 구독자 수 등) 파악용.
"""
import json
import os
import re
import sys
from http.cookiejar import MozillaCookieJar

import requests

COOKIES_PATH = os.environ.get("COOKIES_PATH", r"c:\Users\redch\Desktop\youtube_cookies.txt")


def main():
    if not os.path.exists(COOKIES_PATH):
        print("Cookies file not found:", COOKIES_PATH)
        sys.exit(1)

    cookie_jar = MozillaCookieJar(COOKIES_PATH)
    cookie_jar.load(ignore_discard=True, ignore_expires=True)
    session = requests.Session()
    session.headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-us,en;q=0.5",
    }
    session.cookies = cookie_jar

    print("Fetching https://www.youtube.com/feed/channels ...")
    r = session.get("https://www.youtube.com/feed/channels", timeout=15)
    if r.status_code != 200:
        print("HTTP", r.status_code)
        sys.exit(1)

    html = r.text
    match = re.search(r"var ytInitialData\s*=\s*({.*?});", html, re.DOTALL)
    if not match:
        match = re.search(r"ytInitialData\s*=\s*({.*?});", html, re.DOTALL)
    if not match:
        print("ytInitialData not found in response")
        sys.exit(1)

    data = json.loads(match.group(1))

    # 채널 목록까지 경로 (fetcher와 동일)
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
    except (KeyError, TypeError) as e:
        print("Path error:", e)
        sys.exit(1)

    if not channel_list:
        print("No channel list found in response")
        sys.exit(1)

    print("Channel items count:", len(channel_list))

    first_item = channel_list[0]
    print("\n--- First item top-level keys ---")
    print(list(first_item.keys()))

    if "channelRenderer" in first_item:
        ch = first_item["channelRenderer"]
        print("\n--- channelRenderer top-level keys ---")
        print(list(ch.keys()))

        def key_tree(obj, prefix=""):
            """재귀적으로 키 경로만 출력 (깊이 3까지, 값은 타입만)."""
            if len(prefix) > 80:
                return
            if isinstance(obj, dict):
                for k, v in obj.items():
                    path = f"{prefix}.{k}" if prefix else k
                    if isinstance(v, dict) and path.count(".") < 4:
                        key_tree(v, path)
                    elif isinstance(v, list) and v and path.count(".") < 4:
                        if isinstance(v[0], dict):
                            key_tree(v[0], path + "[]")
                    else:
                        typ = type(v).__name__
                        if isinstance(v, str) and len(v) < 60:
                            print(f"  {path}: ({typ}) {v!r}")
                        else:
                            print(f"  {path}: ({typ})")

        print("\n--- channelRenderer key tree (sample values) ---")
        key_tree(ch)

        out_path = os.path.join(os.path.dirname(__file__), "channels_sample_structure.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(ch, f, ensure_ascii=False, indent=2)
        print("\nFull first channelRenderer saved to:", out_path)

    # 두 번째 채널도 저장해 순서/구독일 관련 필드 비교
    if len(channel_list) >= 2 and "channelRenderer" in channel_list[1]:
        out_path2 = os.path.join(os.path.dirname(__file__), "channels_second_structure.json")
        with open(out_path2, "w", encoding="utf-8") as f:
            json.dump(channel_list[1]["channelRenderer"], f, ensure_ascii=False, indent=2)
        print("Second channelRenderer saved to:", out_path2)

    print("\nDone.")


if __name__ == "__main__":
    main()
