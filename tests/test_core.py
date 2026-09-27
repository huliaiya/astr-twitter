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
    Content,
    ParseException,
    ParseResult,
    TwitterConfig,
    build_caption,
    clean_title,
    decode_snapcdn,
    extract_handle,
    extract_urls,
    find_quoted_url,
    format_media_info,
    is_http_url,
    is_media_image_url,
    parse_tweet,
    parse_twitter_html,
    resolution_label,
    sanitize_filename,
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
    # GIF 推文里的「下载图片」是视频缩略图 = 封面，不再当成独立图片重复发
    assert result.counts["image"] == 0
    assert result.cover_is_content is False
    assert result.duration == "0:06" if result.duration else True


def test_parse_fixture_video() -> None:
    result = parse_twitter_html(_fixture("video"))
    assert result.counts["video"] == 1
    assert result.cover and "GmxZe5AWwAEuoiK" in result.cover
    assert result.title and "Lucky" in result.title
    assert result.tweet_id == "1904171341735178552"
    # 封面缩略图不再混进媒体列表
    assert result.counts["image"] == 0
    assert result.contents[0].label == "720p"
    # 「转换为 MP3」是 href="#" 的占位按钮，绝不能当音频链接
    assert result.counts["audio"] == 0
    video = result.contents[0]
    assert video.target_url and video.target_url.startswith("https://video.twimg.com")
    assert video.filename and video.filename.endswith(".mp4")


def test_parse_fixture_photo() -> None:
    result = parse_twitter_html(_fixture("photo"))
    assert result.counts["image"] == 1
    assert result.counts["video"] == 0
    # 图片推文的封面就是这张图本身
    assert result.cover_is_content is True
    image = result.contents[0]
    assert image.target_url == "https://pbs.twimg.com/media/GfVLtIKWEAAC7vS.jpg"
    assert image.filename == "XDown.app_GfVLtIKWEAAC7vS.jpg"


def test_no_placeholder_urls_anywhere() -> None:
    """任何 fixture 里都不该出现 # / 相对路径这种下载会炸的地址。"""
    for name in ("video", "photo", "gif"):
        result = parse_twitter_html(_fixture(name))
        for item in result.contents:
            assert item.url.startswith("https://"), f"{name}: {item.url}"


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


# --------------------------------------------------------------------------- #
# URL 清洗 / 中转链解码 / 简介拼接（本次修复 InvalidUrlClientError 的核心）
# --------------------------------------------------------------------------- #
def test_is_http_url_rejects_placeholders() -> None:
    for bad in ["#", "/", "", None, "javascript:void(0)", "dl.snapcdn.app/x", " x", 123]:
        assert is_http_url(bad) is False, bad
    for good in ["https://a.b/c", "http://a.b", "  https://a.b  "]:
        assert is_http_url(good) is True, good


def test_media_image_url_filters_avatar_and_emoji() -> None:
    assert is_media_image_url("https://pbs.twimg.com/media/x.jpg") is True
    for junk in [
        "https://pbs.twimg.com/profile_images/1/avatar.jpg",
        "https://abs.twimg.com/emoji/v2/72x72/1f600.png",
        "https://x/twemoji/1f600.svg",
        "https://x/icon.png",
        "https://x/foo.svg",
        "#",
    ]:
        assert is_media_image_url(junk) is False, junk


def test_decode_snapcdn_reads_payload() -> None:
    """中转链里能解出原始地址与文件名（不改下载行为，只用于判断与起名）。"""
    import re as _re

    html = _fixture("video")
    href = _re.search(r'href="(https://dl\.snapcdn\.app/get\?token=[^"]+)"', html).group(1)
    info = decode_snapcdn(href)
    assert info is not None
    assert info["target_url"].startswith("https://video.twimg.com/")
    assert info["filename"].endswith("_720p.mp4")
    assert decode_snapcdn("#") is None
    assert decode_snapcdn("https://dl.snapcdn.app/get?token=not-a-jwt") is None


def test_sanitize_filename() -> None:
    assert sanitize_filename("XDown.app_a.mp4") == "XDown.app_a.mp4"
    traversal = sanitize_filename("../../etc/passwd")
    assert "/" not in traversal and "\\" not in traversal, traversal
    assert not traversal.startswith("."), "不能生成隐藏文件/上级目录"
    assert sanitize_filename("../../etc/passwd") == "_.._etc_passwd"
    assert sanitize_filename("a b\\c:d?.mp4") == "a_b_c_d_.mp4"
    assert sanitize_filename("noext", ".mp4") == "noext.mp4"
    assert sanitize_filename("") is None and sanitize_filename(None) is None


def test_extract_handle() -> None:
    assert extract_handle("https://x.com/Fortnite/status/1904171341735178552") == "Fortnite"
    assert extract_handle("https://twitter.com/chitose_yoshino/status/1") == "chitose_yoshino"
    assert extract_handle("https://x.com/i/web/status/1904171341735178552") is None
    assert extract_handle("https://example.com/Foo/status/1") is None


def test_resolution_label() -> None:
    assert resolution_label(None, "下载 MP4 (720p)") == "720p"
    assert resolution_label("https://video.twimg.com/x/1280x720/y.mp4") == "720p"
    assert resolution_label("https://video.twimg.com/x/480x270/y.mp4") == "270p"
    assert resolution_label("https://video.twimg.com/x/720x1280/y.mp4") == "720p"
    assert resolution_label("https://pbs.twimg.com/media/x.jpg") is None


def _result(**kwargs) -> ParseResult:
    base = dict(
        url="https://x.com/Fortnite/status/1904171341735178552",
        title="Don’t miss the (Lucky) Landing.",
        author_handle="Fortnite",
        duration="0:07",
        contents=[Content(type="video", url="https://dl.snapcdn.app/get?token=x", label="720p")],
    )
    base.update(kwargs)
    return ParseResult(**base)


def test_build_caption_full() -> None:
    caption = build_caption(_result())
    assert caption.splitlines()[0] == "作者：@Fortnite"
    assert "视频 · 720p · 0:07" in caption
    assert "Lucky" in caption


def test_build_caption_options() -> None:
    result = _result(is_repost=True, author_handle=None, author_name="无用户名")
    assert "作者" not in build_caption(result, include_author=False)
    assert "转发" not in build_caption(result, include_author=False)

    repost = build_caption(_result(is_repost=True))
    assert "作者：@Fortnite · 转发" in repost
    assert "🔁" not in repost, "默认不带表情，避免群里看不懂"

    assert "🎬" in build_caption(_result(), emoji=True)

    with_link = build_caption(_result(), include_link=True)
    assert with_link.splitlines()[-1] == "https://x.com/Fortnite/status/1904171341735178552"

    # 没有作者信息时不硬塞「无用户名」
    assert not build_caption(result).startswith("作者")

    # 截断
    long_result = _result(title="字" * 400)
    assert len(build_caption(long_result, max_chars=50).splitlines()[-1]) == 51  # 50 + 省略号


def test_format_media_info_mixed() -> None:
    result = _result(
        contents=[
            Content(type="video", url="u", label="720p"),
            Content(type="image", url="i1"),
            Content(type="image", url="i2"),
        ]
    )
    assert format_media_info(result) == "视频 · 720p · 0:07 ＋ 图片 ×2"
    assert format_media_info(_result(contents=[Content(type="dynamic", url="u")], duration="0:02")) == "GIF · 0:02"
    assert format_media_info(_result(contents=[])) == ""


def test_parse_html_ignores_placeholder_buttons() -> None:
    """回归：转换为 MP3(href=#)、下载更多视频(href=/)、占位下载图片(href=#) 都不该进媒体列表。"""
    html = """
    <div class="thumbnail"><img src="https://pbs.twimg.com/profile_images/1/a.jpg"></div>
    <img src="https://pbs.twimg.com/media/REAL.jpg">
    <a href="https://dl.snapcdn.app/get?token=AAA" class="tw-button-dl button dl-success">下载 MP4 (720p)</a>
    <a href="#" id="convert_mp3_X" class="tw-button-dl button dl-success action-convert">转换为 MP3</a>
    <a href="#" class="tw-button-dl button dl-success action-convert">转换为 GIF</a>
    <a href="#" class="tw-button-dl button dl-success">下载图片</a>
    <a href="/" class="button is-dark is-fullwidth more-video">下载更多视频</a>
    <h3>标题</h3>
    <input type="hidden" id="TwitterId" value="123" />
    """
    result = parse_twitter_html(html)
    assert [item.url for item in result.contents] == ["https://dl.snapcdn.app/get?token=AAA"]
    assert result.counts["audio"] == 0 and result.counts["dynamic"] == 0
    assert result.cover == "https://pbs.twimg.com/media/REAL.jpg", "封面不能取到头像"


def test_cover_prefers_thumbnail_container_over_emoji() -> None:
    """回归：HTML 里先出现表情/头像时，封面不能被它们抢走。"""
    html = """
    <div class="content"><img src="https://abs.twimg.com/emoji/v2/72x72/1f4cc.png" alt="emoji"></div>
    <img src="https://pbs.twimg.com/profile_images/9/avatar.png">
    <div class="tw-left"><div class="thumbnail"><div class="image-tw">
        <img src="https://pbs.twimg.com/media/REAL_COVER.jpg">
    </div></div></div>
    <a href="https://dl.snapcdn.app/get?token=AAA" class="tw-button-dl button dl-success">下载 MP4 (1080p)</a>
    <h3>正文</h3>
    """
    result = parse_twitter_html(html)
    assert result.cover == "https://pbs.twimg.com/media/REAL_COVER.jpg"
    assert result.counts["video"] == 1
    assert result.contents[0].label == "1080p"


def test_emoji_button_skipped_as_image() -> None:
    """回归：下载按钮指向表情图片时不要当成推文图片发出去。"""
    html = """
    <img src="https://pbs.twimg.com/media/REAL.jpg">
    <a href="#" class="tw-button-dl dl-success action-convert">转换为 GIF</a>
    <a href="https://dl.snapcdn.app/get?token=EMOJI" class="abutton is-success">下载图片</a>
    <h3>正文</h3>
    """
    import base64 as _b64
    import json as _json

    def token(url: str) -> str:
        payload = _b64.urlsafe_b64encode(
            _json.dumps({"url": url, "filename": "x.png"}).encode()
        ).decode().rstrip("=")
        return f"https://dl.snapcdn.app/get?token=eyJhbGciOiJIUzI1NiJ9.{payload}.sig"

    emoji_html = html.replace(
        "https://dl.snapcdn.app/get?token=EMOJI",
        token("https://abs.twimg.com/emoji/v2/72x72/1f4cc.png"),
    )
    result = parse_twitter_html(emoji_html)
    assert result.counts["image"] == 0, "表情图片不该被当成推文图片"
    assert result.counts["video"] == 0
