# -*- coding: utf-8 -*-
"""媒体下载与文件头校验（对应原插件的 core/download.py 精简版）。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

import aiohttp

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


def sniff_kind(head: bytes) -> str:
    """按文件头判断真实类型（原插件靠 yt-dlp/gallery-dl 的返回，这里自己校验）。"""
    for kind, magic in _MAGIC:
        if head[: len(magic)] == magic:
            return kind
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "mp4"
    return "unknown"


class DownloadException(Exception):
    """下载失败。"""


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

    async def download(
        self,
        url: str,
        *,
        subdir: str | None = None,
        filename: str | None = None,
    ) -> Media:
        """下载一个媒体 URL 到 base_dir/<subdir>/。

        Args:
            url: 媒体直链（xdown 的 dl.snapcdn.app 中转链或 pbs/video.twimg.com）
            subdir: 子目录，一般是推文 id
            filename: 指定文件名（含扩展名），默认按内容自动生成

        Raises:
            DownloadException: 网络异常、HTTP 错误、超过大小上限
        """
        target_dir = self.base_dir / subdir if subdir else self.base_dir
        target_dir.mkdir(parents=True, exist_ok=True)

        headers = {"User-Agent": self.user_agent}
        try:
            async with self._session.get(
                url,
                headers=headers,
                proxy=self.proxy,
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as resp:
                if resp.status >= 400:
                    raise DownloadException(f"HTTP {resp.status} {resp.reason}")
                data = await resp.read()
                final_url = str(resp.url)
                content_type = resp.headers.get("Content-Type")
        except DownloadException:
            raise
        except Exception as e:  # noqa: BLE001
            raise DownloadException(f"{type(e).__name__}: {e}") from e

        if len(data) > self.max_bytes:
            raise DownloadException(f"文件超过大小上限 {self.max_bytes} 字节")
        if not data:
            raise DownloadException("下载内容为空")

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

        return Media(
            path=path,
            size=len(data),
            kind=kind,
            content_type=content_type,
            final_url=final_url,
        )

    @staticmethod
    def cleanup(path: Path) -> None:
        """删除单个文件（发送完成后清理）。"""
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass
