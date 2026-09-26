# -*- coding: utf-8 -*-
"""astr-twitter：AstrBot 的 X/Twitter 链接解析插件。

对应文件：
  - metadata.yaml            插件元数据
  - _conf_schema.json        WebUI 配置面板
  - requirements.txt         依赖
  - core/twitter.py          解析核心（不依赖 AstrBot，可独立测试）
  - core/downloader.py       媒体下载
  - devtools/                开发/回归工具（Node CLI 与离线 fixture）
"""

from __future__ import annotations

import json
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

try:  # astrbot.api.message_components 是官方推荐的导入方式
    import astrbot.api.message_components as Comp
except Exception:  # pragma: no cover - 兼容极端情况
    Comp = None  # type: ignore[assignment]

from .core.downloader import DownloadException, Downloader, Media
from .core.twitter import (
    DEFAULT_ENDPOINT,
    DEFAULT_ORIGIN,
    ParseException,
    TwitterConfig,
    extract_urls,
    parse_tweet,
)

PLUGIN_NAME = "astr-twitter"
# KV 键：显式开关的会话、以及全局开关
KV_DISABLED_SESSIONS = "disabled_sessions"
KV_ENABLED_SESSIONS = "enabled_sessions"
KV_GLOBAL_AUTO_PARSE = "global_auto_parse"


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
@dataclass
class Settings:
    """插件配置的快照，避免到处 .get()。"""

    enabled: bool = True
    auto_parse: bool = True
    auto_parse_default: bool = False
    interrupt_event: bool = True
    notify_error: bool = False
    max_links: int = 3
    max_media: int = 9
    send_title: bool = True
    send_cover: bool = False
    keep_files: bool = False
    max_video_mb: int = 100
    debounce_seconds: int = 300
    # 解析器
    api_endpoint: str = DEFAULT_ENDPOINT
    api_origin: str = DEFAULT_ORIGIN
    cookie: str = ""
    proxy: str = ""
    timeout: float = 20.0
    retry: int = 2

    @classmethod
    def from_config(cls, config: Any) -> "Settings":
        def get(key: str, default: Any) -> Any:
            try:
                value = config.get(key, default)  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001
                return default
            return default if value is None else value

        def as_bool(key: str, default: bool) -> bool:
            return bool(get(key, default))

        def as_int(key: str, default: int) -> int:
            try:
                return int(get(key, default))
            except (TypeError, ValueError):
                return default

        def as_float(key: str, default: float) -> float:
            try:
                return float(get(key, default))
            except (TypeError, ValueError):
                return default

        return cls(
            enabled=as_bool("enabled", True),
            auto_parse=as_bool("auto_parse", True),
            auto_parse_default=as_bool("auto_parse_default", False),
            interrupt_event=as_bool("interrupt_event", True),
            notify_error=as_bool("notify_error", False),
            max_links=max(1, as_int("max_links", 3)),
            max_media=max(1, as_int("max_media", 9)),
            send_title=as_bool("send_title", True),
            send_cover=as_bool("send_cover", False),
            keep_files=as_bool("keep_files", False),
            max_video_mb=max(1, as_int("max_video_mb", 100)),
            debounce_seconds=max(0, as_int("debounce_seconds", 300)),
            api_endpoint=str(get("api_endpoint", DEFAULT_ENDPOINT) or DEFAULT_ENDPOINT),
            api_origin=str(get("api_origin", DEFAULT_ORIGIN) or DEFAULT_ORIGIN),
            cookie=str(get("cookie", "") or ""),
            proxy=str(get("proxy", "") or ""),
            timeout=max(3.0, as_float("timeout", 20.0)),
            retry=max(0, as_int("retry", 2)),
        )

    def twitter_config(self) -> TwitterConfig:
        return TwitterConfig(
            endpoint=self.api_endpoint,
            origin=self.api_origin,
            cookie=self.cookie,
            proxy=self.proxy or None,
            timeout=self.timeout,
            retry=self.retry,
        )


# --------------------------------------------------------------------------- #
# 插件
# --------------------------------------------------------------------------- #
class TwitterPlugin(Star):
    """X/Twitter 链接解析插件。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config
        self.settings = Settings.from_config(config)
        self._session: aiohttp.ClientSession | None = None
        self._downloader: Downloader | None = None
        # 内存态：会话/全局开关（kv 不可用时兜底）与防抖表
        self._disabled_sessions: set[str] = set()
        self._enabled_sessions: set[str] = set()
        self._global_auto_parse: bool | None = None
        self._policy_loaded = False
        self._recent: dict[str, float] = {}
        # 待清理的临时文件（发送完成后删除）
        self._pending_files: list[Path] = []

    # ---------------- 基础设施 ---------------- #

    @property
    def session(self) -> aiohttp.ClientSession:
        """惰性创建共享 aiohttp 会话（必须在事件循环内首次访问）。"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.settings.timeout),
            )
        return self._session

    @property
    def downloader(self) -> Downloader:
        if self._downloader is None:
            self._downloader = Downloader(
                self.session,
                base_dir=self.data_dir,
                proxy=self.settings.proxy or None,
                timeout=max(60.0, self.settings.timeout * 3),
                max_bytes=self.settings.max_video_mb * 1024 * 1024,
            )
        return self._downloader

    @property
    def data_dir(self) -> Path:
        """插件数据目录：data/plugin_data/<plugin>/twitter。"""
        candidates: list[Path] = []
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

            candidates.append(Path(get_astrbot_plugin_data_path()) / getattr(self, "name", PLUGIN_NAME))
        except Exception:  # noqa: BLE001
            pass
        try:
            from astrbot.api.star import StarTools

            candidates.append(Path(StarTools.get_data_dir()))
        except Exception:  # noqa: BLE001
            pass
        candidates.append(Path(tempfile.gettempdir()) / PLUGIN_NAME)

        for base in candidates:
            try:
                target = base / "twitter"
                target.mkdir(parents=True, exist_ok=True)
                return target
            except OSError:
                continue
        return Path(tempfile.gettempdir()) / PLUGIN_NAME / "twitter"

    # ---------------- 自动解析开关（会话 / 全局） ---------------- #

    async def _load_policy(self) -> None:
        """从 KV 读取开关状态（老版本 AstrBot 没有 kv 接口时退化为纯内存）。"""
        self._policy_loaded = True
        try:
            disabled = await self.get_kv_data(KV_DISABLED_SESSIONS, [])
            if isinstance(disabled, list):
                self._disabled_sessions = {str(x) for x in disabled}

            enabled = await self.get_kv_data(KV_ENABLED_SESSIONS, [])
            if isinstance(enabled, list):
                self._enabled_sessions = {str(x) for x in enabled}

            global_value = await self.get_kv_data(KV_GLOBAL_AUTO_PARSE, None)
            if global_value is None or isinstance(global_value, bool):
                self._global_auto_parse = global_value
        except Exception:  # noqa: BLE001 - kv 不可用时保留内存态
            pass

    async def _ensure_policy(self) -> None:
        """首次使用时加载一次，避免每条消息都读写存储。"""
        if not self._policy_loaded:
            await self._load_policy()

    async def _save_policy(self) -> None:
        self._policy_loaded = True
        try:
            await self.put_kv_data(KV_DISABLED_SESSIONS, sorted(self._disabled_sessions))
            await self.put_kv_data(KV_ENABLED_SESSIONS, sorted(self._enabled_sessions))
            await self.put_kv_data(KV_GLOBAL_AUTO_PARSE, self._global_auto_parse)
        except Exception:  # noqa: BLE001
            pass

    def _effective_auto_parse(self, umo: str) -> tuple[bool, str]:
        """判断某个会话现在是否要自动解析。

        优先级：本会话显式开关 > 全局开关 > 配置里的默认策略。
        """
        if umo in self._enabled_sessions:
            return True, "本会话"
        if umo in self._disabled_sessions:
            return False, "本会话"
        if self._global_auto_parse is not None:
            return self._global_auto_parse, "全局开关"
        return self.settings.auto_parse_default, "默认策略"

    def _debounced(self, key: str) -> bool:
        """同一链接在防抖窗口内只解析一次。"""
        seconds = self.settings.debounce_seconds
        if seconds <= 0:
            return False
        now = time.time()
        # 顺手清理过期项，避免无限增长
        for k, ts in list(self._recent.items()):
            if now - ts > seconds:
                self._recent.pop(k, None)
        last = self._recent.get(key)
        if last is not None and now - last < seconds:
            return True
        self._recent[key] = now
        return False

    @staticmethod
    def _collect_text(event: AstrMessageEvent) -> str:
        """把纯文本与卡片(Json)等组件里的内容收集起来，做链接匹配。"""
        parts: list[str] = [event.message_str or ""]
        try:
            for comp in getattr(event.message_obj, "message", None) or []:
                data = getattr(comp, "data", None)
                if isinstance(data, dict):
                    parts.append(json.dumps(data, ensure_ascii=False))
                elif isinstance(data, str):
                    parts.append(data)
                url = getattr(comp, "url", None)
                if isinstance(url, str):
                    parts.append(url)
        except Exception:  # noqa: BLE001
            pass
        return "\n".join(p for p in parts if p)

    @staticmethod
    def _looks_like_command(text: str) -> bool:
        """手动指令与自动解析都会命中，这里避免重复处理。"""
        stripped = (text or "").lstrip()
        for prefix in ("/", "!", "！", "。"):
            if stripped.startswith(prefix):
                rest = stripped[len(prefix) :]
                if re.match(r"^\s*(解析|推特解析|tw|x解析|开启解析|关闭解析|解析状态)\b", rest):
                    return True
        return False

    @staticmethod
    def _is_global_scope(text: str) -> bool:
        """`/开启解析 全局` → 作用于所有会话。"""
        return bool(re.search(r"(全局|所有会话|全部会话|\bglobal\b)", text or "", re.IGNORECASE))

    # ---------------- 解析 → 发送 ---------------- #

    @staticmethod
    async def _reply(event: AstrMessageEvent, text: str) -> None:
        """主动回一条文本（发送失败不影响主流程）。"""
        try:
            await event.send(event.plain_result(text))
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[{PLUGIN_NAME}] 回复失败: {e}")

    async def _handle_url(self, event: AstrMessageEvent, url: str, *, notify_error: bool) -> list[Any] | None:
        """解析单个链接并下载媒体，返回待发送的消息段；失败返回 None。"""
        settings = self.settings
        if Comp is None:  # pragma: no cover - 正常情况下一定可用
            logger.error(f"[{PLUGIN_NAME}] message_components 不可用，无法发送媒体")
            return None
        try:
            result = await parse_tweet(url, config=settings.twitter_config(), session=self.session)
        except ParseException as e:
            logger.warning(f"[{PLUGIN_NAME}] 解析失败 {url}: {e}")
            if notify_error:
                await self._reply(event, f"解析失败：{e}")
            return None

        logger.info(f"[{PLUGIN_NAME}] {result}")

        contents = result.contents[: settings.max_media]
        if not contents:
            if notify_error:
                await self._reply(event, "没有解析到可发送的媒体")
            return None

        segments: list[Any] = []
        if settings.send_title and result.title:
            segments.append(Comp.Plain(f"{result.title}\n"))

        for item in contents:
            try:
                media: Media = await self.downloader.download(
                    item.url,
                    subdir=result.tweet_id or "unknown",
                )
            except DownloadException as e:
                logger.warning(f"[{PLUGIN_NAME}] 下载失败 {item.type}: {e}")
                if notify_error:
                    await self._reply(event, f"媒体下载失败：{e}")
                continue

            if item.is_video_like:
                segments.append(Comp.Video.fromFileSystem(path=str(media.path)))
            else:
                segments.append(Comp.Image.fromFileSystem(str(media.path)))

            if not settings.keep_files:
                # 发送是异步的，这里交给发送后的清理钩子处理：先登记
                self._pending_cleanup(media.path)

        if settings.send_cover and result.cover:
            try:
                cover = await self.downloader.download(
                    result.cover,
                    subdir=result.tweet_id or "unknown",
                    filename=f"cover-{result.tweet_id or 'x'}.jpg",
                )
                segments.append(Comp.Image.fromFileSystem(str(cover.path)))
                if not settings.keep_files:
                    self._pending_cleanup(cover.path)
            except DownloadException as e:
                logger.debug(f"[{PLUGIN_NAME}] 封面下载失败（可忽略）: {e}")

        if not segments:
            return None
        return segments

    def _pending_cleanup(self, path: Path) -> None:
        self._pending_files.append(path)

    def _flush_cleanup(self) -> None:
        while self._pending_files:
            path = self._pending_files.pop()
            Downloader.cleanup(path)

    # ---------------- 自动解析 ---------------- #

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def on_message(self, event: AstrMessageEvent):
        """监听所有消息，发现推特链接就解析并发送媒体。"""
        settings = self.settings
        if not settings.enabled or not settings.auto_parse:
            return

        text = self._collect_text(event)
        if not text or self._looks_like_command(text):
            return

        # 忽略机器人自己发出的消息，避免自触发
        try:
            if event.get_sender_id() and event.get_sender_id() == event.get_self_id():
                return
        except Exception:  # noqa: BLE001
            pass

        # 会话 / 全局开关：默认策略为「需先发 /开启解析」
        await self._ensure_policy()
        allowed, source = self._effective_auto_parse(event.unified_msg_origin)
        if not allowed:
            logger.debug(f"[{PLUGIN_NAME}] 本会话未开启自动解析（{source}），跳过")
            return

        urls = extract_urls(text)
        if not urls:
            return

        handled = False
        for url in urls[: settings.max_links]:
            tweet_id = url.rstrip("/").rsplit("/", 1)[-1]
            if self._debounced(tweet_id):
                logger.debug(f"[{PLUGIN_NAME}] 防抖命中，跳过 {tweet_id}")
                continue
            segments = await self._handle_url(event, url, notify_error=settings.notify_error)
            if not segments:
                continue
            handled = True
            try:
                yield event.chain_result(segments)
            except Exception as e:  # noqa: BLE001 - 个别平台不支持视频时降级为文本
                logger.warning(f"[{PLUGIN_NAME}] 发送失败，降级为链接: {e}")
                yield event.plain_result(url)

        self._flush_cleanup()

        if handled and settings.interrupt_event:
            # 已处理链接，终止事件传播，避免再触发一次 LLM 回复
            event.stop_event()

    # ---------------- 指令 ---------------- #

    @filter.command("解析", alias={"推特解析", "tw", "x解析"})
    async def cmd_parse(self, event: AstrMessageEvent):
        """解析推特链接并把媒体发到当前会话：/解析 <链接>"""
        urls = extract_urls(self._collect_text(event))
        if not urls:
            yield event.plain_result("用法：/解析 <推特链接>")
            return

        sent = 0
        for url in urls[: self.settings.max_links]:
            segments = await self._handle_url(event, url, notify_error=True)
            if segments:
                sent += 1
                try:
                    yield event.chain_result(segments)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[{PLUGIN_NAME}] 发送失败: {e}")
                    yield event.plain_result(url)
        self._flush_cleanup()
        if sent == 0:
            logger.info(f"[{PLUGIN_NAME}] /解析 未成功解析任何链接")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("开启解析")
    async def cmd_enable(self, event: AstrMessageEvent):
        """开启推特自动解析：/开启解析（本会话）或 /开启解析 全局（所有会话，管理员）"""
        await self._ensure_policy()
        if self._is_global_scope(event.message_str):
            self._global_auto_parse = True
            text = "已开启【全局】推特自动解析 ✅\n之后所有会话里发推特链接都会自动解析。"
        else:
            self._enabled_sessions.add(event.unified_msg_origin)
            self._disabled_sessions.discard(event.unified_msg_origin)
            text = "已开启本会话的推特自动解析 ✅\n之后本会话里发推特链接就会自动解析。"
        await self._save_policy()
        logger.info(f"[{PLUGIN_NAME}] 开启自动解析: {text.splitlines()[0]}")
        yield event.plain_result(text)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("关闭解析")
    async def cmd_disable(self, event: AstrMessageEvent):
        """关闭推特自动解析：/关闭解析（本会话）或 /关闭解析 全局（所有会话，管理员）"""
        await self._ensure_policy()
        if self._is_global_scope(event.message_str):
            self._global_auto_parse = False
            text = "已关闭【全局】推特自动解析 ⛔\n之后所有会话都不再自动解析（仍可用 /解析 <链接> 手动解析）。"
        else:
            self._disabled_sessions.add(event.unified_msg_origin)
            self._enabled_sessions.discard(event.unified_msg_origin)
            text = "已关闭本会话的推特自动解析 ⛔\n仍可用 /解析 <链接> 手动解析。"
        await self._save_policy()
        logger.info(f"[{PLUGIN_NAME}] 关闭自动解析: {text.splitlines()[0]}")
        yield event.plain_result(text)

    @filter.command("解析状态")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看当前会话与插件的解析状态"""
        s = self.settings
        await self._ensure_policy()
        allowed, source = self._effective_auto_parse(event.unified_msg_origin)
        g = self._global_auto_parse
        global_text = "未设置" if g is None else ("开" if g else "关")
        yield event.plain_result(
            "astr-twitter 状态：\n"
            f"- 插件总开关：{'开' if s.enabled else '关'}\n"
            f"- 自动解析总开关：{'开' if s.auto_parse else '关'}\n"
            f"- 全局开关：{global_text}\n"
            f"- 默认策略：{'开启' if s.auto_parse_default else '关闭（需先 /开启解析）'}\n"
            f"- 本会话：{'自动解析中' if allowed else '不自动解析'}（来源：{source}）\n"
            f"- 接口：{s.api_endpoint}\n"
            f"- 代理：{s.proxy or '未设置'}"
        )

    # ---------------- LLM Tool ---------------- #

    @filter.llm_tool(name="parse_twitter_link")
    async def llm_parse_twitter_link(self, event: AstrMessageEvent, url: str):
        """解析 X/Twitter 链接，返回该推文的媒体信息（类型与数量），并尝试把媒体发送到当前会话。

        Args:
            url(string): 要解析的 X/Twitter 状态链接，例如 https://x.com/user/status/123
        """
        segments = await self._handle_url(event, url, notify_error=False)
        if not segments:
            yield event.plain_result(f"未能解析该链接：{url}")
            return
        try:
            yield event.chain_result(segments)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] llm_tool 发送失败: {e}")
            yield event.plain_result(f"解析成功但发送失败：{url}")
        self._flush_cleanup()

    # ---------------- 生命周期 ---------------- #

    async def terminate(self):
        """插件卸载/停用时关闭会话并清理临时文件。"""
        self._flush_cleanup()
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
