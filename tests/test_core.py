# -*- coding: utf-8 -*-
"""core 层测试：URL 匹配、HTML 解析（离线 fixture）、真实解析与下载（联网）。

用法：
    pytest tests/test_core.py                     # 全部
    ASTR_TWITTER_SKIP_LIVE=1 pytest tests/test_core.py   # 只跑离线用例
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from core.downloader import sniff_kind
from core.twitter import (
    ParseException,
    TwitterConfig,
    extract_urls,
    parse_tweet,
    parse_twitter_html,
    search_url,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "devtools" / "fixtures"

live = pytest.mark.skipif(
    os.environ.get("ASTR_TWITTER_SKIP_LIVE") == "1",
    reason="ASTR_TWITTER_SKIP_LIVE=1，跳过联网用例",
)

# 来自上游 nonebot-plugin-parser 测试套件的真实推文
URL_VIDEO = "https://x.com/Fortnite/status/1904171341735178552"
URL_PHOTO = "https://x.com/Fortnite/status/1870484479980052921"
URL_PHOTOS = "https://x.com/chitose_yoshino/status/1841416254810378314"
URL_GIF = "https://x.com/Dithmenos9/status/1966798448499286345"
URL_MISSING = "https://x.com/NASA/status/1683502034445783040"


# --------------------------------------------------------------------------- #
# URL 匹配（离线）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("https://x.com/Fortnite/status/1870484479980052921", "https://x.com/Fortnite/status/1870484479980052921"),
        ("https://www.x.com/Fortnite/status/1870484479980052921", "https://www.x.com/Fortnite/status/1870484479980052921"),
        ("https://twitter.com/Fortnite/status/1870484479980052921", "https://twitter.com/Fortnite/status/1870484479980052921"),
        ("https://mobile.twitter.com/a/status/123", "https://mobile.twitter.com/a/status/123"),
        ("https://x.com/i/web/status/1234567890", "https://x.com/i/web/status/1234567890"),
        ("看这个 https://x.com/a_b/status/999?s=46&t=xyz 哈哈", "https://x.com/a_b/status/999"),
    ],
)
def test_search_url_matches(text: str, expected: str) -> None:
    matched = search_url(text)
    assert matched is not None, f"应当匹配：{text}"
    assert f"https://{matched[1].group(0)}" == expected


@pytest.mark.parametrize(
    "text",
    [
        "https://example.com/a/status/123",
        "https://notx.com/a/status/123",  # 前缀不能是字母数字或点
        "https://x.com/Fortnite",  # 没有 status/<id>
        "",
    ],
)
def test_search_url_rejects(text: str) -> None:
    assert search_url(text) is None


def test_extract_urls_dedup_and_order() -> None:
    text = f"两条：{URL_PHOTO} 和 {URL_VIDEO}，重复一次 {URL_PHOTO}"
    assert extract_urls(text) == [URL_PHOTO, URL_VIDEO]


# --------------------------------------------------------------------------- #
# HTML 解析（离线 fixture，对应原插件 parse_twitter_html）
# --------------------------------------------------------------------------- #
def _fixture(name: str) -> str:
    path = FIXTURES / f"xdown-{name}.html"
    if not path.exists():
        pytest.skip(f"缺少 fixture：{path}")
    return path.read_text(encoding="utf-8")


def test_parse_fixture_gif() -> None:
    result = parse_twitter_html(_fixture("gif"))
    assert result.counts["dynamic"] == 1
    assert "dl.snapcdn.app" in result.contents[-1].url
    assert result.cover and result.cover.startswith("https://pbs.twimg.com")
    assert result.title
    assert result.tweet_id == "1966798448499286345"
    # gif 推文里的「下载图片」是缩略图，原插件也会带上 → 1 张图
    assert result.counts["image"] == 1


def test_parse_fixture_video() -> None:
    result = parse_twitter_html(_fixture("video"))
    assert result.counts["video"] == 1
    assert result.cover
    assert result.title and "Lucky" in result.title
    assert result.tweet_id == "1904171341735178552"


def test_parse_fixture_photo() -> None:
    result = parse_twitter_html(_fixture("photo"))
    assert result.counts["image"] == 1
    assert result.counts["video"] == 0


def test_parse_empty_html_raises() -> None:
    with pytest.raises(ParseException):
        parse_twitter_html("")


async def test_search_url_error_for_plain_text() -> None:
    with pytest.raises(ParseException):
        await parse_tweet("随便一句话，没有链接")


# --------------------------------------------------------------------------- #
# 文件头嗅探（离线）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("head", "kind"),
    [
        (b"\xff\xd8\xff\xe0abc", "jpeg"),
        (b"\x89PNG\r\n\x1a\n", "png"),
        (b"GIF89a", "gif"),
        (b"\x00\x00\x00\x18ftypmp42", "mp4"),
        (b"RIFF....WEBP", "webp"),
        (b"unknown-bytes", "unknown"),
    ],
)
def test_sniff_kind(head: bytes, kind: str) -> None:
    assert sniff_kind(head) == kind


# --------------------------------------------------------------------------- #
# 真实解析（联网）
# --------------------------------------------------------------------------- #
@live
async def test_live_video() -> None:
    result = await parse_tweet(URL_VIDEO, config=TwitterConfig(retry=3))
    assert result.counts["video"] == 1
    assert result.title
    assert result.cover


@live
async def test_live_single_photo() -> None:
    result = await parse_tweet(URL_PHOTO, config=TwitterConfig(retry=3))
    assert result.counts["image"] >= 1


@live
async def test_live_multi_photos() -> None:
    result = await parse_tweet(URL_PHOTOS, config=TwitterConfig(retry=3))
    assert result.counts["image"] >= 2


@live
async def test_live_gif() -> None:
    result = await parse_tweet(URL_GIF, config=TwitterConfig(retry=3))
    assert result.counts["dynamic"] == 1


@live
async def test_live_twitter_domain() -> None:
    result = await parse_tweet(
        "https://twitter.com/Fortnite/status/1870484479980052921",
        config=TwitterConfig(retry=3),
    )
    assert result.counts["image"] >= 1


@live
async def test_live_missing_tweet_raises() -> None:
    with pytest.raises(ParseException):
        await parse_tweet(URL_MISSING, config=TwitterConfig(retry=1))
