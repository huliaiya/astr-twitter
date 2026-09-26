# -*- coding: utf-8 -*-
"""astr-twitter 的推特解析核心（不依赖 AstrBot，可独立测试）。

从 astrbot_plugin_parser 的 core/parsers/twitter.py 抽取：
  @handle 正则匹配 → POST xdown.app/api/ajaxSearch → 解析返回 HTML → 统一 ParseResult

与原插件保持一致的细节：
  - 请求头必须带 Origin / Referer: https://xdown.app
  - 多个清晰度时只取第一个「下载 MP4」（原代码有 break，通常是 720p）
  - gif 走「下载 gif」分支，最终是 mp4 容器
  - 视频/gif 的下载按钮 class 是 tw-button-dl，图片推文是 abutton，两类都要扫
  - 封面取返回 HTML 里第一个 <img>；作者名原插件硬编码为「无用户名」
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import asdict, dataclass, field
from itertools import chain
from typing import Any

import aiohttp
from bs4 import BeautifulSoup, Tag

DEFAULT_ENDPOINT = "https://xdown.app/api/ajaxSearch"
DEFAULT_ORIGIN = "https://xdown.app"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

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

# 匹配 t.co 短链（可选功能：先重定向再解析）
TCO_RE = re.compile(r"https?://t\.co/[A-Za-z0-9]+")

# 转发（RT @user: ...）前缀
RT_RE = re.compile(r"^\s*RT\s+@(?P<user>[A-Za-z0-9_]+)\s*[:：]\s*", re.IGNORECASE)

# HTML / 文本里出现的 status 链接（用于识别被引用/转发的原推）
STATUS_URL_RE = re.compile(
    r"https?://(?:(?:www|mobile)\.)?(?:x|twitter)\.com/([A-Za-z0-9_]+)/status/(\d+)",
    re.IGNORECASE,
)


class ParseException(Exception):
    """解析失败（对应原插件的 core/exception.py:ParseException）。"""


@dataclass
class Content:
    """一条待发送的媒体。"""

    type: str  # video | image | dynamic(gif) | audio
    url: str
    cover: str | None = None
    label: str | None = None  # 如 "720p" / "MP3"，用于日志与排查

    @property
    def is_video_like(self) -> bool:
        """video 与 gif(dynamic) 都是视频容器。"""
        return self.type in ("video", "dynamic")


@dataclass
class ParseResult:
    """解析结果（对应原插件的 core/data.py:ParseResult 的精简版）。"""

    url: str = ""
    tweet_id: str | None = None
    title: str | None = None
    author_name: str = "无用户名"
    cover: str | None = None
    contents: list[Content] = field(default_factory=list)
    # 额外信息
    source: str = "xdown"  # xdown | syndication，便于排查是哪个后端解析的
    is_repost: bool = False  # 正文带 RT @user: 前缀
    quoted_url: str | None = None  # 被引用/转发的原推链接（尽力而为）

    @property
    def counts(self) -> dict[str, int]:
        c = {"video": 0, "image": 0, "dynamic": 0, "audio": 0}
        for item in self.contents:
            c[item.type] = c.get(item.type, 0) + 1
        return c

    @property
    def is_empty(self) -> bool:
        return not self.contents

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["counts"] = self.counts
        return d

    def __str__(self) -> str:  # 方便日志排查
        return f"<ParseResult {self.url} {self.counts} source={self.source} title={self.title!r}>"


@dataclass
class TwitterConfig:
    """解析器配置（由 AstrBot 插件配置映射而来）。"""

    endpoint: str = DEFAULT_ENDPOINT
    origin: str = DEFAULT_ORIGIN
    cookie: str = ""
    proxy: str | None = None
    timeout: float = 20.0
    retry: int = 2

    def headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": self.origin,
            "Referer": f"{self.origin}/",
            "User-Agent": USER_AGENT,
        }
        if self.cookie:
            headers["cookie"] = self.cookie
        return headers


def search_url(text: str) -> tuple[str, re.Match[str]] | None:
    """对应 BaseParser.search_url()：关键词短路 + 正则匹配。

    返回 (keyword, match) 或 None。match.group(0) 是不带 scheme 的链接。
    """
    if not text:
        return None
    for keyword, pattern in PATTERNS:
        if keyword not in text:
            continue
        if searched := pattern.search(text):
            return keyword, searched
    return None


def extract_urls(text: str) -> list[str]:
    """提取文本中所有推特链接（去重、保序、补上 https://）。"""
    found: list[tuple[int, str]] = []
    for re_obj in (pattern for _, pattern in PATTERNS):
        for m in re_obj.finditer(text or ""):
            found.append((m.start(), m.group(0)))
    seen: set[str] = set()
    urls: list[str] = []
    for _, raw in sorted(found, key=lambda x: x[0]):
        url = f"https://{raw}"
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def clean_title(raw: str | None) -> tuple[str | None, bool]:
    """清洗正文：去掉转发前缀与 t.co 短链，返回 (正文, 是否转发)。"""
    if not raw:
        return None, False
    is_repost = False
    if RT_RE.match(raw):
        is_repost = True
        raw = RT_RE.sub("", raw, count=1)
    raw = TCO_RE.sub("", raw).strip()
    raw = re.sub(r"\n{3,}", "\n\n", raw)
    return (raw or None), is_repost


def truncate(text: str | None, max_chars: int) -> str | None:
    """按字符数截断（0 表示不截断）。"""
    if not text or max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "…"


def find_quoted_url(html_or_text: str, tweet_id: str | None) -> str | None:
    """尽力找出被引用/转发的原推链接（与当前推文 ID 不同的第一条）。"""
    for match in STATUS_URL_RE.finditer(html_or_text or ""):
        if match.group(2) != (tweet_id or ""):
            return f"https://x.com/{match.group(1)}/status/{match.group(2)}"
    return None


def parse_twitter_html(html_content: str) -> ParseResult:
    """完全对齐原插件的 parse_twitter_html()，并额外解析音频/转发/引用信息。"""
    if not html_content:
        raise ParseException("解析失败, 数据为空")

    soup = BeautifulSoup(html_content, "html.parser")

    title: str | None = None
    cover_url: str | None = None
    video_url: str | None = None
    video_label: str | None = None
    images_urls: list[str] = []
    dynamic_urls: list[str] = []
    audio_urls: list[str] = []

    # 1. 封面：第一个 <img src>
    thumb_tag = soup.find("img")
    if isinstance(thumb_tag, Tag):
        if cover := thumb_tag.get("src"):
            cover_url = str(cover)

    # 2. 下载链接（tw-button-dl 与 abutton 两类，顺序与原插件 chain() 一致）
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
        lowered = text.lower()
        if "mp4" in lowered or "视频" in text:
            if video_url is None:  # 多个清晰度只取第一个（原插件有 break）
                video_url = href
                video_label = text
        elif "gif" in lowered:
            dynamic_urls.append(href)
        elif "mp3" in lowered or "音频" in text or "audio" in lowered:
            audio_urls.append(href)
        elif "图片" in text or "jpg" in lowered or "jpeg" in lowered or "png" in lowered:
            images_urls.append(href)

    # 3. 标题：第一个 <h3>
    title_tag = soup.find("h3")
    if title_tag:
        title = title_tag.get_text(strip=True)
    title, is_repost = clean_title(title)

    # 4. 推文 ID（原插件里被注释掉了，这里保留做 debug 信息）
    tweet_id: str | None = None
    twitter_id_input = soup.find("input", {"id": "TwitterId"})
    if isinstance(twitter_id_input, Tag) and isinstance(twitter_id_input.get("value"), str):
        tweet_id = str(twitter_id_input.get("value"))

    contents: list[Content] = []
    if video_url:
        contents.append(Content(type="video", url=video_url, cover=cover_url, label=video_label))
    contents.extend(Content(type="image", url=u) for u in images_urls)
    contents.extend(Content(type="dynamic", url=u, cover=cover_url) for u in dynamic_urls)
    contents.extend(Content(type="audio", url=u, label="MP3") for u in audio_urls)

    return ParseResult(
        tweet_id=tweet_id,
        title=title,
        cover=cover_url,
        is_repost=is_repost,
        quoted_url=find_quoted_url(html_content, tweet_id),
        contents=contents,
    )


async def _resolve_tco(url: str, config: TwitterConfig, session: aiohttp.ClientSession) -> str:
    """把 t.co 短链还原成真实链接（单次重定向）。"""
    try:
        async with session.get(
            url,
            headers={"User-Agent": USER_AGENT},
            allow_redirects=False,
            proxy=config.proxy,
        ) as resp:
            if resp.status < 400:
                location = resp.headers.get("Location")
                if location:
                    return str(location)
    except Exception:  # noqa: BLE001 - 还原失败就用原链接继续尝试
        pass
    return url


async def parse_tweet(
    input_text: str,
    *,
    config: TwitterConfig | None = None,
    session: aiohttp.ClientSession | None = None,
    fallback: bool = False,
) -> ParseResult:
    """解析一条推特链接（或含链接的文本）。

    Args:
        input_text: 推特链接或包含链接的文本
        config: 解析器配置
        session: 可选的共享 aiohttp 会话（插件里复用，避免每次新建连接池）
        fallback: xdown 失败时是否再尝试 Twitter 官方 syndication 接口

    Raises:
        ParseException: 无法匹配链接、接口异常、接口返回非 ok、返回内容为空
    """
    config = config or TwitterConfig()
    matched = search_url(input_text)
    if matched is None:
        raise ParseException("无法匹配 URL")
    _, searched = matched
    url = f"https://{searched.group(0)}"

    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=config.timeout),
        )
    assert session is not None
    try:
        try:
            return await _parse_via_xdown(url, input_text, config, session)
        except ParseException as primary:
            if not fallback:
                raise
            # 后备：官方 syndication 接口（免登录），失败则抛原始错误更有参考价值
            try:
                from .syndication import parse_syndication

                return await parse_syndication(url, config=config, session=session)
            except Exception as secondary:  # noqa: BLE001
                raise ParseException(f"{primary}；后备接口也失败：{secondary}") from primary
    finally:
        if own_session:
            await session.close()


async def _parse_via_xdown(
    url: str,
    input_text: str,
    config: TwitterConfig,
    session: aiohttp.ClientSession,
) -> ParseResult:
    """xdown.app 主解析通道。"""
    # 可选：t.co 短链还原
    if TCO_RE.search(input_text) and "status/" not in input_text:
        for tco in TCO_RE.findall(input_text):
            real = await _resolve_tco(tco, config, session)
            if search_url(real):
                url = real if real.startswith("http") else f"https://{real}"
                break

    payload = {"q": url, "lang": "zh-cn"}
    last_err: Exception | None = None
    resp_json: dict[str, Any] | None = None
    for attempt in range(config.retry + 1):
        try:
            async with session.post(
                config.endpoint,
                data=payload,
                headers=config.headers(),
                proxy=config.proxy,
            ) as resp:
                if resp.status >= 400:
                    raise ParseException(f"xdown API {resp.status} {resp.reason}")
                resp_json = await resp.json(content_type=None)
            break
        except ParseException:
            raise
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < config.retry:
                await asyncio.sleep(0.8 * (attempt + 1))
    if resp_json is None:
        raise ParseException(f"xdown 请求失败: {last_err}")

    if resp_json.get("status") != "ok":
        # 接口会在 msg 里给「未找到视频/推文是私人的」等原因
        raise ParseException(str(resp_json.get("msg") or "解析失败"))
    data = resp_json.get("data")
    if data is None:
        raise ParseException("解析失败, 数据为空")

    result = parse_twitter_html(data)
    result.url = url
    result.source = "xdown"
    return result

