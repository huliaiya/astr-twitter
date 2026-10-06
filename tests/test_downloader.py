# -*- coding: utf-8 -*-
"""下载器重试与超时策略的单元测试（离线，用假 session，不真的等网络/退避）。

背景：媒体下载此前**完全没有重试**，而且只依赖 aiohttp 的 total 超时。
在受限网络下 pbs.twimg.com / video.twimg.com 会被解析到错误 IP 并直接挂死，
一次瞬时卡顿就会丢掉整条媒体（用户侧的「媒体下载失败」）。
这里锁住新的行为：瞬时故障重试、确定性故障不重试、封面不重试、退避有上限。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.downloader import Downloader, DownloadException  # noqa: E402

JPEG = b"\xff\xd8\xff" + b"0" * 512
RELAY_URL = "https://dl.snapcdn.app/get?token=fake"


# --------------------------------------------------------------------------- #
# 假的 aiohttp 会话
# --------------------------------------------------------------------------- #
class _FakeContent:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def iter_chunked(self, size: int):
        for i in range(0, len(self._body), size):
            yield self._body[i : i + size]


class _FakeResponse:
    def __init__(self, body: bytes = JPEG, *, status: int = 200, reason: str = "OK"):
        self.status = status
        self.reason = reason
        self.url = RELAY_URL
        self.headers = {"Content-Type": "image/jpeg"}
        self.content = _FakeContent(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """按顺序返回预置结果；用完后重复最后一个。"""

    def __init__(self, results: list) -> None:
        self._results = list(results)
        self.calls = 0

    def get(self, url, **kwargs):
        self.calls += 1
        item = self._results[min(self.calls - 1, len(self._results) - 1)]
        if isinstance(item, BaseException):
            raise item
        return item


def _noop_sleep(record: list):
    async def sleep(delay: float) -> None:
        record.append(delay)

    return sleep


def _make(results: list, tmp_path: Path, *, retry: int = 2):
    session = _FakeSession(results)
    downloader = Downloader(session, base_dir=tmp_path / "media", retry=retry)
    delays: list[float] = []
    downloader._sleep = _noop_sleep(delays)
    return session, downloader, delays


# --------------------------------------------------------------------------- #
# 瞬时故障应当重试
# --------------------------------------------------------------------------- #
async def test_timeout_is_retried_then_succeeds(tmp_path):
    session, downloader, delays = _make(
        [asyncio.TimeoutError("stall"), asyncio.TimeoutError("stall"), _FakeResponse()],
        tmp_path,
        retry=2,
    )
    media = await downloader.download(RELAY_URL)

    assert session.calls == 3, "两次瞬时失败后应当重试第三次并成功"
    assert len(delays) == 2, "两次重试各退避一次"
    assert media.size == len(JPEG)
    assert media.kind == "jpeg"
    assert media.path.exists()


async def test_connection_error_is_retried(tmp_path):
    import aiohttp

    session, downloader, _ = _make(
        [aiohttp.ClientConnectionError("no route to host"), _FakeResponse()],
        tmp_path,
        retry=1,
    )
    media = await downloader.download(RELAY_URL)
    assert session.calls == 2
    assert media.size == len(JPEG)


async def test_broken_exception_str_does_not_mask_the_real_error(tmp_path):
    """某些异常的 __str__ 自己会抛错，不能让它掩盖真正的失败原因。"""

    class Nasty(Exception):
        def __str__(self):
            raise RuntimeError("boom inside __str__")

    session, downloader, _ = _make([Nasty()], tmp_path, retry=0)
    with pytest.raises(DownloadException, match="Nasty"):
        await downloader.download(RELAY_URL)
    assert session.calls == 1


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
async def test_transient_http_status_is_retried(tmp_path, status):
    session, downloader, _ = _make(
        [_FakeResponse(b"", status=status, reason="err"), _FakeResponse()],
        tmp_path,
        retry=1,
    )
    media = await downloader.download(RELAY_URL)
    assert session.calls == 2
    assert media.size == len(JPEG)


async def test_empty_body_is_retried(tmp_path):
    session, downloader, _ = _make([_FakeResponse(b"")], tmp_path, retry=1)
    with pytest.raises(DownloadException, match="下载内容为空"):
        await downloader.download(RELAY_URL)
    assert session.calls == 2, "空响应属于瞬时问题，应当重试"


# --------------------------------------------------------------------------- #
# 确定性故障不该重试（重试纯属浪费时间）
# --------------------------------------------------------------------------- #
async def test_oversize_is_not_retried(tmp_path):
    session, downloader, delays = _make([_FakeResponse()], tmp_path, retry=3)
    with pytest.raises(DownloadException, match="超过大小上限"):
        await downloader.download(RELAY_URL, max_bytes=100)
    assert session.calls == 1
    assert delays == []


async def test_http_404_is_not_retried(tmp_path):
    session, downloader, delays = _make(
        [_FakeResponse(b"", status=404, reason="Not Found")], tmp_path, retry=3
    )
    with pytest.raises(DownloadException, match="HTTP 404"):
        await downloader.download(RELAY_URL)
    assert session.calls == 1
    assert delays == []


async def test_retry_override_zero_disables_retry(tmp_path):
    """封面这类可选内容传 retry=0，失败要快，别拖着正片一起等。"""
    session, downloader, delays = _make([asyncio.TimeoutError("stall")], tmp_path, retry=5)
    with pytest.raises(DownloadException):
        await downloader.download(RELAY_URL, retry=0)
    assert session.calls == 1
    assert delays == []


async def test_download_many_passes_per_item_kwargs(tmp_path):
    """download_many 要把每项的 retry/timeout 透传给 download。"""
    session, downloader, _ = _make([_FakeResponse()], tmp_path, retry=3)
    results = await downloader.download_many([(RELAY_URL, {"retry": 0, "subdir": "t1"})])
    assert isinstance(results[0], Exception) is False
    assert results[0].path.parent.name == "t1"
    assert session.calls == 1


# --------------------------------------------------------------------------- #
# 退避与「被阻断主机」的判定
# --------------------------------------------------------------------------- #
def test_backoff_grows_and_is_capped():
    downloader = Downloader.__new__(Downloader)
    downloader.backoff = 0.8
    downloader.max_backoff = 8.0

    for attempt in range(12):
        raw = min(8.0, 0.8 * (2**attempt))
        delay = downloader._next_delay(attempt)
        assert 0.0 <= delay <= raw, f"第 {attempt} 次退避 {delay} 超出上限 {raw}"
    assert downloader._next_delay(99) <= 8.0


def test_backoff_zero_returns_zero():
    downloader = Downloader.__new__(Downloader)
    downloader.backoff = 0.0
    downloader.max_backoff = 8.0
    assert downloader._next_delay(0) == 0.0


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://pbs.twimg.com/media/x.jpg", True),
        ("https://video.twimg.com/tweet_video/x.mp4", True),
        ("https://dl.snapcdn.app/get?token=x", False),
        ("https://xdown.app/api/ajaxSearch", False),
    ],
)
def test_direct_media_host_detection(url, expected):
    """只有 X 的媒体主机才该给「请配置 proxy」的提示，避免误导。"""
    assert Downloader._is_direct_media_host(url) is expected


def test_blocked_hint_tells_user_to_set_proxy():
    from core import downloader as mod

    assert "proxy" in mod._BLOCKED_HINT
    assert "%s" in mod._BLOCKED_HINT


# --------------------------------------------------------------------------- #
# 缓存
# --------------------------------------------------------------------------- #
async def test_second_download_hits_cache(tmp_path):
    session, downloader, _ = _make([_FakeResponse()], tmp_path)
    first = await downloader.download(RELAY_URL)
    second = await downloader.download(RELAY_URL)
    assert session.calls == 1, "同一 URL 不该下两次"
    assert first.path == second.path

    downloader.forget_cache()
    await downloader.download(RELAY_URL)
    assert session.calls == 2, "清缓存后应当重新下载"
