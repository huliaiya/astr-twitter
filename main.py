# -*- coding: utf-8 -*-
"""astr-twitter：AstrBot 的 X/Twitter 链接解析插件。

对应文件：
  - metadata.yaml            插件元数据
  - _conf_schema.json        WebUI 配置面板
  - requirements.txt         依赖
  - core/twitter.py          解析核心（不依赖 AstrBot，可独立测试）
  - core/syndication.py      xdown 失败时的官方后备接口
  - core/downloader.py       媒体下载（流式 + 并发）
  - core/history.py          解析历史（供 WebUI 页面查看）
  - pages/history/           插件 WebUI 页面
  - .astrbot-plugin/i18n/    多语言
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

from .core.downloader import DownloadException, Downloader
from .core.history import HistoryRecord, HistoryStore
from .core.twitter import (
    DEFAULT_ENDPOINT,
    DEFAULT_ORIGIN,
    Content,
    ParseException,
    ParseResult,
    TwitterConfig,
    build_caption,
    extract_urls,
    parse_tweet,
    sanitize_filename,
    truncate,
)

PLUGIN_NAME = "astr-twitter"
# 与 metadata.yaml 的 name 保持一致：插件 Web API 的路由前缀与页面前缀都用它
PLUGIN_ID = "astrbot_plugin_twitter"
# KV 键：显式开关的会话、以及全局开关
KV_DISABLED_SESSIONS = "disabled_sessions"
KV_ENABLED_SESSIONS = "enabled_sessions"
KV_GLOBAL_AUTO_PARSE = "global_auto_parse"

TRIGGER_MODES = ("all", "at", "command_only")

# 封面下载的硬超时（秒）：封面宁可没有，也不能拖慢主体的发送
COVER_TIMEOUT = 6.0



# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
@dataclass
class Settings:
    """插件配置的快照，避免到处 .get()。"""

    enabled: bool = True
    auto_parse: bool = True
    auto_parse_default: bool = False
    trigger_mode: str = "all"  # all | at | command_only
    interrupt_event: bool = True
    notify_error: bool = False
    max_links: int = 3
    max_media: int = 9
    max_title_chars: int = 300
    send_title: bool = True
    send_author: bool = True
    send_media_info: bool = True
    send_link: bool = False
    caption_emoji: bool = False
    send_cover: bool = False
    quote_reply: bool = True  # 解析结果引用触发它的那条消息
    show_elapsed: bool = True  # 简介顶部显示解析耗时
    hint_when_disabled: bool = False  # 未开启自动解析时回一句提示
    send_audio: bool = False
    parse_quoted: bool = False
    keep_files: bool = False
    fallback_link: bool = True
    max_video_mb: int = 100
    download_concurrency: int = 3
    debounce_seconds: int = 300
    history_enabled: bool = True
    history_size: int = 200
    # 解析器
    api_endpoint: str = DEFAULT_ENDPOINT
    api_origin: str = DEFAULT_ORIGIN
    cookie: str = ""
    proxy: str = ""
    timeout: float = 20.0
    retry: int = 2
    fallback_syndication: bool = True

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
            trigger_mode=(
                str(get("trigger_mode", "all") or "all").strip().lower()
                if str(get("trigger_mode", "all") or "all").strip().lower() in TRIGGER_MODES
                else "all"
            ),
            interrupt_event=as_bool("interrupt_event", True),
            notify_error=as_bool("notify_error", False),
            max_links=max(1, as_int("max_links", 3)),
            max_media=max(1, as_int("max_media", 9)),
            max_title_chars=max(0, as_int("max_title_chars", 300)),
            send_title=as_bool("send_title", True),
            send_author=as_bool("send_author", True),
            send_media_info=as_bool("send_media_info", True),
            send_link=as_bool("send_link", False),
            caption_emoji=as_bool("caption_emoji", False),
            send_cover=as_bool("send_cover", False),
            show_elapsed=as_bool("show_elapsed", True),
            hint_when_disabled=as_bool("hint_when_disabled", False),
            quote_reply=as_bool("quote_reply", True),
            send_audio=as_bool("send_audio", False),
            parse_quoted=as_bool("parse_quoted", False),
            keep_files=as_bool("keep_files", False),
            fallback_link=as_bool("fallback_link", True),
            max_video_mb=max(1, as_int("max_video_mb", 100)),
            download_concurrency=max(1, min(8, as_int("download_concurrency", 3))),
            debounce_seconds=max(0, as_int("debounce_seconds", 300)),
            history_enabled=as_bool("history_enabled", True),
            history_size=max(10, min(2000, as_int("history_size", 200))),
            api_endpoint=str(get("api_endpoint", DEFAULT_ENDPOINT) or DEFAULT_ENDPOINT),
            api_origin=str(get("api_origin", DEFAULT_ORIGIN) or DEFAULT_ORIGIN),
            cookie=str(get("cookie", "") or ""),
            proxy=str(get("proxy", "") or ""),
            timeout=max(3.0, as_float("timeout", 20.0)),
            retry=max(0, as_int("retry", 2)),
            fallback_syndication=as_bool("fallback_syndication", True),
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
        # 记住哪些平台不接受「引用 + 媒体」，避免每次解析都多一次失败往返
        self._quote_unsupported: set[str] = set()
        self._segmented_cache: bool | None = None
        # 待清理的临时文件（发送完成后删除）
        self._pending_files: list[Path] = []
        # 解析历史（WebUI 页面用）
        self._history_store: HistoryStore | None = None
        self._register_web_apis()

    def _register_web_apis(self) -> None:
        """给 WebUI 页面注册插件 Web API（老版本 AstrBot 没有这个能力就跳过）。"""
        try:
            from astrbot.api.web import request  # noqa: F401 - 仅探测能力
        except Exception:  # noqa: BLE001
            logger.debug(f"[{PLUGIN_NAME}] 当前 AstrBot 版本不支持插件 Web API，忽略历史页面接口")
            return
        try:
            self.context.register_web_api(
                f"/{PLUGIN_ID}/history",
                self.api_history,
                ["GET"],
                "X 解析历史",
            )
            self.context.register_web_api(
                f"/{PLUGIN_ID}/history/clear",
                self.api_history_clear,
                ["POST"],
                "清空 X 解析历史",
            )
            logger.debug(f"[{PLUGIN_NAME}] 已注册插件 Web API：/{PLUGIN_ID}/history")
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[{PLUGIN_NAME}] 注册插件 Web API 失败: {e}")

    # ---------------- 插件 Web API（pages/history 调用） ---------------- #

    async def api_history(self):
        """GET 解析历史（供 WebUI 页面使用）。"""
        from astrbot.api.web import request

        limit = request.query.get("limit", 50, type=int) or 50
        offset = request.query.get("offset", 0, type=int) or 0
        store = self._history()
        return {
            "status": "ok",
            "data": {
                "records": await store.list(limit=limit, offset=offset),
                "stats": await store.stats(),
                "settings": {
                    "history_size": self.settings.history_size,
                    "api_endpoint": self.settings.api_endpoint,
                    "proxy": bool(self.settings.proxy),
                    "default_auto_parse": self.settings.auto_parse_default,
                    "trigger_mode": self.settings.trigger_mode,
                },
            },
        }

    async def api_history_clear(self):
        """POST 清空解析历史。"""
        removed = await self._history().clear()
        logger.info(f"[{PLUGIN_NAME}] 已通过 WebUI 清空 {removed} 条解析历史")
        return {"status": "ok", "data": {"removed": removed}}

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
                concurrency=self.settings.download_concurrency,
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
                if re.match(r"^\s*(解析|[Xx]解析|tw|开启解析|关闭解析|解析状态|解析历史)\b", rest):
                    return True
        return False

    @staticmethod
    def _is_global_scope(text: str) -> bool:
        """`/开启解析 全局` → 作用于所有会话。"""
        return bool(re.search(r"(全局|所有会话|全部会话|\bglobal\b)", text or "", re.IGNORECASE))

    @staticmethod
    def _is_at_bot(event: AstrMessageEvent) -> bool:
        """消息里有没有 @本机器人（trigger_mode=at 时用）。"""
        try:
            self_id = str(event.get_self_id() or "")
        except Exception:  # noqa: BLE001
            self_id = ""
        if not self_id:
            return False
        for comp in getattr(event.message_obj, "message", None) or []:
            qq = getattr(comp, "qq", None)
            if qq is not None and str(qq) == self_id:
                return True
        text = event.message_str or ""
        return f"[At:{self_id}]" in text or f"@{self_id}" in text

    # ---------------- 解析 → 发送 ---------------- #

    def _with_quote(self, event: AstrMessageEvent, segments: list[Any]) -> list[Any]:
        """按需在最前面插入「引用回复」，引用触发解析的那条消息（谁发的就引用谁）。

        AstrBot 官方的 result_decorate 阶段也是用 Reply(id=message_id) 做的，
        这里做成插件级开关，方便按需关闭。
        """
        if not self.settings.quote_reply or Comp is None or not segments:
            return segments
        if self._platform_key(event) in self._quote_unsupported:
            return segments
        reply_cls = getattr(Comp, "Reply", None)
        if reply_cls is None:  # 老版本 AstrBot 没有 Reply 组件
            return segments
        message_id = getattr(getattr(event, "message_obj", None), "message_id", None)
        if not message_id:
            return segments
        # AstrBot 的 respond 阶段校验 Reply 是否有效时要求 id 与 sender_id 都在
        fields: dict[str, Any] = {"id": message_id}
        try:
            sender_id = event.get_sender_id()
            if sender_id:
                fields["sender_id"] = sender_id
            sender_name = event.get_sender_name()
            if sender_name:
                fields["sender_nickname"] = sender_name
        except Exception:  # noqa: BLE001
            pass
        try:
            return [reply_cls(**fields), *segments]
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[{PLUGIN_NAME}] 构造引用回复失败，改为普通发送: {e}")
            return segments

    def _platform_key(self, event: AstrMessageEvent) -> str:
        """平台标识，用于记住「这个平台不接受带引用的媒体消息」。"""
        try:
            return str(event.get_platform_id() or event.get_platform_name() or "")
        except Exception:  # noqa: BLE001
            return ""

    def _segmented_reply_enabled(self) -> bool:
        """AstrBot 开了「分段回复」吗？

        开了的话 respond 阶段会把消息链拆成一个个组件单独发送，而且 Reply 只会
        挂在第一条上（`header_comps.clear()`），媒体那条就丢掉引用了；组件之间的
        间隔（默认 1.5~3.5 秒）也会让「发完视频」明显变慢。
        所以这种情况下我们主动把简介单独发，媒体自己带引用。
        """
        if self._segmented_cache is None:
            enabled = False
            try:
                config = self.context.get_config()
                settings = config.get("platform_settings", {}).get("segmented_reply", {})
                enabled = bool(settings.get("enable")) and not bool(
                    settings.get("only_llm_result", True)
                )
            except Exception:  # noqa: BLE001 - 读不到就按没开处理
                enabled = False
            self._segmented_cache = enabled
            if enabled:
                logger.info(
                    f"[{PLUGIN_NAME}] 检测到 AstrBot 开启了分段回复：简介将单独发送，"
                    "媒体消息单独带引用，避免引用被分段逻辑丢掉"
                )
        return self._segmented_cache

    @staticmethod
    def _split_caption(segments: list[Any]) -> tuple[list[Any], list[Any]]:
        """把首个纯文本段（简介）与其他媒体段分开。"""
        caption: list[Any] = []
        media: list[Any] = []
        for segment in segments:
            if not caption and not media and type(segment).__name__ == "Plain":
                caption.append(segment)
            else:
                media.append(segment)
        return caption, media

    async def _send_chain(self, event: AstrMessageEvent, segments: list[Any], url: str):
        """发送媒体链：先带引用发，失败再退到不带引用，最后退到「简介 + 链接」。

        带引用发送失败（一般是个别平台不支持引用 + 媒体）时会记到
        self._quote_unsupported 里，同一平台之后不再重复试，省掉一次失败往返。
        """
        platform = self._platform_key(event)

        # 分段回复场景：简介先单独发一条，媒体再自己带引用发
        caption_segments, media_segments = self._split_caption(segments)
        caption_sent = False
        if self._segmented_reply_enabled() and caption_segments and media_segments:
            try:
                yield event.chain_result(caption_segments)
                caption_sent = True
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[{PLUGIN_NAME}] 简介单独发送失败: {e}")
            segments = media_segments

        logger.info(
            f"[{PLUGIN_NAME}] 发送解析结果：平台={platform or '未知'} "
            f"组件={[type(c).__name__ for c in segments]}"
        )

        attempts: list[list[Any]] = []
        quoted = self._with_quote(event, segments)
        if quoted is not segments:
            attempts.append(quoted)
        attempts.append(segments)
        for index, chain in enumerate(attempts):
            try:
                yield event.chain_result(chain)
                return
            except Exception as e:  # noqa: BLE001 - 个别平台不支持视频/引用
                if index == 0 and len(attempts) > 1:
                    logger.warning(f"[{PLUGIN_NAME}] 带引用发送失败，改为普通发送: {e}")
                    if platform:
                        self._quote_unsupported.add(platform)
                        logger.info(
                            f"[{PLUGIN_NAME}] 平台 {platform} 不支持带引用的媒体消息，"
                            "本次运行内后续解析不再尝试引用"
                        )
                else:
                    logger.warning(f"[{PLUGIN_NAME}] 发送失败: {e}")
        # 媒体彻底发不出去：退回「简介 + 直链」，至少别把作者/正文信息丢掉
        # （简介已经单独发过就不重复了）
        fallback = "" if caption_sent else self._plain_text_of(segments)
        yield event.plain_result(f"{fallback}{url}" if fallback else url)

    @staticmethod
    def _plain_text_of(segments: list[Any]) -> str:
        """把消息段里的纯文本拼起来（用于发送失败时的兜底）。"""
        parts: list[str] = []
        for segment in segments:
            text = getattr(segment, "text", None)
            if isinstance(text, str) and text:
                parts.append(text.rstrip("\n"))
        return "\n".join(parts) + "\n" if parts else ""

    @staticmethod
    async def _reply(event: AstrMessageEvent, text: str) -> None:
        """主动回一条文本（发送失败不影响主流程）。"""
        try:
            await event.send(event.plain_result(text))
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[{PLUGIN_NAME}] 回复失败: {e}")

    async def _handle_url(
        self,
        event: AstrMessageEvent,
        url: str,
        *,
        notify_error: bool,
        depth: int = 0,
        started: float | None = None,
    ) -> list[Any] | None:
        """解析单个链接并下载媒体，返回待发送的消息段；失败返回 None。

        depth 用于「引用/转发原推」的一层递归（depth=1 时不再继续）。
        started 是收到消息的时间戳，用来算「解析耗时」（含解析 + 下载）。
        """
        settings = self.settings
        started = time.monotonic() if started is None else started
        if Comp is None:  # pragma: no cover - 正常情况下一定可用
            logger.error(f"[{PLUGIN_NAME}] message_components 不可用，无法发送媒体")
            return None
        try:
            result = await parse_tweet(
                url,
                config=settings.twitter_config(),
                session=self.session,
                fallback=settings.fallback_syndication,
            )
        except ParseException as e:
            logger.warning(f"[{PLUGIN_NAME}] 解析失败 {url}: {e}")
            await self._record_failure(event, url, str(e))
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

        # 多图推文并行下载（带并发上限），边下边校验大小
        wanted = [item for item in contents if item.type != "audio" or settings.send_audio]

        # 视频/GIF 的封面单独取一张（图片推文的封面就是图片本身，不重复发）
        cover_item: Content | None = None
        if settings.send_cover and result.cover and not result.cover_is_content:
            cover_item = Content(
                type="image",
                url=result.cover,
                label="封面",
                filename=sanitize_filename(f"cover-{result.tweet_id or 'x'}.jpg", ".jpg"),
            )

        batch = ([cover_item] if cover_item else []) + wanted
        outcome = await self.downloader.download_many(
            [
                (
                    item.url,
                    {
                        "subdir": result.tweet_id or "unknown",
                        "filename": item.filename,
                        # 封面只是锦上添花：给个短超时，别让它拖着视频一起等
                        **({"timeout": min(settings.timeout, COVER_TIMEOUT)} if item is cover_item else {}),
                    },
                )
                for item in batch
            ],
            max_bytes=settings.max_video_mb * 1024 * 1024,
        )

        cover_segments: list[Any] = []
        media_segments: list[Any] = []
        downloaded = 0
        total_bytes = 0
        for index, (item, media) in enumerate(zip(batch, outcome)):
            is_cover = cover_item is not None and index == 0
            target = cover_segments if is_cover else media_segments
            if isinstance(media, Exception):
                logger.warning(f"[{PLUGIN_NAME}] 下载失败 {item.type}({item.label or ''}): {media}")
                if is_cover:
                    logger.debug(f"[{PLUGIN_NAME}] 封面下载失败（可忽略）: {media}")
                    continue
                if settings.fallback_link:
                    target.append(Comp.Plain(f"{item.url}\n"))
                if notify_error:
                    await self._reply(event, f"媒体下载失败：{item.type} {media}")
                continue
            if not is_cover:
                downloaded += 1
                total_bytes += media.size
            if item.is_video_like:
                try:
                    target.append(
                        Comp.Video.fromFileSystem(path=str(media.path), cover=result.cover or "")
                    )
                except Exception as e:  # noqa: BLE001 - 封面参数不被接受时退回普通视频
                    logger.debug(f"[{PLUGIN_NAME}] 带封面发送失败，改为普通视频: {e}")
                    target.append(Comp.Video.fromFileSystem(path=str(media.path)))
            elif item.type == "audio":
                target.append(Comp.Record.fromFileSystem(str(media.path)))
            else:
                target.append(Comp.Image.fromFileSystem(str(media.path)))
            if not settings.keep_files:
                # 发送是异步的，先登记，发送完成后由清理钩子删除
                self._pending_cleanup(media.path)

        # 封面放最前面（像缩略图预览），其余媒体随后
        segments.extend(cover_segments)
        segments.extend(media_segments)

        # 简介（作者 / 媒体信息 / 正文）最后拼：这样「解析耗时」才能包含下载时间
        caption = build_caption(
            result,
            include_text=settings.send_title,
            include_author=settings.send_author,
            include_media_info=settings.send_media_info,
            include_link=settings.send_link,
            emoji=settings.caption_emoji,
            max_chars=settings.max_title_chars,
            elapsed=time.monotonic() - started,
            show_elapsed=settings.show_elapsed,
        )
        if caption:
            segments.insert(0, Comp.Plain(caption + "\n"))

        await self._record_success(event, result, downloaded, total_bytes)

        # 引用/转发的原推：可选地再解析一层
        if (
            settings.parse_quoted
            and depth == 0
            and result.quoted_url
            and result.quoted_url != result.url
        ):
            logger.info(f"[{PLUGIN_NAME}] 继续解析被引用的原推 {result.quoted_url}")
            quoted = await self._handle_url(event, result.quoted_url, notify_error=False, depth=1)
            if quoted:
                segments.extend(quoted)

        if not segments:
            return None
        return segments

    # ---------------- 历史记录 ---------------- #

    def _history(self) -> HistoryStore:
        if self._history_store is None:
            self._history_store = HistoryStore(
                self.data_dir / "history.json",
                max_records=self.settings.history_size,
            )
        return self._history_store

    def _base_record(self, event: AstrMessageEvent, url: str) -> HistoryRecord:
        return HistoryRecord(
            url=url,
            session=str(event.unified_msg_origin),
            sender=str(event.get_sender_id() or ""),
        )

    async def _record_success(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
        media: int,
        total_bytes: int,
    ) -> None:
        if not self.settings.history_enabled:
            return
        record = self._base_record(event, result.url)
        record.tweet_id = result.tweet_id
        record.ok = True
        record.source = result.source
        record.counts = result.counts
        record.media = media
        record.bytes = total_bytes
        record.title = truncate(result.title, 200)
        record.author = result.author_name
        record.is_repost = result.is_repost
        try:
            await self._history().add(record)
        except Exception as e:  # noqa: BLE001 - 历史记录失败不影响解析
            logger.debug(f"[{PLUGIN_NAME}] 写入历史失败: {e}")

    async def _record_failure(self, event: AstrMessageEvent, url: str, error: str) -> None:
        if not self.settings.history_enabled:
            return
        record = self._base_record(event, url)
        record.tweet_id = url.rstrip("/").rsplit("/", 1)[-1]
        record.ok = False
        record.error = error[:200]
        try:
            await self._history().add(record)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[{PLUGIN_NAME}] 写入历史失败: {e}")

    def _pending_cleanup(self, path: Path) -> None:
        self._pending_files.append(path)

    def _flush_cleanup(self) -> None:
        while self._pending_files:
            path = self._pending_files.pop()
            Downloader.cleanup(path)

    # ---------------- 自动解析 ---------------- #

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def on_message(self, event: AstrMessageEvent):
        """监听所有消息，发现 X 链接就解析并发送媒体。"""
        settings = self.settings
        started = time.monotonic()
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

        # 触发方式：all=消息里有链接就解析；at=需要 @机器人；command_only=只用指令/LLM 工具
        if settings.trigger_mode == "command_only":
            return
        if settings.trigger_mode == "at" and not self._is_at_bot(event):
            logger.debug(f"[{PLUGIN_NAME}] trigger_mode=at 且未 @机器人，跳过")
            return

        # 会话 / 全局开关：默认策略为「需先发 /开启解析」
        urls = extract_urls(text)
        if not urls:
            return

        await self._ensure_policy()
        allowed, source = self._effective_auto_parse(event.unified_msg_origin)
        if not allowed:
            # 有人确实发了链接，只是本会话没开——这种情况值得留一条 INFO，方便排查
            logger.info(
                f"[{PLUGIN_NAME}] 发现 X 链接但本会话未开启自动解析（{source}）；"
                f"发 /开启解析 即可开启，或直接用 /解析 <链接>。会话：{event.unified_msg_origin}"
            )
            if settings.hint_when_disabled:
                await self._reply(event, "本会话未开启 X 自动解析，发 /开启解析 开启，或直接用 /解析 <链接>")
            return

        handled = False
        for url in urls[: settings.max_links]:
            tweet_id = url.rstrip("/").rsplit("/", 1)[-1]
            if self._debounced(tweet_id):
                logger.debug(f"[{PLUGIN_NAME}] 防抖命中，跳过 {tweet_id}")
                continue
            segments = await self._handle_url(
                event, url, notify_error=settings.notify_error, started=started
            )
            if not segments:
                continue
            handled = True
            async for item in self._send_chain(event, segments, url):
                yield item

        self._flush_cleanup()

        if handled and settings.interrupt_event:
            # 已处理链接，终止事件传播，避免再触发一次 LLM 回复
            event.stop_event()

    # ---------------- 指令 ---------------- #

    @filter.command("解析", alias={"X解析", "x解析", "tw", "推特解析"})
    async def cmd_parse(self, event: AstrMessageEvent):
        """解析 X 链接并把媒体发到当前会话：/解析 <链接>"""
        started = time.monotonic()
        urls = extract_urls(self._collect_text(event))
        if not urls:
            yield event.plain_result("用法：/解析 <X 链接>")
            return

        sent = 0
        for url in urls[: self.settings.max_links]:
            segments = await self._handle_url(event, url, notify_error=True, started=started)
            if segments:
                sent += 1
                async for item in self._send_chain(event, segments, url):
                    yield item
        self._flush_cleanup()
        if sent == 0:
            logger.info(f"[{PLUGIN_NAME}] /解析 未成功解析任何链接")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("解析历史")
    async def cmd_history(self, event: AstrMessageEvent):
        """查看最近的解析记录（管理员）：/解析历史 [条数]"""
        match = re.search(r"(\d+)", event.message_str or "")
        limit = max(1, min(20, int(match.group(1)) if match else 5))
        if not self.settings.history_enabled:
            yield event.plain_result("历史记录已在配置里关闭（history_enabled=false）。")
            return

        records = await self._history().list(limit=limit)
        stats = await self._history().stats()
        if not records:
            yield event.plain_result("还没有解析记录。")
            return

        lines = [
            f"最近 {len(records)} 条解析记录"
            f"（共 {stats['total']} 条，成功 {stats['ok']} / 失败 {stats['failed']}，"
            f"成功率 {stats['success_rate']}%）："
        ]
        for record in records:
            icon = "✅" if record.get("ok") else "❌"
            counts = record.get("counts") or {}
            detail = " ".join(
                f"{name}×{counts[name]}" for name in ("video", "image", "dynamic", "audio") if counts.get(name)
            )
            if not record.get("ok"):
                detail = str(record.get("error") or "失败")[:40]
            lines.append(f"{icon} {record.get('time', '')} {detail} {record.get('url', '')}")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("开启解析")
    async def cmd_enable(self, event: AstrMessageEvent):
        """开启 X 自动解析：/开启解析（本会话）或 /开启解析 全局（所有会话，管理员）"""
        await self._ensure_policy()
        if self._is_global_scope(event.message_str):
            self._global_auto_parse = True
            text = "已开启【全局】X 自动解析 ✅\n之后所有会话里发 X 链接都会自动解析。"
        else:
            self._enabled_sessions.add(event.unified_msg_origin)
            self._disabled_sessions.discard(event.unified_msg_origin)
            text = "已开启本会话的 X 自动解析 ✅\n之后本会话里发 X 链接就会自动解析。"
        await self._save_policy()
        logger.info(f"[{PLUGIN_NAME}] 开启自动解析: {text.splitlines()[0]}")
        yield event.plain_result(text)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("关闭解析")
    async def cmd_disable(self, event: AstrMessageEvent):
        """关闭 X 自动解析：/关闭解析（本会话）或 /关闭解析 全局（所有会话，管理员）"""
        await self._ensure_policy()
        if self._is_global_scope(event.message_str):
            self._global_auto_parse = False
            text = "已关闭【全局】X 自动解析 ⛔\n之后所有会话都不再自动解析（仍可用 /解析 <链接> 手动解析）。"
        else:
            self._disabled_sessions.add(event.unified_msg_origin)
            self._enabled_sessions.discard(event.unified_msg_origin)
            text = "已关闭本会话的 X 自动解析 ⛔\n仍可用 /解析 <链接> 手动解析。"
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
        mode_text = {
            "all": "消息里有链接就解析",
            "at": "需要 @机器人",
            "command_only": "只用指令/LLM 工具",
        }.get(s.trigger_mode, s.trigger_mode)
        hint = (
            ""
            if allowed
            else "- 开启方式：发 /开启解析（只影响本会话），或 /开启解析 全局\n"
        )
        yield event.plain_result(
            "astr-twitter 状态：\n"
            f"- 插件总开关：{'开' if s.enabled else '关'}\n"
            f"- 自动解析总开关：{'开' if s.auto_parse else '关'}\n"
            f"- 触发方式：{mode_text}\n"
            f"- 全局开关：{global_text}\n"
            f"- 默认策略：{'开启' if s.auto_parse_default else '关闭（需先 /开启解析）'}\n"
            f"- 本会话：{'自动解析中' if allowed else '不自动解析'}（来源：{source}）\n"
            f"- 本会话 ID：{event.unified_msg_origin}\n"
            f"{hint}"
            f"- 简介显示解析耗时：{'开' if s.show_elapsed else '关'}\n"
            f"- 引用回复：{'开' if s.quote_reply else '关'}\n"
            f"- 后备接口：{'开' if s.fallback_syndication else '关'}\n"
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
        started = time.monotonic()
        segments = await self._handle_url(event, url, notify_error=False, started=started)
        if not segments:
            yield event.plain_result(f"未能解析该链接：{url}")
            return
        try:
            async for item in self._send_chain(event, segments, f"解析成功但发送失败：{url}"):
                yield item
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
