# -*- coding: utf-8 -*-
"""自动解析策略：会话/全局开关、防抖、触发方式判断。"""

from __future__ import annotations

import time
from typing import Any

from astrbot import logger

from .config import (
    KV_DISABLED_SESSIONS,
    KV_ENABLED_SESSIONS,
    KV_GLOBAL_AUTO_PARSE,
    Settings,
)


class AutoParsePolicy:
    """管理自动解析的会话/全局开关策略。"""

    def __init__(self, settings: Settings, context: Any) -> None:
        self.settings = settings
        self.context = context
        self._disabled_sessions: set[str] = set()
        self._enabled_sessions: set[str] = set()
        self._global_auto_parse: bool | None = None
        self._policy_loaded = False
        self._recent: dict[str, float] = {}

    # ------------------------------------------------------------------ #
    # 持久化
    # ------------------------------------------------------------------ #
    async def _load_policy(self) -> None:
        """首次使用时加载一次，避免每条消息都读写存储。"""
        if self._policy_loaded:
            return
        try:
            disabled = await self.context.get_kv_data(KV_DISABLED_SESSIONS, [])
            if isinstance(disabled, list):
                self._disabled_sessions = {str(x) for x in disabled}

            enabled = await self.context.get_kv_data(KV_ENABLED_SESSIONS, [])
            if isinstance(enabled, list):
                self._enabled_sessions = {str(x) for x in enabled}

            global_value = await self.context.get_kv_data(KV_GLOBAL_AUTO_PARSE, None)
            if global_value is None or isinstance(global_value, bool):
                self._global_auto_parse = global_value
        except Exception:  # noqa: BLE001
            pass
        self._policy_loaded = True

    async def _save_policy(self) -> None:
        self._policy_loaded = True
        try:
            await self.context.put_kv_data(KV_DISABLED_SESSIONS, sorted(self._disabled_sessions))
            await self.context.put_kv_data(KV_ENABLED_SESSIONS, sorted(self._enabled_sessions))
            await self.context.put_kv_data(KV_GLOBAL_AUTO_PARSE, self._global_auto_parse)
        except Exception:  # noqa: BLE001
            pass

    async def ensure_loaded(self) -> None:
        if not self._policy_loaded:
            await self._load_policy()

    # ------------------------------------------------------------------ #
    # 判断逻辑
    # ------------------------------------------------------------------ #
    def effective_auto_parse(self, umo: str) -> tuple[bool, str]:
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

    # ------------------------------------------------------------------ #
    # 命令处理
    # ------------------------------------------------------------------ #
    async def enable(self, umo: str, global_flag: bool) -> str:
        """开启自动解析。"""
        await self.ensure_loaded()
        if global_flag:
            self._global_auto_parse = True
            self._enabled_sessions.clear()
            self._disabled_sessions.clear()
            await self._save_policy()
            return "已开启全局 X 自动解析 ✅"
        self._enabled_sessions.add(umo)
        self._disabled_sessions.discard(umo)
        await self._save_policy()
        return f"已开启本会话的 X 自动解析 ✅（{umo}）"

    async def disable(self, umo: str, global_flag: bool) -> str:
        """关闭自动解析。"""
        await self.ensure_loaded()
        if global_flag:
            self._global_auto_parse = False
            self._enabled_sessions.clear()
            self._disabled_sessions.clear()
            await self._save_policy()
            return "已关闭全局 X 自动解析 ❌"
        self._disabled_sessions.add(umo)
        self._enabled_sessions.discard(umo)
        await self._save_policy()
        return f"已关闭本会话的 X 自动解析 ❌（{umo}）"

    async def status(self, umo: str) -> str:
        """生成状态文本。"""
        await self.ensure_loaded()
        allowed, source = self.effective_auto_parse(umo)
        g = self._global_auto_parse
        global_text = "未设置" if g is None else ("开" if g else "关")
        mode_text = {
            "all": "消息里有链接就解析",
            "at": "需要 @机器人",
            "command_only": "只用指令/LLM 工具",
        }.get(self.settings.trigger_mode, self.settings.trigger_mode)
        hint = (
            ""
            if allowed
            else "- 开启方式：发 /开启解析（只影响本会话），或 /开启解析 全局\n"
        )
        return (
            "astr-twitter 状态：\n"
            f"- 插件总开关：{'开' if self.settings.enabled else '关'}\n"
            f"- 自动解析总开关：{'开' if self.settings.auto_parse else '关'}\n"
            f"- 触发方式：{mode_text}\n"
            f"- 全局开关：{global_text}\n"
            f"- 默认策略：{'开启' if self.settings.auto_parse_default else '关闭（需先 /开启解析）'}\n"
            f"- 本会话：{'自动解析中' if allowed else '不自动解析'}（来源：{source}）\n"
            f"- 本会话 ID：{umo}\n"
            f"{hint}"
            f"- 简介显示解析耗时：{'开' if self.settings.show_elapsed else '关'}\n"
            f"- 引用回复：{'开' if self.settings.quote_reply else '关'}\n"
            f"- 解析前提示：{'开' if self.settings.show_parsing_hint else '关'}\n"
            f"- 后备接口：{'开' if self.settings.fallback_syndication else '关'}\n"
            f"- 接口：{self.settings.api_endpoint}\n"
            f"- 代理：{self.settings.proxy or '未设置'}"
        )

    # ------------------------------------------------------------------ #
    # 防抖
    # ------------------------------------------------------------------ #
    def debounced(self, key: str) -> bool:
        """同一链接在防抖窗口内只解析一次。"""
        seconds = self.settings.debounce_seconds
        if seconds <= 0:
            return False
        now = time.time()
        # 顺手清理过期项，避免无限增长
        for k, ts in list(self._recent.items()):
            if now - ts > seconds:
                self._recent.pop(k, None)
        if key in self._recent:
            return True
        self._recent[key] = now
        return False

    # ------------------------------------------------------------------ #
    # 消息预处理
    # ------------------------------------------------------------------ #
    @staticmethod
    def collect_text(event: Any) -> str:
        """把纯文本与卡片(Json)等组件里的内容收集起来，做链接匹配。"""
        parts: list[str] = [event.message_str or ""]
        try:
            for comp in getattr(event.message_obj, "message", None) or []:
                data = getattr(comp, "data", None)
                if isinstance(data, dict):
                    parts.append(str(data))
                elif isinstance(data, str):
                    parts.append(data)
                url = getattr(comp, "url", None)
                if isinstance(url, str):
                    parts.append(url)
        except Exception:  # noqa: BLE001
            pass
        return "\n".join(p for p in parts if p)

    @staticmethod
    def looks_like_command(text: str) -> bool:
        """是否像指令（避免误触发）。"""
        return bool(_COMMAND_RE.search(text))

    @staticmethod
    def is_at_bot(event: Any) -> bool:
        """是否 @ 了机器人。"""
        self_id = event.get_self_id()
        if not self_id:
            return False
        for comp in getattr(event.message_obj, "message", None) or []:
            qq = getattr(comp, "qq", None)
            if qq is not None and str(qq) == self_id:
                return True
        text = event.message_str or ""
        return f"[At:{self_id}]" in text or f"@{self_id}" in text


# 复用正则（避免循环导入）
import re
_COMMAND_RE = re.compile(r"^\s*/?(解析|[Xx]解析|tw|开启解析|关闭解析|解析状态|解析历史)\b")