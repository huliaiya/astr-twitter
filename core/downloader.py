# -*- coding: utf-8 -*-
"""媒体下载与文件头校验（对应原插件的 core/download.py 精简版）。"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiohttp

try:  # 在插件里复用 AstrBot 的插件日志；单独跑 core 时退回标准库
    from astrbot import logger
except Exception:  # noqa: BLE001
    import logging

    logger = logging.getLogger("astr-twitter")

# 可重试的 HTTP 状态码：限流与网关类错误
_TRANSIENT_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

# X 的媒体服务器：在部分网络下会被解析到错误的 IP 并直接挂死，
# 所以连接超时要短，失败要快，才好重试或改走代理。
_DIRECT_MEDIA_HOSTS = ("pbs.twimg.com", "video.twimg.com")

_BLOCKED_HINT = (
    "[astr-twitter] 直连 X 媒体服务器失败。若当前网络无法直连 %s"
    "（连接被阻断或 DNS 被污染），请在插件配置的 proxy 中填写可用的"
    " HTTP/SOCKS5 代理后重试。"
)

# 文件头魔数 → 类型
_MAGIC: tuple[tuple[str, bytes], ...] = (
    ("jpeg", b"\xff\xd8\xff"),
    ("png", b"\x89PNG"),
    ("gif", b"GIF8"),
    ("webp", b"RIFF"),
    ("pdf", b"%PDF"),
)

_EXT_BY_KIND = {
    "jpeg": ".jpg",
    "png": ".png",
    "gif": ".gif",
    "webp": ".webp",
    "mp4": ".mp4",
    "pdf": ".pdf",
}


def _describe(exc: BaseException) -> str:
    """安全地把异常转成字符串。

    个别 aiohttp 异常（如缺 connection key 的 ClientConnectorError）的 __str__
    自身会抛错，直接用 f"{e}" 会用一个无关的 AttributeError 掩盖真正的故障。
    """
    try:
        return f"{type(exc).__name__}: {exc}"
    except Exception:  # noqa: BLE001
        return type(exc).__name__


def sniff_kind(head: bytes) -> str:
    """按文件头判断真实类型（原插件靠 yt-dlp/gallery-dl 的返回，这里自己校验）。"""
    for kind, magic in _MAGIC:
        if head[: len(magic)] == magic:
            return kind
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "mp4"
    return "unknown"


class DownloadException(Exception):
    """下载失败。

    Attributes:
        retryable: 是否为「重试可能成功」的瞬时故障（超时、连接重置、限流、5xx）。
    """

    def __init__(self, message: str = "", *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass
class Media:
    """一个已落盘的媒体文件。"""

    path: Path
    size: int
    kind: str
    content_type: str | None = None
    final_url: str = ""

    @property
    def is_video(self) -> bool:
        return self.kind == "mp4"


class Downloader:
    """复用同一个 aiohttp 会话下载媒体。"""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        base_dir: Path,
        proxy: str | None = None,
        timeout: float = 120.0,
        max_bytes: int = 100 * 1024 * 1024,
        user_agent: str | None = None,
        concurrency: int = 3,
        retry: int = 2,
        backoff: float = 0.8,
        max_backoff: float = 8.0,
        sock_connect: float = 10.0,
        sock_read: float = 30.0,
    ) -> None:
        self._session = session
        self.base_dir = Path(base_dir)
        self.proxy = proxy
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.user_agent = user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )
        # 多图推文并行下载，但限制并发，避免把对方站点打挂
        self._semaphore = asyncio.Semaphore(max(1, concurrency))
        self._cache: dict[str, Media] = {}

        # 媒体下载此前完全不重试：一次瞬时卡顿就会丢掉整条媒体。
        # 这里让解析 API 的 retry 设置同样作用于媒体下载。
        self.retry = max(0, retry)
        self.backoff = max(0.0, backoff)
        self.max_backoff = max(0.0, max_backoff)
        # 连接与读取分别限时：total 仍然宽松（大视频不会被误杀），
        # 但被阻断/卡死的主机可以尽快失败并重试，而不是干等到底。
        self.sock_connect = sock_connect
        self.sock_read = sock_read
        # 便于测试注入，避免测试真的等待
        self._sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

    def _next_delay(self, attempt: int) -> float:
        """指数退避（带上限与少量抖动，避免多个媒体同时重试）。"""
        import random

        delay = min(self.max_backoff, self.backoff * (2**attempt))
        return delay * (0.6 + 0.4 * random.random()) if delay else 0.0

    @staticmethod
    def _is_direct_media_host(url: str) -> bool:
        """URL 是否指向 X 的媒体服务器（这类主机在受限网络下常被阻断）。"""
        host = urlsplit(url).hostname or ""
        return any(host.endswith(h) for h in _DIRECT_MEDIA_HOSTS)

    @classmethod
    def _warn_if_network_blocked(cls, url: str) -> None:
        """直连 X 媒体服务器失败时，给出可操作的排查提示。"""
        if cls._is_direct_media_host(url):
            logger.warning(_BLOCKED_HINT, urlsplit(url).hostname)

    async def _fetch(
        self, url: str, limit: int, timeout: float | None
    ) -> tuple[bytes, str, str | None]:
        """单次请求并读完整个响应体，返回 (数据, 最终 URL, Content-Type)。"""
        headers = {"User-Agent": self.user_agent}
        client_timeout = aiohttp.ClientTimeout(
            total=timeout or self.timeout,
            sock_connect=self.sock_connect,
            sock_read=self.sock_read,
        )
        try:
            async with self._semaphore:
                async with self._session.get(
                    url,
                    headers=headers,
                    proxy=self.proxy,
                    allow_redirects=True,
                    timeout=client_timeout,
                ) as resp:
                    if resp.status >= 400:
                        raise DownloadException(
                            f"HTTP {resp.status} {resp.reason}",
                            retryable=resp.status in _TRANSIENT_STATUS,
                        )
                    final_url = str(resp.url)
                    content_type = resp.headers.get("Content-Type")

                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in resp.content.iter_chunked(1 << 16):
                        size += len(chunk)
                        if size > limit:
                            raise DownloadException(
                                f"文件超过大小上限 {limit} 字节，已中断下载"
                            )
                        chunks.append(chunk)
                    data = b"".join(chunks)
        except DownloadException:
            raise
        except (
            TimeoutError,
            aiohttp.ClientConnectionError,
            aiohttp.ServerDisconnectedError,
            ConnectionResetError,
        ) as e:
            # 超时/连不上/被重置属于瞬时故障，值得重试
            raise DownloadException(_describe(e), retryable=True) from e
        except Exception as e:  # noqa: BLE001
            raise DownloadException(_describe(e)) from e

        if not data:
            raise DownloadException("下载内容为空", retryable=True)
        return data, final_url, content_type

    async def download(
        self,
        url: str,
        *,
        subdir: str | None = None,
        filename: str | None = None,
        max_bytes: int | None = None,
        use_cache: bool = True,
        timeout: float | None = None,
        retry: int | None = None,
    ) -> Media:
        """下载一个媒体 URL 到 base_dir/<subdir>/。

        采用流式写入 + 边下边校验大小，超过上限立即中断，不会先把整个文件读进内存。

        Args:
            url: 媒体直链（xdown 的 dl.snapcdn.app 中转链或 pbs/video.twimg.com）
            subdir: 子目录，一般是推文 id
            filename: 指定文件名（含扩展名），默认按内容自动生成
            max_bytes: 本次下载的大小上限，默认用实例的 max_bytes
            use_cache: 同一 URL 在本进程内只下一次（多条消息重复发同一个链接时省流量）
            timeout: 本次下载的超时（秒），默认用实例的 timeout；封面这类可选内容给个短值
            retry: 本次下载的重试次数，默认用实例的 retry；封面这类可选内容可传 0

        Raises:
            DownloadException: 网络异常、HTTP 错误、超过大小上限
        """
        limit = self.max_bytes if max_bytes is None else max_bytes
        if use_cache and url in self._cache:
            cached = self._cache[url]
            if cached.path.exists():
                return cached

        target_dir = self.base_dir / subdir if subdir else self.base_dir
        target_dir.mkdir(parents=True, exist_ok=True)

        attempts = max(1, (self.retry if retry is None else max(0, retry)) + 1)
        data: bytes | None = None
        final_url = ""
        content_type: str | None = None
        for attempt in range(attempts):
            try:
                data, final_url, content_type = await self._fetch(url, limit, timeout)
            except DownloadException as e:
                self._warn_if_network_blocked(url)
                if not e.retryable or attempt >= attempts - 1:
                    raise
                delay = self._next_delay(attempt)
                logger.debug(
                    "[astr-twitter] 媒体下载失败，%.1fs 后重试（第 %d/%d 次）：%s",
                    delay,
                    attempt + 1,
                    self.retry,
                    _describe(e),
                )
                await self._sleep(delay)
            else:
                break
        if data is None:  # pragma: no cover - 上面的循环必然 break 或 raise
            raise DownloadException("下载失败")

        kind = sniff_kind(data[:16])
        if filename is None:
            ext = _EXT_BY_KIND.get(kind)
            if ext is None:
                # 从最终 URL 猜扩展名
                suffix = Path(final_url.split("?")[0]).suffix
                ext = suffix if suffix and len(suffix) <= 5 else ".bin"
            filename = f"{uuid.uuid4().hex[:12]}{ext}"

        path = target_dir / filename
        path.write_bytes(data)

        media = Media(
            path=path,
            size=len(data),
            kind=kind,
            content_type=content_type,
            final_url=final_url,
        )
        if use_cache:
            self._cache[url] = media
        return media

    async def download_many(
        self,
        items: Iterable[tuple[str, dict[str, Any]]],
        *,
        max_bytes: int | None = None,
    ) -> list[Media | Exception]:
        """并行下载多条媒体，返回与入参等长的列表（失败项是 Exception 对象）。

        items 里每项是 (url, 关键字参数 dict)，例如：
            [("https://...1.jpg", {"subdir": "123"}), ...]
        """
        prepared = list(items)

        async def run(index: int, url: str, kwargs: dict[str, Any]):
            try:
                return await self.download(url, max_bytes=max_bytes, **kwargs)
            except DownloadException as e:
                return e
            except Exception as e:  # noqa: BLE001 - 单个失败不影响其他
                # 统一包成 DownloadException，保证调用方拿到的错误一定是可安全打印的
                return DownloadException(_describe(e))

        results = await asyncio.gather(
            *(run(i, url, kwargs) for i, (url, kwargs) in enumerate(prepared))
        )
        return list(results)

    def forget_cache(self) -> None:
        """清空 URL→文件 缓存（发送完删除文件后调用，避免指向已删除的文件）。"""
        self._cache.clear()

    @staticmethod
    def cleanup(path: Path) -> None:
        """删除单个文件（发送完成后清理）。"""
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass
