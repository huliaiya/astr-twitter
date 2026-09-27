# -*- coding: utf-8 -*-
"""Twitter/X 官方 syndication 接口后备解析（免登录）。

当 xdown.app 返回失败或数据为空时，退回调用：

    https://cdn.syndication.twimg.com/tweet-result?id=<tweet_id>&token=<token>&lang=zh

`token` 是X前端用推文 ID 算出来的固定串（见 `syndication_token()`）。
这个接口不需要任何登录凭证，返回的 JSON 里自带：

  - `text`            正文
  - `user.name`      作者昵称
  - `mediaDetails[]` 媒体（photo / video / animated_gif）
  - `video_info.variants[]` 各清晰度的 mp4 直链

注意：
  - 该接口对部分推文会返回错误或空体（受保护/已删除/年龄限制），此时抛 ParseException；
  - 直链域名是 `pbs.twimg.com` / `video.twimg.com`，在部分地区的服务器上可能直连不通，
    这种情况请配置插件里的 `proxy`。
"""

from __future__ import annotations

import math
import re
from typing import Any

import aiohttp

from .twitter import (
    USER_AGENT,
    Content,
    ParseException,
    ParseResult,
    TwitterConfig,
    clean_title,
)

SYNDICATION_ENDPOINT = "https://cdn.syndication.twimg.com/tweet-result"

STATUS_ID_RE = re.compile(r"/status/(\d+)")


def syndication_token(tweet_id: str) -> str:
    """复刻X前端的 token：`((id / 1e15) * π).toString(36).replace(/0+|\\./g, "")`。

    JS 的 `Number.prototype.toString(radix)`（V8 的 DoubleToRadix）在非十进制下会
    一直输出小数位，直到「再多的位也影响不到这个 double」为止，所以这里按 V8 的
    delta 算法逐位复刻（见 `to_radix36`），再用同一个正则去掉 0 与小数点。
    """
    value = (int(tweet_id) / 1e15) * math.pi
    return re.sub(r"0+|\.", "", to_radix36(value))


_DIGITS36 = "0123456789abcdefghijklmnopqrstuvwxyz"
_MIN_DENORMAL = 5e-324


def to_radix36(value: float) -> str:
    """等价于 JS `(value).toString(36)`（复刻 V8 DoubleToRadix）。"""
    integer = math.floor(value)
    fraction = value - integer
    out = _int_to_radix36(int(integer))
    if fraction <= 0:
        return out

    out += "."
    delta = max(0.5 * (math.nextafter(value, math.inf) - value), _MIN_DENORMAL)
    while True:
        delta *= 36
        fraction *= 36
        digit = int(fraction)
        out += _DIGITS36[digit]
        fraction -= digit
        if fraction > 0.5 or (fraction == 0.5 and digit % 2 == 1):
            if fraction + delta > 1:
                return _radix36_increment(out)
        if fraction < delta:
            return out


def _radix36_increment(text: str) -> str:
    """把最后一位 +1，处理进位（V8 在舍入越界时会回溯进位）。"""
    chars = list(text)
    index = len(chars) - 1
    while index >= 0:
        char = chars[index]
        if char == ".":
            index -= 1
            continue
        position = _DIGITS36.index(char)
        if position + 1 < 36:
            chars[index] = _DIGITS36[position + 1]
            return "".join(chars)
        chars[index] = "0"
        index -= 1
    whole, _, frac = text.partition(".")
    return _int_to_radix36(int(whole or "0", 36) + 1) + "." + "0" * len(frac)


def _int_to_radix36(value: int) -> str:
    if value == 0:
        return "0"
    out = ""
    n = abs(value)
    while n > 0:
        out = _DIGITS36[n % 36] + out
        n //= 36
    return f"-{out}" if value < 0 else out


def extract_tweet_id(url: str) -> str | None:
    match = STATUS_ID_RE.search(url or "")
    return match.group(1) if match else None


def _pick_video_url(media: dict[str, Any]) -> str | None:
    """从 video_info.variants 里挑一条可用的 mp4（优先最高码率）。"""
    info = media.get("video_info") or {}
    variants = info.get("variants") or []
    best_url: str | None = None
    best_bitrate = -1
    fallback: str | None = None
    for variant in variants:
        if not isinstance(variant, dict):
            continue
        url = variant.get("url")
        if not isinstance(url, str) or not url:
            continue
        if "m3u8" in url:
            continue  # HLS 播放列表不是文件，跳过
        if variant.get("content_type") not in (None, "video/mp4"):
            continue
        bitrate = variant.get("bitrate")
        if isinstance(bitrate, int) and bitrate > best_bitrate:
            best_bitrate = bitrate
            best_url = url
        if fallback is None:
            fallback = url
    return best_url or fallback


def _first_photo_url(media: dict[str, Any]) -> str | None:
    url = media.get("media_url_https") or media.get("media_url")
    if isinstance(url, str) and url:
        return url if "?" in url else f"{url}?name=orig"
    return None


def parse_syndication_json(payload: dict[str, Any], url: str = "") -> ParseResult:
    """把 syndication JSON 映射成统一的 ParseResult。"""
    if not isinstance(payload, dict) or not payload:
        raise ParseException("syndication 返回为空")

    tweet_id = str(payload.get("id_str") or payload.get("id") or extract_tweet_id(url) or "")

    user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    author = None
    handle = None
    if isinstance(user, dict):
        author = user.get("name") or user.get("screen_name")
        screen_name = user.get("screen_name")
        if isinstance(screen_name, str) and screen_name:
            handle = screen_name
    title, is_repost = clean_title(payload.get("text") if isinstance(payload.get("text"), str) else None)

    cover: str | None = None
    contents: list[Content] = []

    # 1) mediaDetails 为主
    media_details = payload.get("mediaDetails")
    if not isinstance(media_details, list):
        media_details = []
    for media in media_details:
        if not isinstance(media, dict):
            continue
        kind = media.get("type")
        if kind == "photo":
            photo = _first_photo_url(media)
            if photo:
                contents.append(Content(type="image", url=photo))
                cover = cover or photo
        elif kind in ("video", "animated_gif"):
            video = _pick_video_url(media)
            if not video:
                continue
            media_cover = _first_photo_url(media)
            cover = cover or media_cover
            contents.append(
                Content(
                    type="dynamic" if kind == "animated_gif" else "video",
                    url=video,
                    cover=media_cover,
                    label="syndication",
                )
            )

    # 2) 老结构兜底：photos / entities.media
    if not contents:
        photos = payload.get("photos")
        if isinstance(photos, list):
            for photo in photos:
                if isinstance(photo, dict) and isinstance(photo.get("url"), str):
                    contents.append(Content(type="image", url=photo["url"]))
                    cover = cover or photo["url"]

    if not contents:
        raise ParseException("syndication 未返回任何媒体")

    duration = payload.get("video") if isinstance(payload.get("video"), dict) else {}
    duration_text: str | None = None
    if isinstance(duration, dict):
        seconds = duration.get("durationMs")
        if isinstance(seconds, (int, float)) and seconds > 0:
            total = int(round(seconds / 1000))
            duration_text = f"{total // 60}:{total % 60:02d}"

    cover_is_content = any(
        (item.url or "").split("?", 1)[0] == (cover or "").split("?", 1)[0] for item in contents
    )
    return ParseResult(
        url=url,
        tweet_id=tweet_id or None,
        title=title,
        author_name=author or "无用户名",
        author_handle=handle,
        cover=cover,
        contents=contents,
        source="syndication",
        is_repost=is_repost,
        quoted_url=None,
        duration=duration_text,
        cover_is_content=cover_is_content,
    )


async def parse_syndication(
    url: str,
    *,
    config: TwitterConfig | None = None,
    session: aiohttp.ClientSession | None = None,
) -> ParseResult:
    """调用 syndication 接口解析（xdown 失败时的后备通道）。"""
    config = config or TwitterConfig()
    tweet_id = extract_tweet_id(url)
    if not tweet_id:
        raise ParseException("无法从链接里取到推文 ID")

    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=config.timeout))
    assert session is not None
    try:
        params = {
            "id": tweet_id,
            "token": syndication_token(tweet_id),
            "lang": "zh",
        }
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        async with session.get(
            SYNDICATION_ENDPOINT,
            params=params,
            headers=headers,
            proxy=config.proxy,
        ) as resp:
            if resp.status >= 400:
                raise ParseException(f"syndication API {resp.status} {resp.reason}")
            text = await resp.text()
        if not text.strip():
            raise ParseException("syndication 返回空体")
        import json as _json

        payload = _json.loads(text)
        return parse_syndication_json(payload, url=url)
    finally:
        if own_session:
            await session.close()
