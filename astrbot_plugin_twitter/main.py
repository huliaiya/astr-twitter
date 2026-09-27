# -*- coding: utf-8 -*-
"""astr-twitter: AstrBot X 解析插件主入口。

模块化结构：
- config.py: 配置管理
- sender.py: 发送逻辑（引用、分段回复、重试降级）
- policy.py: 自动解析策略（会话/全局开关、防抖、触发判断）
- webapi.py: WebUI 历史页面 API
- core/: 解析核心（不依赖 AstrBot）
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, AsyncGenerator

import aiohttp

from astrbot import logger
from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import (
    Plain, Video, Reply, Image, Record, File, At, Node, Nodes, Json,
    Face, Poke, Forward, Music, Contact, Location, RPS, Dice, Shake, Share, Unknown,
    ComponentType,
)
from astrbot.api.platform import AstrBotMessage, MessageType
from astrbot.core.star import Star
from astrbot.core.utils.metrics import Metric

from .config import Settings, KV_DISABLED_SESSIONS, KV_ENABLED_SESSIONS, KV_GLOBAL_AUTO_PARSE
from .sender import Sender
from .policy import AutoParsePolicy
from .webapi import register_web_apis
from .core.downloader import Downloader
from .core import parse_tweet, TRIGGER_MODES, extract_urls

# 消息组件别名，供测试用：Comp.Plain 等同于 Plain
Comp = type("Comp", (), {
    "Plain": Plain,
    "Video": Video,
    "Reply": Reply,
    "Image": Image,
    "Record": Record,
    "File": File,
    "At": At,
    "Node": Node,
    "Nodes": Nodes,
    "Json": Json,
    "Face": Face,
    "Poke": Poke,
    "Forward": Forward,
    "Music": Music,
    "Contact": Contact,
    "Location": Location,
    "RPS": RPS,
    "Dice": Dice,
    "Shake": Shake,
    "Share": Share,
    "Unknown": Unknown,
    "ComponentType": ComponentType,
})

# 插件标识（不变，决定安装目录/路由前缀）
PLUGIN_NAME = "astr-twitter"
PLUGIN_ID = "astrbot_plugin_twitter"

# 命令识别正则
_COMMAND_RE = re.compile(r"^\s*(解析|[Xx]解析|tw|开启解析|关闭解析|解析状态|解析历史)\b")


class TwitterPlugin(Star):
    """AstrBot X 解析插件主类。"""

    def __init__(self, context: Star.Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config = config
        self.settings = Settings.from_config(config)

        # 配置校验
        errors = self.settings.validate()
        if errors:
            for e in errors:
                logger.error(f"[{PLUGIN_NAME}] 配置错误: {e}")

        # 运行时依赖
        self._session: aiohttp.ClientSession | None = None
        self._downloader: Any = None
        self._sender: Sender | None = None
        self._policy: AutoParsePolicy | None = None

        # 内存态
        self._pending_files: list[Path] = []
        self._history_store: Any = None

        # 检测 AstrBot 分段回复设置
        self._segmented_reply = self._detect_segmented_reply()

        # 注册 Web API
        self._register_web_apis()

    # ------------------------------------------------------------------ #
    # 初始化/清理
    # ------------------------------------------------------------------ #
    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            timeout = aiohttp.ClientTimeout(total=self.settings.timeout)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    @property
    def downloader(self) -> Any:
        if self._downloader is None:
            self._downloader = Downloader(
                self.session,
                base_dir=self.data_dir / "media",
                proxy=self.settings.proxy or None,
                timeout=self.settings.timeout,
                max_bytes=self.settings.max_video_mb * 1024 * 1024,
                concurrency=self.settings.download_concurrency,
            )
        return self._downloader

    @property
    def data_dir(self) -> Path:
        return Path(self.context.get_config("data_dir", ".")) / "data" / PLUGIN_ID

    @property
    def sender(self) -> Sender:
        if self._sender is None:
            self._sender = Sender(
                settings=self.settings,
                downloader=self.downloader,
                session=self.session,
                data_dir=self.data_dir,
            )
            self._sender.set_segmented_reply(self._segmented_reply)
        return self._sender

    @property
    def policy(self) -> AutoParsePolicy:
        if self._policy is None:
            self._policy = AutoParsePolicy(self.settings, self.context)
        return self._policy

    # 测试兼容：暴露内部 policy 方法
    async def _ensure_policy(self) -> None:
        await self.policy.ensure_loaded()

    def _debounced(self, key: str) -> bool:
        return self.policy.debounced(key)

    def _effective_auto_parse(self, umo: str) -> tuple[bool, str]:
        return self.policy.effective_auto_parse(umo)

    def _history(self):
        from .core.history import HistoryStore
        return HistoryStore(self.data_dir / "history.json", max_records=self.settings.history_size)

    def _detect_segmented_reply(self) -> bool:
        """读取 AstrBot 全局配置，判断是否开启分段回复。"""
        try:
            cfg = self.context.get_config()
            seg = cfg.get("platform_settings", {}).get("segmented_reply", {})
            return bool(seg.get("enable")) and not bool(seg.get("only_llm_result", True))
        except Exception:  # noqa: BLE001
            return False

    def _register_web_apis(self) -> None:
        try:
            from astrbot.api.web import request  # noqa: F401
        except Exception:  # noqa: BLE001
            logger.debug(f"[{PLUGIN_NAME}] 当前 AstrBot 版本不支持插件 Web API，忽略历史页面接口")
            return
        try:
            from .webapi import register_web_apis
            self._api_history, self._api_history_clear = register_web_apis(self.context, PLUGIN_ID, self.data_dir, self.settings.history_size)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 注册 Web API 失败: {e}")

    async def api_history(self):
        """Web API: GET /history"""
        if self._api_history:
            return await self._api_history()
        return {"status": "error", "data": None}

    async def api_history_clear(self):
        """Web API: POST /history/clear"""
        if self._api_history_clear:
            return await self._api_history_clear()
        return {"status": "error", "data": None}

    async def terminate(self) -> None:
        """插件卸载/重载时清理资源。"""
        if self._session:
            await self._session.close()
            self._session = None
        # 清理残留临时文件
        for path in self._pending_files:
            Downloader.cleanup(path)
        self._pending_files.clear()

    # ------------------------------------------------------------------ #
    # 消息预处理工具（复用 policy 中的静态方法）
    # ------------------------------------------------------------------ #
    @staticmethod
    def _collect_text(event: AstrMessageEvent) -> str:
        return AutoParsePolicy.collect_text(event)

    @staticmethod
    def _looks_like_command(text: str) -> bool:
        return AutoParsePolicy.looks_like_command(text)

    def _is_at_bot(self, event: AstrMessageEvent) -> bool:
        return AutoParsePolicy.is_at_bot(event)

    # ------------------------------------------------------------------ #
    # 自动解析
    # ------------------------------------------------------------------ #
    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def on_message(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """监听所有消息，发现 X 链接就解析并发送媒体。"""
        if not self.settings.enabled or not self.settings.auto_parse:
            return

        started = time.monotonic()
        text = self._collect_text(event)
        if not text or self._looks_like_command(text):
            return

        # 忽略机器人自己发出的消息，避免自触发
        try:
            if event.get_sender_id() and event.get_sender_id() == event.get_self_id():
                return
        except Exception:  # noqa: BLE001
            pass

        # 触发方式：all=消息里有链接就解析；at=需要 @机器人；command_only=只用指令/LLM 工具
        if self.settings.trigger_mode == "command_only":
            return
        if self.settings.trigger_mode == "at" and not self._is_at_bot(event):
            logger.debug(f"[{PLUGIN_NAME}] trigger_mode=at 且未 @机器人，跳过")
            return

        # 会话/全局开关：默认策略为「需先发 /开启解析」
        await self.policy.ensure_loaded()
        allowed, source = self.policy.effective_auto_parse(event.unified_msg_origin)
        if not allowed:
            # 有人确实发了链接，只是本会话没开——这种情况值得留一条 INFO，方便排查
            logger.info(
                f"[{PLUGIN_NAME}] 发现 X 链接但本会话未开启自动解析（{source}）；"
                f"发 /开启解析 即可开启，或直接用 /解析 <链接>。会话：{event.unified_msg_origin}"
            )
            if self.settings.hint_when_disabled:
                await self.sender._reply(event, "本会话未开启 X 自动解析，发 /开启解析 开启，或直接用 /解析 <链接>")
            return

        from .core import extract_urls
        urls = extract_urls(text)
        if not urls:
            return

        handled = False
        for url in urls[: self.settings.max_links]:
            tweet_id = url.rstrip("/").rsplit("/", 1)[-1]
            if self.policy.debounced(tweet_id):
                logger.debug(f"[{PLUGIN_NAME}] 防抖命中，跳过 {tweet_id}")
                continue
            segments = await self.sender.handle_url(
                event, url, notify_error=self.settings.notify_error, started=started
            )
            if not segments:
                continue
            handled = True
            async for item in self.sender.send_chain(
                event, segments, url, segmented_reply=self._segmented_reply_enabled
            ):
                yield item

        self.sender.flush_cleanup()

        if handled and self.settings.interrupt_event:
            event.stop_event()

    # ------------------------------------------------------------------ #
    # 指令
    # ------------------------------------------------------------------ #
    @filter.command("解析", alias={"X解析", "x解析", "tw", "推特解析"})
    async def cmd_parse(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """解析 X 链接并把媒体发到当前会话：/解析 <链接>"""
        from .core import extract_urls
        started = time.monotonic()
        urls = extract_urls(self._collect_text(event))
        if not urls:
            yield event.plain_result("用法：/解析 <X 链接>")
            return

        sent = 0
        for url in urls[: self.settings.max_links]:
            segments = await self.sender.handle_url(event, url, notify_error=True, started=started)
            if segments:
                sent += 1
                async for item in self.sender.send_chain(
                    event, segments, url, segmented_reply=self._segmented_reply_enabled
                ):
                    yield item
        self.sender.flush_cleanup()
        if sent == 0:
            logger.info(f"[{PLUGIN_NAME}] /解析 未成功解析任何链接")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("解析历史")
    async def cmd_history(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """查看最近的解析记录（管理员）：/解析历史 [条数]"""
        import re
        match = re.search(r"(\d+)", event.message_str or "")
        limit = max(1, min(20, int(match.group(1)) if match else 5))
        if not self.settings.history_enabled:
            yield event.plain_result("历史记录功能未启用")
            return
        from .core.history import HistoryStore
        store = HistoryStore(self.data_dir / "history.json", max_records=self.settings.history_size)
        records = await store.list(limit)
        if not records:
            yield event.plain_result("暂无历史记录")
            return
        lines = ["最近解析记录："]
        for r in records:
            status = "✅" if r.get("ok") else "❌"
            media = sum((r.get("counts") or {}).values())
            lines.append(f"{status} {r.get('time', '')} {r.get('url', '')} ({media} 个媒体, {r.get('bytes', 0)} bytes)")
            if not r.get("ok") and r.get("error"):
                lines.append(f"   错误: {r['error']}")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("开启解析")
    async def cmd_enable(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """开启自动解析：/开启解析 [全局]"""
        is_global = "全局" in (event.message_str or "")
        umo = "global" if is_global else event.unified_msg_origin
        msg = await self.policy.enable(umo, is_global)
        yield event.plain_result(msg)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("关闭解析")
    async def cmd_disable(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """关闭自动解析：/关闭解析 [全局]"""
        is_global = "全局" in (event.message_str or "")
        umo = "global" if is_global else event.unified_msg_origin
        msg = await self.policy.disable(umo, is_global)
        yield event.plain_result(msg)

    @filter.command("解析状态")
    async def cmd_status(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """查看当前自动解析状态：/解析状态"""
        umo = event.unified_msg_origin
        text = await self.policy.status(umo)
        yield event.plain_result(text)

    # ------------------------------------------------------------------ #
    # LLM Tool
    # ------------------------------------------------------------------ #
    @filter.llm_tool(name="parse_twitter_link")
    async def llm_parse_twitter_link(
        self, event: AstrMessageEvent, url: str
    ) -> AsyncGenerator[Any, None]:
        """解析 X/Twitter 链接，返回该推文的媒体信息（类型与数量），并尝试把媒体发送到当前会话。"""
        from .core import extract_urls
        started = time.monotonic()
        urls = extract_urls(url)
        if not urls:
            yield event.plain_result("无效的 X 链接")
            return
        url = urls[0]
        segments = await self.sender.handle_url(event, url, notify_error=False, started=started)
        if segments:
            async for item in self.sender.send_chain(
                event, segments, url, segmented_reply=self._segmented_reply_enabled
            ):
                yield item
            yield event.plain_result(f"解析完成：{url}")
        else:
            yield event.plain_result(f"解析失败：{url}")


# 导出供 __init__.py 使用
from .config import Settings
from .core import parse_tweet, extract_urls
from .core.history import HistoryRecord
from .sender import Sender

# 兼容旧测试：暴露内部方法
async def _handle_url(self, event: AstrMessageEvent, url: str, **kwargs):
    return await self.sender.handle_url(event, url, **kwargs)

async def _send_chain(self, event: AstrMessageEvent, segments: list[Any], url: str, **kwargs):
    async for item in self.sender.send_chain(event, segments, url, **kwargs):
        yield item

@property
def _segmented_reply_enabled(self) -> bool:
    return self._segmented_reply

__all__ = [
    "TwitterPlugin",
    "Settings",
    "PLUGIN_NAME",
    "PLUGIN_ID",
    "extract_urls",
    "parse_tweet",
    "HistoryRecord",
]