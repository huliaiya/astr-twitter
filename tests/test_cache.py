# -*- coding: utf-8 -*-
"""解析缓存与并发去重的离线单元测试（不访问真实网络）。

覆盖：
- TTL 命中（相同 URL 只触发一次真实请求）
- 并发同一 URL 只放行一个 owner，其余等待复用
- TTL 过期后重新请求
- 缓存容量上限与淘汰
- clear_parse_cache 清空
"""

from __future__ import annotations

import asyncio
import time

import pytest

from core.twitter import (
    ParseException,
    ParseResult,
    TwitterConfig,
    clear_parse_cache,
    parse_tweet,
)


# --------------------------------------------------------------------------- #
# 假的「真实请求」：记录调用次数
# --------------------------------------------------------------------------- #
class _CallCounter:
    def __init__(self, calls: int = 0, delay: float = 0.0):
        self.calls = calls
        self.delay = delay


@pytest.fixture(autouse=True)
def _clean_cache():
    clear_parse_cache()
    yield
    clear_parse_cache()


@pytest.fixture
def counter(monkeypatch):
    """把真实请求的函数替换成一个假的：记录调用次数并立即返回假 ParseResult。"""
    state = {"calls": 0}
    import core.twitter as tw

    async def fake_via_xdown(url, input_text, config, session):
        state["calls"] += 1
        return ParseResult(url=url, author_name="fake")

    monkeypatch.setattr(tw, "_parse_via_xdown", fake_via_xdown)
    return state


# --------------------------------------------------------------------------- #
# 命中
# --------------------------------------------------------------------------- #
async def test_second_call_hits_cache(counter):
    url = "https://x.com/a/status/1"
    cfg = TwitterConfig(parse_cache_ttl=300.0)
    r1 = await parse_tweet(url, config=cfg)
    r2 = await parse_tweet(url, config=cfg)
    assert counter["calls"] == 1, "第二次应当命中缓存"
    assert r1 is r2, "命中应返回同一个对象"
    assert r1.url == url


async def test_cache_disabled_with_zero_ttl(counter):
    url = "https://x.com/a/status/2"
    cfg = TwitterConfig(parse_cache_ttl=0.0)
    await parse_tweet(url, config=cfg)
    await parse_tweet(url, config=cfg)
    assert counter["calls"] == 2, "TTL=0 应关闭缓存"
    import core.twitter as tw
    assert url not in tw._parse_cache, "关闭缓存时不应写入 _parse_cache"


async def test_expired_ttl_refetches(counter):
    url = "https://x.com/a/status/3"
    cfg = TwitterConfig(parse_cache_ttl=0.05)
    await parse_tweet(url, config=cfg)
    await asyncio.sleep(0.06)
    await parse_tweet(url, config=cfg)
    assert counter["calls"] == 2, "超过 TTL 应重新请求"


# --------------------------------------------------------------------------- #
# 并发去重
# --------------------------------------------------------------------------- #
async def test_concurrent_calls_dedup(counter):
    """同一 URL 并发多次，只放行一个真实请求；其余等待复用。"""
    url = "https://x.com/a/status/4"
    cfg = TwitterConfig(parse_cache_ttl=300.0)
    results = await asyncio.gather(*[parse_tweet(url, config=cfg) for _ in range(5)])
    assert counter["calls"] == 1, "5 个并发调用应当只产生一次真实请求"
    for r in results:
        assert r is results[0], "所有等待者应拿到同一个结果对象"


async def test_concurrent_failure_releases_marker(monkeypatch):
    """owner 失败后在途标记被清掉，下一个调用能再次走完整流程。"""
    import core.twitter as tw

    url = "https://x.com/a/status/5"
    cfg = TwitterConfig(parse_cache_ttl=300.0)

    state = {"fail": True}

    async def fake_single(url, input_text, config, session, fallback):
        if state["fail"]:
            state["fail"] = False
            raise ParseException("模拟失败")
        return ParseResult(url=url, author_name="ok")

    monkeypatch.setattr(tw, "_parse_single", fake_single)
    with pytest.raises(ParseException):
        await parse_tweet(url, config=cfg)
    assert url not in tw._inflight_cache, "失败后必须清理在途标记"
    r = await parse_tweet(url, config=cfg)
    assert r.author_name == "ok", "失败后下一次调用应走完整流程并成功"


# --------------------------------------------------------------------------- #
# 容量与清空
# --------------------------------------------------------------------------- #
async def test_cache_evicts_oldest_when_full(monkeypatch):
    """缓存超过 200 条时，写入应淘汰最旧的 50 条，避免长驻进程内存膨胀。"""
    import core.twitter as tw

    cfg = TwitterConfig(parse_cache_ttl=300.0)
    # 预先填到 199 条
    for i in range(199):
        u = f"https://x.com/evict/status/{i}"
        tw._parse_cache[u] = (time.monotonic(), ParseResult(url=u))
    assert len(tw._parse_cache) == 199

    # 直接调 owner 路径（fake 掉 _parse_single，不碰网络）
    async def fake_single(url, input_text, config, session, fallback):
        return ParseResult(url=url)

    monkeypatch.setattr(tw, "_parse_single", fake_single)
    # 用纯数字 tweet ID，通过 search_url 校验
    await parse_tweet("https://x.com/evict/status/200", config=cfg)
    assert len(tw._parse_cache) == 200, f"写入 200 条时不应淘汰，实际 {len(tw._parse_cache)}"
    await parse_tweet("https://x.com/evict/status/201", config=cfg)
    assert len(tw._parse_cache) <= 200, f"超过 200 条应被控制在 200 以内，实际 {len(tw._parse_cache)}"
    # 最新的两条必须在
    assert "https://x.com/evict/status/200" in tw._parse_cache
    assert "https://x.com/evict/status/201" in tw._parse_cache
    # 最早的一条（i=0）应当已被淘汰
    assert "https://x.com/evict/status/0" not in tw._parse_cache


def test_clear_returns_count():
    import core.twitter as tw

    tw._parse_cache["https://x.com/z/1"] = (time.monotonic(), ParseResult(url="x"))
    tw._parse_cache["https://x.com/z/2"] = (time.monotonic(), ParseResult(url="x"))
    assert clear_parse_cache() == 2
    assert len(tw._parse_cache) == 0
