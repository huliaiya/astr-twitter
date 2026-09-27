# -*- coding: utf-8 -*-
"""astr-twitter 的解析核心（不依赖 AstrBot，可单独测试）。"""

from .downloader import DownloadException, Downloader, Media, sniff_kind
from .history import HistoryRecord, HistoryStore
from .syndication import parse_syndication, parse_syndication_json, syndication_token
from .twitter import (
    DEFAULT_ENDPOINT,
    DEFAULT_ORIGIN,
    Content,
    ParseException,
    ParseResult,
    TwitterConfig,
    build_caption,
    clean_title,
    decode_snapcdn,
    extract_handle,
    extract_urls,
    find_quoted_url,
    format_media_info,
    is_http_url,
    media_key,
    parse_tweet,
    parse_twitter_html,
    sanitize_filename,
    search_url,
    truncate,
)

__all__ = [
    "DEFAULT_ENDPOINT",
    "DEFAULT_ORIGIN",
    "Content",
    "DownloadException",
    "Downloader",
    "HistoryRecord",
    "HistoryStore",
    "Media",
    "ParseException",
    "ParseResult",
    "TwitterConfig",
    "build_caption",
    "clean_title",
    "decode_snapcdn",
    "extract_handle",
    "extract_urls",
    "find_quoted_url",
    "format_media_info",
    "is_http_url",
    "media_key",
    "sanitize_filename",
    "parse_syndication",
    "parse_syndication_json",
    "parse_tweet",
    "parse_twitter_html",
    "search_url",
    "syndication_token",
    "truncate",
]
