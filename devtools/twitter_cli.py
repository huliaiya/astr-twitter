# -*- coding: utf-8 -*-
"""命令行解析工具（不需要 AstrBot，直接调用 core 层）。

用法：
    python devtools/twitter_cli.py <链接或含链接的文本> [--download 目录] [--raw]

示例：
    python devtools/twitter_cli.py https://x.com/Fortnite/status/1870484479980052921
    python devtools/twitter_cli.py "看这个 https://x.com/Dithmenos9/status/1966798448499286345" --download out

依赖：aiohttp、beautifulsoup4
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aiohttp  # noqa: E402

from core.downloader import Downloader  # noqa: E402
from core.twitter import ParseException, TwitterConfig, parse_tweet  # noqa: E402


async def run(input_text: str, download_dir: str | None) -> int:
    config = TwitterConfig(timeout=30.0, retry=3)
    async with aiohttp.ClientSession() as session:
        try:
            result = await parse_tweet(input_text, config=config, session=session)
        except ParseException as e:
            print(f"ParseException: {e}", file=sys.stderr)
            return 1

        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))

        if download_dir:
            downloader = Downloader(session, base_dir=Path(download_dir), timeout=120.0)
            for item in result.contents:
                try:
                    media = await downloader.download(item.url, subdir=result.tweet_id or "unknown")
                    print(
                        f"[{item.type}] {media.path} | {media.size} bytes | {media.kind}",
                        file=sys.stderr,
                    )
                except Exception as e:  # noqa: BLE001
                    print(f"[{item.type}] 下载失败: {e}", file=sys.stderr)
            if result.cover:
                try:
                    cover = await downloader.download(
                        result.cover,
                        subdir=result.tweet_id or "unknown",
                        filename=f"cover-{result.tweet_id or 'x'}.jpg",
                    )
                    print(f"[cover] {cover.path} | {cover.size} bytes | {cover.kind}", file=sys.stderr)
                except Exception as e:  # noqa: BLE001
                    print(f"[cover] 下载失败（pbs.twimg.com 直链可能被墙）: {e}", file=sys.stderr)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="独立版X解析器 CLI（无需 AstrBot）")
    parser.add_argument("input", help="X链接或包含链接的文本")
    parser.add_argument("--download", nargs="?", const="downloads", help="下载媒体到目录")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.input, args.download)))


if __name__ == "__main__":
    main()
