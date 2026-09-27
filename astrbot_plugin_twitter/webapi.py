# -*- coding: utf-8 -*-
"""插件 Web API：历史页面、清空历史。"""

from __future__ import annotations

from typing import Any

from astrbot import logger


def register_web_apis(context: Any, plugin_id: str, history_dir: Any, history_size: int):
    """注册插件 Web API（老版本 AstrBot 没有这个能力就跳过）。返回 (history_handler, clear_handler) 供测试用。"""
    try:
        from astrbot.api.web import request  # noqa: F401 - 仅探测能力
    except Exception:  # noqa: BLE001
        logger.debug(f"[astr-twitter] 当前 AstrBot 版本不支持插件 Web API，忽略历史页面接口")
        return None, None

    history_handler = _make_history_handler(history_dir, history_size)
    clear_handler = _make_clear_handler(history_dir)
    
    try:
        context.register_web_api(
            f"/{plugin_id}/history",
            history_handler,
            ["GET"],
            "X 解析历史",
        )
        context.register_web_api(
            f"/{plugin_id}/history/clear",
            clear_handler,
            ["POST"],
            "清空 X 解析历史",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[astr-twitter] 注册 Web API 失败: {e}")
    
    return history_handler, clear_handler


def _make_history_handler(history_dir: Any, history_size: int):
    """生成 GET /history 处理器。"""
    from .core.history import HistoryStore

    async def handler() -> dict[str, Any]:
        from astrbot.api.web import request

        # 查询参数
        limit = 100
        try:
            q = request.query
            if q and "limit" in q:
                limit = max(1, min(int(q["limit"]), 500))
        except Exception:  # noqa: BLE001
            pass

        store = HistoryStore(history_dir / "history.json", history_size)
        records = await store.get_recent(limit)

        # 统计
        total = len(await store.get_all())
        ok = sum(1 for r in (await store.get_all()) if r.ok)
        failed = total - ok
        success_rate = round(ok / total * 100, 1) if total else 0
        total_bytes = sum(r.bytes for r in (await store.get_all()))

        from collections import Counter
        counts = Counter()
        author_counter = Counter()
        for r in (await store.get_all()):
            for k, v in (r.counts or {}).items():
                counts[k] += v
            if r.author:
                author_counter[r.author] += 1

        top_authors = [{"name": k, "count": v} for k, v in author_counter.most_common(5)]

        return {
            "status": "ok",
            "data": {
                "records": [
                    {
                        "time": r.time,
                        "url": r.url,
                        "ok": r.ok,
                        "counts": r.counts or {},
                        "media": sum((r.counts or {}).values()),
                        "bytes": r.bytes,
                        "title": r.title,
                        "source": r.source or "xdown",
                        "error": r.error,
                    }
                    for r in records
                ],
                "stats": {
                    "total": total,
                    "ok": ok,
                    "failed": failed,
                    "success_rate": success_rate,
                    "bytes": total_bytes,
                    "counts": dict(counts),
                    "top_authors": top_authors,
                },
                "settings": {
                    "api_endpoint": "https://xdown.app/api/ajaxSearch",
                    "proxy": "",
                    "trigger_mode": "all",
                    "history_size": history_size,
                },
            },
        }

    return handler


def _make_clear_handler(history_dir: Any):
    """生成 POST /history/clear 处理器。"""
    from .core.history import HistoryStore

    async def handler() -> dict[str, Any]:
        store = HistoryStore(history_dir / "history.json", 200)
        await store.clear()
        return {"status": "ok", "data": {"cleared": True}}

    return handler