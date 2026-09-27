# -*- coding: utf-8 -*-
"""astr-twitter 的X解析核心（不依赖 AstrBot，可独立测试）。

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
import base64
import json
import re
import urllib.parse
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
    url: str  # 实际下载地址（xdown 的中转链，或直链）
    cover: str | None = None
    label: str | None = None  # 如 "720p" / "MP3"，用于日志与排查
    target_url: str | None = None  # 中转链里解出来的原始地址（video.twimg.com / pbs.twimg.com）
    filename: str | None = None  # 建议文件名（来自中转链）
    is_cover: bool = False  # 这张图其实就是视频/GIF 的封面缩略图

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
    author_handle: str | None = None  # 从链接里取到的 @handle，如 Fortnite
    duration: str | None = None  # 视频/GIF 时长，如 "0:07"
    cover_is_content: bool = False  # 封面本身就在 contents 里（图片推文），别重复发

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
    fallback_syndication: bool = True

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


# --------------------------------------------------------------------------- #
# 小工具：URL 校验 / 中转链解码 / 文件名 / 正文与标题
# --------------------------------------------------------------------------- #
# 明显不是推文媒体的图片（头像、表情、图标），不要当封面
JUNK_IMAGE_RE = re.compile(
    r"(profile_images|/emoji/|twemoji|/icon|\.svg($|[?#])|data:image)",
    re.IGNORECASE,
)

# 分辨率，如 /1280x720/
RESOLUTION_RE = re.compile(r"/(\d{3,4})x(\d{3,4})/")
# 时长，如 <p>0:07</p>
DURATION_RE = re.compile(r"^\s*(\d{1,2}:\d{2})\s*$")


def is_http_url(url: Any) -> bool:
    """只接受 http(s) 绝对地址：#、/、javascript:、空值、相对路径一律拒绝。

    xdown 的返回里既有真链接，也有 `href="#"`（转换为 MP3 等占位按钮）
    与 `href="/"`（下载更多视频），这些拿去下载会抛 InvalidUrlClientError。
    """
    if not isinstance(url, str):
        return False
    url = url.strip()
    return url.startswith("http://") or url.startswith("https://")


def is_media_image_url(url: Any) -> bool:
    """能当封面的图片地址（排除头像/表情/图标）。"""
    return is_http_url(url) and not JUNK_IMAGE_RE.search(str(url))


def decode_snapcdn(url: str) -> dict[str, str] | None:
    """解开 xdown 中转链 `dl.snapcdn.app/get?token=<JWT>` 里的原始地址与文件名。

    只是读 JWT 的 payload（不校验签名，也不改变下载走中转链的行为），
    用来：判断「下载图片」是不是视频封面、拿到分辨率、给落地文件起个正常名字。
    """
    if not is_http_url(url) or "token=" not in url:
        return None
    try:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        token = (query.get("token") or [""])[0]
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except Exception:  # noqa: BLE001 - 解不开就算了
        return None
    if not isinstance(data, dict):
        return None
    target = data.get("url")
    filename = data.get("filename")
    result: dict[str, str] = {}
    if isinstance(target, str) and target:
        result["target_url"] = target
    if isinstance(filename, str) and filename:
        result["filename"] = filename
    return result or None


def media_key(url: str | None) -> str:
    """比较两条媒体是否同一份内容（忽略查询参数与大小写）。"""
    if not url:
        return ""
    return url.split("?", 1)[0].strip().lower()


def sanitize_filename(name: str | None, fallback_ext: str = ".bin") -> str | None:
    """把中转链给的文件名洗成安全的文件名。"""
    if not name:
        return None
    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", str(name)).strip().strip(".")
    name = re.sub(r"\s+", "_", name)
    if not name:
        return None
    if "." not in name:
        name += fallback_ext
    return name[:120]


def resolution_label(target_url: str | None, text: str = "") -> str | None:
    """从按钮文案 `(720p)` 或原始地址 `/1280x720/` 里取分辨率标签。"""
    match = re.search(r"\((\d{3,4})p\)", text or "")
    if match:
        return f"{match.group(1)}p"
    if target_url:
        found = RESOLUTION_RE.search(target_url)
        if found:
            width, height = int(found.group(1)), int(found.group(2))
            return f"{min(width, height)}p"
    return None


def extract_handle(url: str) -> str | None:
    """从推文链接里取作者 handle：x.com/Fortnite/status/123 → Fortnite。"""
    match = re.search(
        r"(?:x|twitter)\.com/(?!i/web)([A-Za-z0-9_]{1,20})/status/\d+",
        url or "",
        re.IGNORECASE,
    )
    return match.group(1) if match else None


def format_media_info(result: "ParseResult", *, emoji: bool = False) -> str:
    """把「视频 · 720p · 0:07 / 图片 ×3」这类媒体信息拼成一行。"""
    parts: list[str] = []
    videos = [c for c in result.contents if c.type == "video"]
    gifs = [c for c in result.contents if c.type == "dynamic"]
    images = [c for c in result.contents if c.type == "image"]
    audios = [c for c in result.contents if c.type == "audio"]

    def build(name: str, count: int, label: str, extra: list[str]) -> str:
        head = f"{name} ×{count}" if count > 1 else name
        detail = [d for d in extra if d]
        text = f"{head} · {' · '.join(detail)}" if detail else head
        return f"{label} {text}".strip()

    if videos:
        parts.append(build("视频", len(videos), "🎬" if emoji else "", [videos[0].label, result.duration]))
    if gifs:
        parts.append(build("GIF", len(gifs), "🎞️" if emoji else "", [result.duration]))
    if images:
        parts.append(build("图片", len(images), "🖼️" if emoji else "", []))
    if audios:
        parts.append(build("音频", len(audios), "🎵" if emoji else "", []))
    return " ＋ ".join(parts)


def format_elapsed(seconds: float) -> str:
    """把耗时格式化成给人看的样子：0.8s / 12.3s / 1m05s。"""
    try:
        seconds = max(0.0, float(seconds))
    except (TypeError, ValueError):
        return "-"
    if seconds < 10:
        return f"{seconds:.1f}s"
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes}m{rest:02d}s"


def build_caption(
    result: "ParseResult",
    *,
    include_text: bool = True,
    include_author: bool = True,
    include_media_info: bool = True,
    include_link: bool = False,
    emoji: bool = False,
    max_chars: int = 300,
    elapsed: float | None = None,
    show_elapsed: bool = False,
) -> str:
    """拼发送用的简介：解析耗时、作者、媒体信息、正文、链接（都可单独关闭）。"""
    lines: list[str] = []

    if show_elapsed and elapsed is not None:
        lines.append(f"{'⏱ ' if emoji else ''}解析耗时 {format_elapsed(elapsed)}")

    if include_author:
        handle = (result.author_handle or "").lstrip("@")
        name = result.author_name if result.author_name != "无用户名" else ""
        if handle and name:
            author = f"{name} (@{handle})"
        else:
            author = f"@{handle}" if handle else name
        if author:
            if result.is_repost:
                author += " · 转发"
            lines.append(f"作者：{author}")

    if include_media_info:
        info = format_media_info(result, emoji=emoji)
        if info:
            lines.append(info)

    if include_text:
        text = truncate(result.title, max_chars)
        if text:
            lines.append(text)

    if include_link and result.url:
        lines.append(result.url)

    return "\n".join(lines)


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
    """提取文本中所有X链接（去重、保序、补上 https://）。"""
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
    """解析 xdown 返回的 HTML。

    相比原插件额外做了这些事（都是线上踩过的坑）：
      - 只接受 http(s) 绝对地址：`href="#"`（转换为 MP3 等占位按钮）、`href="/"`
        （下载更多视频）拿去下载会抛 InvalidUrlClientError；
      - 跳过 `action-convert` 之类的转换按钮，只认「下载 xxx」；
      - 解开中转链里的原始地址，用来识别「下载图片」其实是视频封面、取分辨率、起文件名；
      - 视频/GIF 推文里的那张图就是封面缩略图，归到 cover，不重复当图片发；
      - 封面取第一张真正的媒体图（排除头像/表情/图标）；
      - 顺带解析时长。
    """
    if not html_content:
        raise ParseException("解析失败, 数据为空")

    soup = BeautifulSoup(html_content, "html.parser")

    title: str | None = None
    cover_url: str | None = None
    duration: str | None = None

    # 1. 封面：优先 xdown 的缩略图容器（.thumbnail/.image-tw/.tw-left），
    #    再退回第一张真正的媒体图 —— 跳过头像、表情（twemoji）和各种图标，
    #    否则群里收到的「封面」可能是一张表情或图钉。
    candidates: list[Tag] = []
    for selector in (".thumbnail img", ".image-tw img", ".tw-left img"):
        candidates.extend(tag for tag in soup.select(selector) if isinstance(tag, Tag))
    candidates.extend(tag for tag in soup.find_all("img") if isinstance(tag, Tag))
    for img in candidates:
        src = img.get("src")
        if is_media_image_url(src):
            cover_url = str(src).strip()
            break

    contents: list[Content] = []
    seen: set[str] = set()

    # 2. 下载按钮：tw-button-dl 与 abutton 两类，顺序与原插件一致
    tw_button_tags = soup.find_all("a", class_="tw-button-dl")
    abutton_tags = soup.find_all("a", class_="abutton")
    for tag in chain(tw_button_tags, abutton_tags):
        if not isinstance(tag, Tag):
            continue
        classes = [str(c) for c in (tag.get("class") or [])]
        if "action-convert" in classes:
            continue  # 「转换为 MP3/GIF」是占位按钮（href="#"），真转换走 POST
        text = tag.get_text(strip=True)
        lowered = text.lower()
        if not text.startswith("下载"):
            continue  # 只认「下载 MP4 / 下载图片 / 下载 gif」，排除「下载更多视频」
        href = tag.get("href")
        if not is_http_url(href):
            continue  # # / / / javascript: 一律跳过
        href = str(href).strip()

        info = decode_snapcdn(href) or {}
        target = info.get("target_url")
        key = media_key(target or href)
        if key in seen:
            continue
        seen.add(key)

        filename = sanitize_filename(
            info.get("filename"), ".mp4" if ("mp4" in lowered or "gif" in lowered) else ".jpg"
        )
        if "mp4" in lowered or "视频" in text:
            # 多个清晰度只取第一个（与原插件一致，通常是 720p）
            if any(item.type == "video" for item in contents):
                continue
            contents.append(
                Content(
                    type="video",
                    url=href,
                    cover=cover_url,
                    label=resolution_label(target, text) or text or None,
                    target_url=target,
                    filename=filename,
                )
            )
        elif "gif" in lowered:
            contents.append(
                Content(
                    type="dynamic",
                    url=href,
                    cover=cover_url,
                    label=resolution_label(target, text),
                    target_url=target,
                    filename=filename,
                )
            )
        elif "mp3" in lowered or "音频" in text or "audio" in lowered:
            contents.append(
                Content(type="audio", url=href, label="MP3", target_url=target, filename=filename)
            )
        elif "图片" in text or "jpg" in lowered or "jpeg" in lowered or "png" in lowered:
            if target and JUNK_IMAGE_RE.search(target):
                continue  # 表情/头像/图标，不是推文图片
            contents.append(
                Content(
                    type="image",
                    url=href,
                    label=resolution_label(target, text),
                    target_url=target,
                    filename=filename,
                )
            )

    # 3. 标题：第一个 <h3>
    title_tag = soup.find("h3")
    if title_tag:
        title = title_tag.get_text(strip=True)
    title, is_repost = clean_title(title)

    # 4. 时长：内容区里的 <p>0:07</p>
    for paragraph in soup.find_all("p"):
        if not isinstance(paragraph, Tag):
            continue
        matched = DURATION_RE.match(paragraph.get_text(strip=True))
        if matched:
            duration = matched.group(1)
            break

    # 5. 视频/GIF 推文里的「下载图片」= 视频缩略图，算封面而不是独立图片
    has_moving = any(item.is_video_like for item in contents)
    kept: list[Content] = []
    for item in contents:
        if (
            item.type == "image"
            and has_moving
            and media_key(item.target_url or item.url) == media_key(cover_url)
        ):
            item.is_cover = True
            cover_url = cover_url or item.target_url or item.url
            continue
        kept.append(item)
    contents = kept
    # 只有图片的推文：封面就是其中一张，别重复发
    cover_is_content = any(
        media_key(item.target_url or item.url) == media_key(cover_url) for item in contents
    )

    # 6. 推文 ID（原插件里被注释掉了，这里保留做 debug 信息）
    tweet_id: str | None = None
    twitter_id_input = soup.find("input", {"id": "TwitterId"})
    if isinstance(twitter_id_input, Tag) and isinstance(twitter_id_input.get("value"), str):
        tweet_id = str(twitter_id_input.get("value"))

    return ParseResult(
        tweet_id=tweet_id,
        title=title,
        cover=cover_url,
        is_repost=is_repost,
        quoted_url=find_quoted_url(html_content, tweet_id),
        contents=contents,
        duration=duration,
        cover_is_content=cover_is_content,
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
    """解析一条X链接（或含链接的文本）。

    Args:
        input_text: X链接或包含链接的文本
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
    result.author_handle = extract_handle(url)
    return result

