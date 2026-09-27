# -*- coding: utf-8 -*-
"""配置管理：Settings 数据类、验证、默认值、TwitterConfig 转换。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, ClassVar, Literal, get_type_hints

from astrbot import logger

from .core import (
    DEFAULT_ENDPOINT,
    DEFAULT_ORIGIN,
    TwitterConfig,
)

# 触发模式字面量
TriggerMode = Literal["all", "at", "command_only"]

# 正则：命令识别
_COMMAND_RE = re.compile(r"^\s*(解析|[Xx]解析|tw|开启解析|关闭解析|解析状态|解析历史)\b")


@dataclass(slots=True)
class Settings:
    """插件配置快照（不可变，线程安全）。

    所有字段均有默认值，支持从 AstrBot Config 实例构建。
    """

    # ===== 总开关 =====
    enabled: bool = True
    auto_parse: bool = True

    # ===== 自动解析策略 =====
    auto_parse_default: bool = False
    trigger_mode: TriggerMode = "all"
    interrupt_event: bool = True
    notify_error: bool = False
    hint_when_disabled: bool = False
    show_parsing_hint: bool = True

    # ===== 解析限制 =====
    max_links: int = 3
    max_media: int = 9
    max_title_chars: int = 300
    max_video_mb: int = 100
    debounce_seconds: int = 300

    # ===== 简介内容控制 =====
    send_title: bool = True
    send_author: bool = True
    send_media_info: bool = True
    send_link: bool = False
    caption_emoji: bool = False
    send_cover: bool = False
    show_elapsed: bool = True
    send_audio: bool = False
    parse_quoted: bool = False

    # ===== 发送行为 =====
    quote_reply: bool = True
    keep_files: bool = False
    fallback_link: bool = True
    fallback_syndication: bool = True

    # ===== 网络/下载 =====
    api_endpoint: str = DEFAULT_ENDPOINT
    api_origin: str = DEFAULT_ORIGIN
    cookie: str = ""
    proxy: str = ""
    timeout: float = 20.0
    retry: int = 2
    download_concurrency: int = 3

    # ===== 历史记录 =====
    history_enabled: bool = True
    history_size: int = 200

    # ===== 元数据（不参与配置比对）=====
    _config_version: int = field(default=1, repr=False, compare=False)

    # 字段名到类型的映射（用于验证）
    # 类型提示在类定义后动态获取（避免前向引用问题）
    _field_types: ClassVar[dict[str, type]] = {}

    # ------------------------------------------------------------------ #
    # 构建方法
    # ------------------------------------------------------------------ #
    @classmethod
    def from_config(cls, config: Any) -> "Settings":
        """从 AstrBot Config 实例构建 Settings。

        缺失键使用默认值；类型不匹配时记录警告并回退默认值。
        """
        def get(key: str, default: Any) -> Any:
            try:
                val = config.get(key, default)  # type: ignore[union-attr]
            except Exception:
                return default
            return default if val is None else val

        def as_bool(key: str, default: bool) -> bool:
            v = get(key, default)
            if isinstance(v, bool):
                return v
            if isinstance(v, (int, float)):
                return bool(v)
            if isinstance(v, str):
                return v.lower() in ("1", "true", "yes", "on")
            logger.warning(f"[astr-twitter] 配置 {key} 类型异常 {type(v)}，回退默认值 {default}")
            return default

        def as_int(key: str, default: int) -> int:
            v = get(key, default)
            try:
                return int(v)
            except (TypeError, ValueError):
                logger.warning(f"[astr-twitter] 配置 {key} 非整数 {v!r}，回退默认值 {default}")
                return default

        def as_float(key: str, default: float) -> float:
            v = get(key, default)
            try:
                return float(v)
            except (TypeError, ValueError):
                logger.warning(f"[astr-twitter] 配置 {key} 非浮点数 {v!r}，回退默认值 {default}")
                return default

        def as_trigger_mode(key: str, default: TriggerMode) -> TriggerMode:
            v = get(key, default)
            if isinstance(v, str):
                v = v.lower()
            if v in ("all", "at", "command_only"):
                return v  # type: ignore[return-value]
            logger.warning(f"[astr-twitter] 配置 {key} 值非法 {v!r}，回退默认值 {default}")
            return default

        # 解析类型提示（支持 from __future__ import annotations 的字符串形式）
        from typing import get_type_hints
        type_hints = get_type_hints(cls)

        kwargs = {}
        for f in fields(cls):
            if not f.init:
                continue
            t = type_hints.get(f.name, f.type)
            # 处理字符串形式的类型提示
            if isinstance(t, str):
                t = t.strip()
            if t == "bool" or t is bool:
                kwargs[f.name] = as_bool(f.name, f.default)
            elif t == "int" or t is int:
                val = as_int(f.name, f.default)
                if f.name == "download_concurrency":
                    val = min(max(val, 1), 8)
                elif f.name == "history_size":
                    val = max(val, 10)
                kwargs[f.name] = val
            elif t == "float" or t is float:
                kwargs[f.name] = as_float(f.name, f.default)
            elif f.name == "trigger_mode":
                kwargs[f.name] = as_trigger_mode(f.name, f.default)
            else:
                kwargs[f.name] = get(f.name, f.default)
        return cls(**kwargs)

    # ------------------------------------------------------------------ #
    # 转换方法
    # ------------------------------------------------------------------ #
    def twitter_config(self) -> TwitterConfig:
        """生成 core.TwitterConfig（传给解析器）。"""
        return TwitterConfig(
            endpoint=self.api_endpoint,
            origin=self.api_origin,
            cookie=self.cookie or None,
            proxy=self.proxy or None,
            timeout=self.timeout,
            retry=self.retry,
            fallback_syndication=self.fallback_syndication,
        )

    def to_dict(self) -> dict[str, Any]:
        """导出为字典（用于 WebUI 显示、调试）。"""
        return {f.name: getattr(self, f.name) for f in fields(self) if f.init}

    # ------------------------------------------------------------------ #
    # 验证
    # ------------------------------------------------------------------ #
    def validate(self) -> list[str]:
        """返回错误信息列表（空表示通过）。"""
        errors: list[str] = []
        if self.max_links <= 0:
            errors.append("max_links 必须 > 0")
        if self.max_media <= 0:
            errors.append("max_media 必须 > 0")
        if self.max_video_mb <= 0:
            errors.append("max_video_mb 必须 > 0")
        if self.timeout <= 0:
            errors.append("timeout 必须 > 0")
        if self.retry < 0:
            errors.append("retry 必须 >= 0")
        if self.download_concurrency <= 0:
            errors.append("download_concurrency 必须 > 0")
        if self.download_concurrency > 8:
            errors.append("download_concurrency 不能超过 8")
        if self.history_size <= 0:
            errors.append("history_size 必须 > 0")
        if self.max_title_chars <= 0:
            errors.append("max_title_chars 必须 > 0")
        if self.debounce_seconds < 0:
            errors.append("debounce_seconds 不能为负")
        return errors


# ---------------------------------------------------------------------- #
# KV 键常量
# ---------------------------------------------------------------------- #
KV_DISABLED_SESSIONS = "disabled_sessions"
KV_ENABLED_SESSIONS = "enabled_sessions"
KV_GLOBAL_AUTO_PARSE = "global_auto_parse"