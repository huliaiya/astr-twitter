# -*- coding: utf-8 -*-
"""astr-twitter: AstrBot X 解析插件。

导出插件主类供 AstrBot 发现。
"""
from .main import (
    TwitterPlugin,
    Settings,
    PLUGIN_NAME,
    PLUGIN_ID,
    extract_urls,
    parse_tweet,
)
from .core.downloader import Downloader, DownloadException, sniff_kind

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
__version__ = "1.6.0"