# -*- coding: utf-8 -*-
"""发送逻辑：引用回复、分段回复适配、重试降级、媒体链构建。"""

from __future__ import annotations

import time
from typing import Any, TYPE_CHECKING

from astrbot import logger
from astrbot.api.message_components import (
    Plain, Video, Reply, Image, Record, File, At, Node, Nodes, Json,
    Face, Poke, Forward, Music, Contact, Location, RPS, Dice, Shake, Share, Unknown,
    ComponentType,
)

from .config import Settings

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent
    from .core.twitter import Content, ParseResult

# 封面下载硬超时（秒）
COVER_TIMEOUT = 6.0


class Sender:
    """封装所有发送相关逻辑，不持有状态。"""

    def __init__(self, settings: Settings, downloader: Any, session: Any, data_dir: Any) -> None:
        self.settings = settings
        self.downloader = downloader
        self.session = session
        self.data_dir = data_dir
        self._quote_unsupported: set[str] = set()
        self._segmented_cache: bool | None = None
        self._pending_files: list[Any] = []

    # ------------------------------------------------------------------ #
    # 引用回复
    # ------------------------------------------------------------------ #
    def _with_quote(self, event: AstrMessageEvent, segments: list[Any]) -> list[Any]:
        """按需在最前面插入「引用回复」，引用触发解析的那条消息。

        AstrBot 官方的 result_decorate 阶段也是用 Reply(id=message_id) 做的。
        这里补齐 sender_id / sender_nickname，满足 respond 阶段校验。
        """
        if not self.settings.quote_reply or not segments:
            return segments
        message_id = getattr(getattr(event, "message_obj", None), "message_id", None)
        if not message_id:
            return segments
        # 如果该平台已知不支持引用，直接跳过
        if self._platform_key(event) in self._quote_unsupported:
            return segments
        try:
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
            return [Reply(**fields), *segments]
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[astr-twitter] 构造引用回复失败，改为普通发送: {e}")
            return segments

    def _platform_key(self, event: AstrMessageEvent) -> str:
        """平台标识，用于记住「这个平台不接受带引用的媒体消息」。"""
        try:
            return str(event.get_platform_id() or event.get_platform_name() or "")
        except Exception:  # noqa: BLE001
            return ""

    def _split_caption(self, segments: list[Any]) -> tuple[list[Any], list[Any]]:
        """把首个纯文本段（简介）与其他媒体段分开。"""
        caption: list[Any] = []
        media: list[Any] = []
        for segment in segments:
            if not caption and not media and type(segment).__name__ == "Plain":
                caption.append(segment)
            else:
                media.append(segment)
        return caption, media

    def _plain_text_of(self, segments: list[Any]) -> str:
        """把消息段里的纯文本拼起来（用于发送失败时的兜底）。"""
        parts: list[str] = []
        for segment in segments:
            text = getattr(segment, "text", None)
            if isinstance(text, str) and text:
                parts.append(text.rstrip("\n"))
        return "\n".join(parts) + "\n" if parts else ""

    # ------------------------------------------------------------------ #
    # 分段回复检测
    # ------------------------------------------------------------------ #
    def _segmented_reply_enabled(self) -> bool:
        """AstrBot 开了「分段回复」吗？

        开了的话 respond 阶段会把消息链拆成一个个组件单独发送，而且 Reply 只会
        挂在第一条上（header_comps.clear()），媒体那条就丢掉引用了；组件之间的
        间隔（默认 1.5~3.5 秒）也会让「发完视频」明显变慢。
        所以这种情况下我们主动把简介单独发，媒体自己带引用。
        """
        if self._segmented_cache is None:
            enabled = False
            try:
                # 这里无法直接拿到 context，由外部传入或在插件里缓存
                # 留作接口，实际判断在插件层做
                enabled = False
            except Exception:  # noqa: BLE001
                enabled = False
            self._segmented_cache = enabled
        return self._segmented_cache

    def set_segmented_reply(self, enabled: bool) -> None:
        """由插件初始化时调用，设置分段回复状态。"""
        self._segmented_cache = enabled

    # ------------------------------------------------------------------ #
    # 核心发送链
    # ------------------------------------------------------------------ #
    async def send_chain(
        self,
        event: AstrMessageEvent,
        segments: list[Any],
        url: str,
        *,
        segmented_reply: bool = False,
    ) -> Any:
        """发送媒体链：先带引用发，失败再退到不带引用，最后退到「完整简介 + 链接」。

        Args:
            event: 消息事件
            segments: 消息段列表（含简介 Plain + 媒体）
            url: 原始链接（兜底用）
            segmented_reply: 是否启用分段回复模式
        """
        platform = self._platform_key(event)

        # 先把完整的纯文本（简介）存下来，作为最后兜底
        full_caption_text = self._plain_text_of(segments).rstrip("\n")

        # 分段回复场景：简介先单独发一条，媒体再自己带引用发
        caption_segments, media_segments = self._split_caption(segments)
        caption_sent = False
        if segmented_reply and caption_segments and media_segments:
            try:
                yield event.chain_result(caption_segments)
                caption_sent = True
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[astr-twitter] 简介单独发送失败: {e}")
            segments = media_segments

        logger.info(
            f"[astr-twitter] 发送解析结果：平台={platform or '未知'} "
            f"组件={[type(c).__name__ for c in segments]} caption_sent={caption_sent}"
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
            except Exception as e:  # noqa: BLE001
                if index == 0 and len(attempts) > 1:
                    logger.warning(f"[astr-twitter] 带引用发送失败，改为普通发送: {e}")
                    if platform:
                        self._quote_unsupported.add(platform)
                        logger.info(
                            f"[astr-twitter] 平台 {platform} 不支持带引用的媒体消息，"
                            "本次运行内后续解析不再尝试引用"
                        )
                else:
                    logger.warning(f"[astr-twitter] 发送失败: {e}")

        # 媒体彻底发不出去：退回「完整简介 + 直链」
        fallback = "" if caption_sent else full_caption_text
        yield event.plain_result(f"{fallback}{url}" if fallback else url)

    # ------------------------------------------------------------------ #
    # 单链接处理
    # ------------------------------------------------------------------ #
    async def handle_url(
        self,
        event: AstrMessageEvent,
        url: str,
        *,
        notify_error: bool,
        depth: int = 0,
        started: float | None = None,
    ) -> list[Any] | None:
        """解析单个链接并下载媒体，返回待发送的消息段；失败返回 None。

        Args:
            event: 消息事件
            url: X/Twitter 链接
            notify_error: 解析/下载失败时是否主动通知用户
            depth: 引用/转发原推的递归深度（depth=1 时不再继续）
            started: 收到消息的时间戳，用来算「解析耗时」（含解析 + 下载）
        """
        from .core import parse_tweet, build_caption, format_elapsed
        from .core.twitter import ParseException, Content

        settings = self.settings
        started = time.monotonic() if started is None else started

        # 解析前提示：识别平台为 X，正在解析中...
        if settings.show_parsing_hint and notify_error:
            try:
                await event.send(event.plain_result("🔎 识别平台为 X，正在解析中..."))
            except Exception:  # noqa: BLE001
                pass

        try:
            result = await parse_tweet(
                url,
                config=settings.twitter_config(),
                session=self.session,
                fallback=settings.fallback_syndication,
            )
        except ParseException as e:
            logger.warning(f"[astr-twitter] 解析失败 {url}: {e}")
            await self._record_failure(event, url, str(e))
            if notify_error:
                await self._reply(event, f"解析失败：{e}")
            return None

        logger.info(f"[astr-twitter] {result}")

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
                filename=f"cover-{result.tweet_id or 'x'}.jpg",
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
                logger.warning(f"[astr-twitter] 下载失败 {item.type}({item.label or ''}): {media}")
                if is_cover:
                    logger.debug(f"[astr-twitter] 封面下载失败（可忽略）: {media}")
                    continue
                if settings.fallback_link:
                    target.append(Plain(f"{item.url}\n"))
                if notify_error:
                    await self._reply(event, f"媒体下载失败：{item.type} {media}")
                continue
            if not is_cover:
                downloaded += 1
                total_bytes += media.size
            if item.is_video_like:
                try:
                    target.append(
                        Video.fromFileSystem(path=str(media.path), cover=result.cover or "")
                    )
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"[astr-twitter] 带封面发送失败，改为普通视频: {e}")
                    target.append(Video.fromFileSystem(path=str(media.path)))
            elif item.type == "audio":
                target.append(Record.fromFileSystem(str(media.path)))
            else:
                target.append(Image.fromFileSystem(str(media.path)))
            if not settings.keep_files:
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
            segments.insert(0, Plain(caption + "\n"))

        await self._record_success(event, result, downloaded, total_bytes)

        # 引用/转发的原推：可选地再解析一层
        if (
            settings.parse_quoted
            and depth == 0
            and result.quoted_url
            and result.quoted_url != result.url
        ):
            logger.info(f"[astr-twitter] 继续解析被引用的原推 {result.quoted_url}")
            quoted = await self.handle_url(event, result.quoted_url, notify_error=False, depth=1)
            if quoted:
                segments.extend(quoted)

        if not segments:
            return None
        return segments

    # ------------------------------------------------------------------ #
    # 回复/记录/清理辅助
    # ------------------------------------------------------------------ #
    async def _reply(self, event: AstrMessageEvent, text: str) -> None:
        """主动回一条文本（发送失败不影响主流程）。"""
        try:
            await event.send(event.plain_result(text))
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[astr-twitter] 回复失败: {e}")

    async def _record_success(
        self, event: AstrMessageEvent, result: Any, downloaded: int, total_bytes: int
    ) -> None:
        if not self.settings.history_enabled:
            return
        from .core.history import HistoryRecord, HistoryStore
        record = HistoryRecord(
            ts=time.time(),
            url=result.url,
            tweet_id=result.tweet_id,
            ok=True,
            media=downloaded,
            bytes=total_bytes,
            title=result.title[:200] if result.title else "",
            author=result.author_name,
            is_repost=result.is_repost,
        )
        try:
            await HistoryStore(self.data_dir / "history.json", max_records=self.settings.history_size).add(record)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[astr-twitter] 写入历史失败: {e}")

    async def _record_failure(self, event: AstrMessageEvent, url: str, error: str) -> None:
        if not self.settings.history_enabled:
            return
        from .core.history import HistoryRecord, HistoryStore
        record = HistoryRecord(
            ts=time.time(),
            url=url,
            tweet_id=url.rstrip("/").rsplit("/", 1)[-1],
            ok=False,
            error=error[:200],
        )
        try:
            await HistoryStore(self.data_dir / "history.json", max_records=self.settings.history_size).add(record)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[astr-twitter] 写入历史失败: {e}")

    def _pending_cleanup(self, path: Any) -> None:
        self._pending_files.append(path)

    def flush_cleanup(self) -> None:
        from .core.downloader import Downloader
        while self._pending_files:
            path = self._pending_files.pop()
            Downloader.cleanup(path)