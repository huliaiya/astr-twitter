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
    ParseException,
    ParseResult,
    TwitterConfig,
    extract_urls,
    parse_tweet,
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
    send_cover: bool = False
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
            send_cover=as_bool("send_cover", False),
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
                "推特解析历史",
            )
            self.context.register_web_api(
                f"/{PLUGIN_ID}/history/clear",
                self.api_history_clear,
                ["POST"],
                "清空推特解析历史",
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
                if re.match(r"^\s*(解析|推特解析|tw|x解析|开启解析|关闭解析|解析状态|解析历史)\b", rest):
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
    ) -> list[Any] | None:
        """解析单个链接并下载媒体，返回待发送的消息段；失败返回 None。

        depth 用于「引用/转发原推」的一层递归（depth=1 时不再继续）。
        """
        settings = self.settings
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
        title = truncate(result.title, settings.max_title_chars)
        if settings.send_title and title:
            prefix = "🔁 转发自 " if result.is_repost else ""
            segments.append(Comp.Plain(f"{prefix}{title}\n"))

        # 多图推文并行下载（带并发上限），边下边校验大小
        wanted = [item for item in contents if item.type != "audio" or settings.send_audio]
        outcome = await self.downloader.download_many(
            [(item.url, {"subdir": result.tweet_id or "unknown"}) for item in wanted],
            max_bytes=settings.max_video_mb * 1024 * 1024,
        )
        downloaded = 0
        total_bytes = 0
        for item, media in zip(wanted, outcome):
            if isinstance(media, Exception):
                logger.warning(f"[{PLUGIN_NAME}] 下载失败 {item.type}: {media}")
                if settings.fallback_link:
                    segments.append(Comp.Plain(f"{item.url}\n"))
                if notify_error:
                    await self._reply(event, f"媒体下载失败：{media}")
                continue
            downloaded += 1
            total_bytes += media.size
            if item.is_video_like:
                segments.append(Comp.Video.fromFileSystem(path=str(media.path)))
            elif item.type == "audio":
                segments.append(Comp.Record.fromFileSystem(str(media.path)))
            else:
                segments.append(Comp.Image.fromFileSystem(str(media.path)))
            if not settings.keep_files:
                # 发送是异步的，先登记，发送完成后由清理钩子删除
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

        # 触发方式：all=消息里有链接就解析；at=需要 @机器人；command_only=只用指令/LLM 工具
        if settings.trigger_mode == "command_only":
            return
        if settings.trigger_mode == "at" and not self._is_at_bot(event):
            logger.debug(f"[{PLUGIN_NAME}] trigger_mode=at 且未 @机器人，跳过")
            return

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
        mode_text = {
            "all": "消息里有链接就解析",
            "at": "需要 @机器人",
            "command_only": "只用指令/LLM 工具",
        }.get(s.trigger_mode, s.trigger_mode)
        yield event.plain_result(
            "astr-twitter 状态：\n"
            f"- 插件总开关：{'开' if s.enabled else '关'}\n"
            f"- 自动解析总开关：{'开' if s.auto_parse else '关'}\n"
            f"- 触发方式：{mode_text}\n"
            f"- 全局开关：{global_text}\n"
            f"- 默认策略：{'开启' if s.auto_parse_default else '关闭（需先 /开启解析）'}\n"
            f"- 本会话：{'自动解析中' if allowed else '不自动解析'}（来源：{source}）\n"
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
