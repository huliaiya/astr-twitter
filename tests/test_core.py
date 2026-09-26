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

from core.downloader import DownloadException, Downloader, sniff_kind
from core.history import HistoryRecord, HistoryStore
from core.syndication import (
    parse_syndication_json,
    syndication_token,
    to_radix36,
)
from core.twitter import (
    ParseException,
    TwitterConfig,
    clean_title,
    extract_urls,
    find_quoted_url,
    parse_tweet,
    parse_twitter_html,
    search_url,
    truncate,
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


# --------------------------------------------------------------------------- #
# 正文清洗 / 引用识别（离线）
# --------------------------------------------------------------------------- #
def test_clean_title_strips_rt_prefix_and_tco() -> None:
    title, is_repost = clean_title("RT @someone: 来看看 https://t.co/abc123 这个")
    assert is_repost is True
    assert title == "来看看  这个"
    assert clean_title("普通正文 https://t.co/xyz") == ("普通正文", False)
    assert clean_title(None) == (None, False)
    assert clean_title("") == (None, False)


def test_truncate() -> None:
    assert truncate("abcdef", 3) == "abc…"
    assert truncate("abc", 10) == "abc"
    assert truncate("abc", 0) == "abc"
    assert truncate(None, 5) is None


def test_find_quoted_url() -> None:
    html = '<a href="https://x.com/me/status/1966798448499286345">me</a>'
    assert find_quoted_url(html, "1966798448499286345") is None
    html2 = html + '<a href="https://twitter.com/other/status/111">quoted</a>'
    assert find_quoted_url(html2, "1966798448499286345") == "https://x.com/other/status/111"


def test_parse_audio_button() -> None:
    """接口若返回音频（MP3）按钮，应解析成 audio 类型。"""
    html = (
        "<div><img src='https://pbs.twimg.com/cover.jpg'></div>"
        "<a class='tw-button-dl' href='https://dl.example/v.mp4'>下载 MP4 720p</a>"
        "<a class='abutton' href='https://dl.example/a.mp3'>下载 MP3</a>"
        "<h3>有音频的推文</h3>"
    )
    result = parse_twitter_html(html)
    assert result.counts["video"] == 1
    assert result.counts["audio"] == 1
    assert result.contents[-1].type == "audio"
    assert result.contents[-1].label == "MP3"


def test_parse_rt_html_marks_repost() -> None:
    html = (
        "<a class='abutton' href='https://dl.example/1.jpg'>下载图片</a>"
        "<h3>RT @orig: 原推正文</h3>"
    )
    result = parse_twitter_html(html)
    assert result.is_repost is True
    assert result.title == "原推正文"


# --------------------------------------------------------------------------- #
# syndication 后备接口（离线；token 与本机 node 输出逐一比对过）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("tweet_id", "token"),
    [
        ("1904171341735178552", "4m64pdv2qbu"),
        ("1870484479980052921", "4j8at6uan5x"),
        ("1966798448499286345", "4rmvnwlu58w"),
        ("1683502034445783040", "42wvleeu7qr"),
        ("1", "bhi2ay3f28n"),
        ("555", "4x2thirmp7j"),
        ("1234567890123456789", "2zqic77uqyk"),
        ("999999999999999999", "2f9lc2ug9mm"),
    ],
)
def test_syndication_token_matches_v8(tweet_id: str, token: str) -> None:
    """token 必须和浏览器（V8 的 Number.toString(36)）算出来的一模一样。"""
    assert syndication_token(tweet_id) == token


def test_to_radix36_matches_js() -> None:
    assert to_radix36(255.5) == "73.i"
    assert to_radix36(0.5) == "0.i"
    assert to_radix36(36.0) == "10"


def test_parse_syndication_json_video() -> None:
    payload = {
        "id_str": "1904171341735178552",
        "text": "Lucky 33",
        "user": {"name": "Fortnite", "screen_name": "Fortnite"},
        "mediaDetails": [
            {
                "type": "video",
                "media_url_https": "https://pbs.twimg.com/cover.jpg",
                "video_info": {
                    "variants": [
                        {"content_type": "video/mp4", "bitrate": 256000, "url": "https://video.twimg.com/low.mp4"},
                        {"content_type": "video/mp4", "bitrate": 2176000, "url": "https://video.twimg.com/high.mp4"},
                        {"content_type": "application/x-mpegURL", "url": "https://video.twimg.com/x.m3u8"},
                    ]
                },
            }
        ],
    }
    result = parse_syndication_json(payload, url="https://x.com/Fortnite/status/1904171341735178552")
    assert result.source == "syndication"
    assert result.author_name == "Fortnite"
    assert result.counts["video"] == 1
    # 取码率最高的 mp4，忽略 m3u8
    assert result.contents[0].url == "https://video.twimg.com/high.mp4"
    assert result.cover == "https://pbs.twimg.com/cover.jpg?name=orig"


def test_parse_syndication_json_photos_and_gif() -> None:
    payload = {
        "id_str": "1",
        "text": "多图",
        "user": {"name": "someone"},
        "mediaDetails": [
            {"type": "photo", "media_url_https": "https://pbs.twimg.com/1.jpg"},
            {"type": "photo", "media_url_https": "https://pbs.twimg.com/2.jpg?name=small"},
            {
                "type": "animated_gif",
                "media_url_https": "https://pbs.twimg.com/t.jpg",
                "video_info": {"variants": [{"content_type": "video/mp4", "url": "https://video.twimg.com/g.mp4"}]},
            },
        ],
    }
    result = parse_syndication_json(payload)
    assert result.counts == {"video": 0, "image": 2, "dynamic": 1, "audio": 0}
    assert result.contents[1].url == "https://pbs.twimg.com/2.jpg?name=small"  # 已带参数就不改


def test_parse_syndication_json_empty_raises() -> None:
    with pytest.raises(ParseException):
        parse_syndication_json({})
    with pytest.raises(ParseException):
        parse_syndication_json({"text": "没有媒体"})


# --------------------------------------------------------------------------- #
# 下载器：流式限流 / 并发 / 缓存 / 落盘（离线，用假会话）
# --------------------------------------------------------------------------- #
class _FakeContent:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    def iter_chunked(self, _size: int):
        async def gen():
            for chunk in self._chunks:
                yield chunk

        return gen()


class _FakeResponse:
    def __init__(self, url: str, chunks: list[bytes], status: int = 200) -> None:
        self.url = url
        self.status = status
        self.reason = "OK"
        self.headers = {"Content-Type": "video/mp4"}
        self.content = _FakeContent(chunks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _FakeSession:
    def __init__(self, mapping: dict[str, object]) -> None:
        self.mapping = mapping
        self.calls: list[str] = []

    def get(self, url: str, **kwargs):
        self.calls.append(url)
        target = self.mapping[url]
        if isinstance(target, Exception):
            raise target
        return target


MP4_HEAD = b"\x00\x00\x00\x18ftypmp42"


async def test_downloader_writes_file_and_sniffs_kind(tmp_path) -> None:
    url = "https://cdn.example/v.mp4"
    session = _FakeSession({url: _FakeResponse(url, [MP4_HEAD, b"x" * 100])})
    downloader = Downloader(session, base_dir=tmp_path)  # type: ignore[arg-type]
    media = await downloader.download(url, subdir="123")
    assert media.kind == "mp4"
    assert media.path.suffix == ".mp4"
    assert media.path.exists() and media.size == len(MP4_HEAD) + 100
    assert media.path.parent.name == "123"


async def test_downloader_aborts_over_size_limit(tmp_path) -> None:
    url = "https://cdn.example/big.mp4"
    session = _FakeSession({url: _FakeResponse(url, [b"y" * 40, b"z" * 40])})
    downloader = Downloader(session, base_dir=tmp_path, max_bytes=50)  # type: ignore[arg-type]
    with pytest.raises(DownloadException, match="大小上限"):
        await downloader.download(url)
    assert list(tmp_path.glob("*.mp4")) == []  # 超限不留残文件


async def test_downloader_cache_and_download_many(tmp_path) -> None:
    good = "https://cdn.example/a.jpg"
    bad = "https://cdn.example/b.jpg"
    session = _FakeSession(
        {
            good: _FakeResponse(good, [b"\xff\xd8\xff" + b"a" * 50]),
            bad: RuntimeError("boom"),
        }
    )
    downloader = Downloader(session, base_dir=tmp_path)  # type: ignore[arg-type]

    results = await downloader.download_many(
        [(good, {"subdir": "1"}), (bad, {"subdir": "1"}), (good, {"subdir": "1"})]
    )
    assert len(results) == 3
    assert results[0].kind == "jpeg" and results[2].kind == "jpeg"
    assert isinstance(results[1], DownloadException)
    assert results[0].path == results[2].path  # 命中缓存，不会下两次
    assert session.calls.count(good) == 1
    assert session.calls.count(bad) == 1


# --------------------------------------------------------------------------- #
# 解析历史（离线）
# --------------------------------------------------------------------------- #
async def test_history_store_roundtrip(tmp_path) -> None:
    store = HistoryStore(tmp_path / "history.json", max_records=3)
    assert await store.list() == []
    assert (await store.stats())["total"] == 0

    for index in range(5):
        await store.add(
            HistoryRecord(
                url=f"https://x.com/a/status/{index}",
                tweet_id=str(index),
                ok=index != 3,
                counts={"image": 1},
                media=1,
                bytes=100,
                author="alice",
                error="失败原因" if index == 3 else None,
            )
        )

    records = await store.list()
    assert len(records) == 3  # 超过上限后只留最新
    assert records[0]["url"].endswith("/4")
    stats = await store.stats()
    assert stats["total"] == 3
    assert stats["failed"] == 1
    assert stats["counts"]["image"] == 3
    assert stats["top_authors"] == [{"name": "alice", "count": 3}]

    assert await store.clear() == 3
    assert await store.list() == []


async def test_history_store_tolerates_broken_file(tmp_path) -> None:
    path = tmp_path / "history.json"
    path.write_text("{不是 json", encoding="utf-8")
    store = HistoryStore(path)
    assert await store.list() == []
    await store.add(HistoryRecord(url="https://x.com/a/status/1"))
    assert len(await store.list()) == 1
