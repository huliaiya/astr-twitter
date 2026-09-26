# twitter_parser_standalone.py
# -*- coding: utf-8 -*-
"""
从 astrbot_plugin_parser 的 core/parsers/twitter.py 单独抽出的推特解析器（Python 版）。

与 Node 版（twitter-parser.mjs）等价，逻辑逐行对齐原插件：
  1. 用「关键词 + 正则」匹配 x.com / twitter.com 的 status 链接
  2. POST https://xdown.app/api/ajaxSearch  (q=<url>&lang=zh-cn)
  3. 解析返回 HTML：第一个 <img> → 封面；a.tw-button-dl / a.abutton → 视频/图片/gif；第一个 <h3> → 标题
  4. 输出统一结构

⚠️ 说明：本机没有 Python 运行时（只有 Node），所以**这个文件没有在本机执行过**；
   通过测试的是等价的 Node 实现 twitter-parser.mjs。本文件依赖 aiohttp + beautifulsoup4
   （和原插件相同），去掉 AstrBot 的 BaseParser / Downloader / CookieJar 后即可独立运行。

运行：
    python twitter_parser_standalone.py <链接或含链接的文本> [--download 目录] [--json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from dataclasses import asdict, dataclass, field
from itertools import chain
from pathlib import Path
from typing import Any

import aiohttp
from bs4 import BeautifulSoup, Tag

XDOWN_URL = "https://xdown.app/api/ajaxSearch"

HEADERS: dict[str, str] = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/x-www-form-urlencoded",
    "Origin": "https://xdown.app",
    "Referer": "https://xdown.app/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
}

# 与插件 twitter.py 的两个 @handle 正则完全一致
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "twitter.com",
        re.compile(
            r"(?<![A-Za-z0-9.-])(?:(?:www|mobile)\.)?twitter\.com/"
            r"(?:[A-Za-z0-9_]+/)*status/\d+"
        ),
    ),
    (
        "x.com",
        re.compile(r"(?<![A-Za-z0-9.-])(?:www\.)?x\.com/(?:[A-Za-z0-9_]+/)*status/\d+"),
    ),
]


class ParseException(Exception):
    pass


@dataclass
class Content:
    type: str  # video | image | dynamic
    url: str
    cover: str | None = None


@dataclass
class ParseResult:
    platform: dict[str, str] = field(
        default_factory=lambda: {"name": "twitter", "display_name": "推特"}
    )
    url: str = ""
    tweet_id: str | None = None
    title: str | None = None
    author: dict[str, Any] = field(default_factory=lambda: {"name": "无用户名", "avatar": None})
    cover: str | None = None
    contents: list[Content] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        c = {"video": 0, "image": 0, "dynamic": 0}
        for item in self.contents:
            c[item.type] = c.get(item.type, 0) + 1
        return c

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["counts"] = self.counts
        return d


def search_url(text: str) -> tuple[str, re.Match[str]] | None:
    """对应 BaseParser.search_url：先看关键词在不在，再跑正则。"""
    for keyword, pattern in PATTERNS:
        if keyword not in text:
            continue
        if searched := pattern.search(text):
            return keyword, searched
    return None


def parse_twitter_html(html_content: str) -> ParseResult:
    """完全对齐插件的 parse_twitter_html()。"""
    if not html_content:
        raise ParseException("解析失败, 数据为空")

    soup = BeautifulSoup(html_content, "html.parser")

    title: str | None = None
    cover_url: str | None = None
    video_url: str | None = None
    images_urls: list[str] = []
    dynamic_urls: list[str] = []

    # 1. 封面：第一个 <img src>
    thumb_tag = soup.find("img")
    if isinstance(thumb_tag, Tag):
        if cover := thumb_tag.get("src"):
            cover_url = str(cover)

    # 2. 下载链接
    tw_button_tags = soup.find_all("a", class_="tw-button-dl")
    abutton_tags = soup.find_all("a", class_="abutton")
    for tag in chain(tw_button_tags, abutton_tags):
        if not isinstance(tag, Tag):
            continue
        href = tag.get("href")
        if href is None:
            continue
        href = str(href)
        text = tag.get_text(strip=True)
        if "下载 MP4" in text:
            video_url = href
            break  # 多个清晰度时取第一个（通常 720p）
        elif "下载图片" in text:
            images_urls.append(href)
        elif "下载 gif" in text:
            dynamic_urls.append(href)

    # 3. 标题：第一个 <h3>
    title_tag = soup.find("h3")
    if title_tag:
        title = title_tag.get_text(strip=True)

    # 4. 插件里被注释掉的 tweet id，这里作为附加信息
    tweet_id: str | None = None
    twitter_id_input = soup.find("input", {"id": "TwitterId"})
    if isinstance(twitter_id_input, Tag) and isinstance(twitter_id_input.get("value"), str):
        tweet_id = str(twitter_id_input.get("value"))

    contents: list[Content] = []
    if video_url:
        contents.append(Content(type="video", url=video_url, cover=cover_url))
    contents.extend(Content(type="image", url=u) for u in images_urls)
    contents.extend(Content(type="dynamic", url=u, cover=cover_url) for u in dynamic_urls)

    return ParseResult(
        url="",
        tweet_id=tweet_id,
        title=title,
        cover=cover_url,
        contents=contents,
    )


async def parse_tweet(
    input_text: str,
    session: aiohttp.ClientSession | None = None,
    timeout: float = 20.0,
    retry: int = 2,
) -> ParseResult:
    """解析一条推特链接（或包含链接的文本）。"""
    matched = search_url(input_text)
    if matched is None:
        raise ParseException("无法匹配 URL")
    _, searched = matched
    url = f"https://{searched.group(0)}"

    own_session = session is None
    session = session or aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout))
    try:
        last_err: Exception | None = None
        resp_json: dict[str, Any] | None = None
        for attempt in range(retry + 1):
            try:
                async with session.post(
                    XDOWN_URL,
                    data={"q": url, "lang": "zh-cn"},
                    headers=HEADERS,
                ) as resp:
                    if resp.status >= 400:
                        raise ParseException(f"xdown API {resp.status} {resp.reason}")
                    resp_json = await resp.json(content_type=None)
                break
            except Exception as e:  # noqa: BLE001 - 与插件一样带重试
                last_err = e
                if attempt < retry:
                    await asyncio.sleep(0.8 * (attempt + 1))
        if resp_json is None:
            raise ParseException(f"xdown 请求失败: {last_err}")

        if resp_json.get("status") != "ok":
            raise ParseException(resp_json.get("msg") or "解析失败")
        data = resp_json.get("data")
        if data is None:
            raise ParseException("解析失败, 数据为空")

        result = parse_twitter_html(data)
        result.url = url
        return result
    finally:
        if own_session:
            await session.close()


async def download_media(url: str, out_dir: str | Path = "downloads") -> Path:
    """下载媒体到本地（原插件对应 Downloader.download_*）。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
        async with session.get(url, headers={"User-Agent": HEADERS["User-Agent"]}) as resp:
            resp.raise_for_status()
            final = str(resp.url)
            ext = Path(final.split("?")[0]).suffix or ".bin"
            path = out / f"{abs(hash(url)):x}{ext}"
            path.write_bytes(await resp.read())
            return path


async def main() -> None:
    parser = argparse.ArgumentParser(description="独立版推特解析器（抽自 astrbot_plugin_parser）")
    parser.add_argument("input", help="推特链接或包含链接的文本")
    parser.add_argument("--download", nargs="?", const="downloads", help="下载媒体到目录")
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    args = parser.parse_args()

    try:
        result = await parse_tweet(args.input)
    except ParseException as e:
        print(f"ParseException: {e}")
        raise SystemExit(1) from e

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))

    if args.download:
        for content in result.contents:
            try:
                path = await download_media(content.url, args.download)
                print(f"[{content.type}] {path} | {path.stat().st_size} bytes")
            except Exception as e:  # noqa: BLE001
                print(f"[{content.type}] 下载失败: {e}")


if __name__ == "__main__":
    asyncio.run(main())
