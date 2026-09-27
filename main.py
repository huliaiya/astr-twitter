# -*- coding: utf-8 -*-
"""astr-twitter 兼容层：保持原 main.py 位置供 AstrBot 导入。

AstrBot 的插件加载器期望在 <plugin_dir>/main.py 找到插件主类。
本文件仅作薄兼容层，实际实现已迁移到 astrbot_plugin_twitter 包内。
"""

from astrbot_plugin_twitter import (
    TwitterPlugin,
    Settings,
    PLUGIN_NAME,
    PLUGIN_ID,
    extract_urls,
    parse_tweet,
)
from astrbot_plugin_twitter.core.downloader import Downloader, DownloadException, sniff_kind

__all__ = [
    "TwitterPlugin",
    "Settings",
    "PLUGIN_NAME",
    "PLUGIN_ID",
    "extract_urls",
    "parse_tweet",
    "Downloader",
    "DownloadException",
    "sniff_kind",
]