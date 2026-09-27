# -*- coding: utf-8 -*-
"""解析历史记录（JSON 文件，容量有上限）。

记录结构（每条）：
    {
      "ts": 1735689600.0,          # 时间戳
      "time": "2025-01-01 12:00:00",
      "url": "...", "tweet_id": "...",
      "ok": true, "source": "xdown",
      "counts": {"video":0,"image":2,"dynamic":0,"audio":0},
      "media": 2, "bytes": 122214,
      "title": "...", "error": null,
      "author": "无用户名", "is_repost": false,
      "session": "fake:GroupMessage:123",   # 会话（用于统计，可按需脱敏）
      "sender": "10001"
    }

写入是「读-改-写」并且带锁，避免并发消息互相覆盖；文件损坏时自动忽略。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_RECORDS = 300


@dataclass
class HistoryRecord:
    url: str = ""
    tweet_id: str | None = None
    ok: bool = True
    source: str = "xdown"
    counts: dict[str, int] = field(default_factory=dict)
    media: int = 0
    bytes: int = 0
    title: str | None = None
    error: str | None = None
    author: str = "无用户名"
    is_repost: bool = False
    session: str = ""
    sender: str = ""
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        data = {
            "ts": self.ts,
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.ts)),
            "url": self.url,
            "tweet_id": self.tweet_id,
            "ok": self.ok,
            "source": self.source,
            "counts": self.counts,
            "media": self.media,
            "bytes": self.bytes,
            "title": self.title,
            "error": self.error,
            "author": self.author,
            "is_repost": self.is_repost,
            "session": self.session,
            "sender": self.sender,
        }
        return data


class HistoryStore:
    """有上限的解析历史（落盘 JSON，进程内共享）。"""

    def __init__(self, path: Path, *, max_records: int = MAX_RECORDS) -> None:
        self.path = path
        self.max_records = max(1, max_records)
        self._lock = asyncio.Lock()

    # ---------------- 读写 ---------------- #

    def _read(self) -> list[dict[str, Any]]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if isinstance(data, dict):  # 兼容 {"records": [...]}
            data = data.get("records", [])
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

    def _write(self, records: list[dict[str, Any]]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(records, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            tmp.replace(self.path)
        except OSError:
            pass

    # ---------------- 对外接口 ---------------- #

    async def add(self, record: HistoryRecord) -> None:
        async with self._lock:
            records = self._read()
            records.append(record.to_dict())
            if len(records) > self.max_records:
                records = records[-self.max_records :]
            self._write(records)

    async def list(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        async with self._lock:
            records = self._read()
        records.reverse()  # 最新在前
        limit = max(1, min(int(limit or 50), self.max_records))
        offset = max(0, int(offset or 0))
        return records[offset : offset + limit]

    async def clear(self) -> int:
        async with self._lock:
            count = len(self._read())
            self._write([])
        return count

    async def stats(self) -> dict[str, Any]:
        async with self._lock:
            records = self._read()
        total = len(records)
        ok = sum(1 for r in records if r.get("ok"))
        counts = {"video": 0, "image": 0, "dynamic": 0, "audio": 0}
        authors: dict[str, int] = {}
        bytes_total = 0
        for record in records:
            for key, value in (record.get("counts") or {}).items():
                counts[key] = counts.get(key, 0) + int(value or 0)
            author = str(record.get("author") or "").strip()
            if author and author != "无用户名":
                authors[author] = authors.get(author, 0) + 1
            bytes_total += int(record.get("bytes") or 0)
        top_authors = sorted(authors.items(), key=lambda kv: kv[1], reverse=True)[:10]
        return {
            "total": total,
            "ok": ok,
            "failed": total - ok,
            "success_rate": round(ok / total * 100, 1) if total else 0.0,
            "counts": counts,
            "bytes": bytes_total,
            "top_authors": [{"name": n, "count": c} for n, c in top_authors],
            "last_ts": records[-1].get("ts") if records else None,
        }
