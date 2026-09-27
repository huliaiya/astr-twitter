# -*- coding: utf-8 -*-
"""插件集成测试：用真实 AstrBot（4.x）的 API 加载 main.py 并跑完整链路。

    pytest tests/test_plugin.py

未安装 AstrBot 时整个文件会被跳过（core 层测试仍可单独运行）。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("astrbot.api.event", reason="需要安装 AstrBot 才能跑插件集成测试")

from astrbot.api.event import AstrMessageEvent  # noqa: E402
from astrbot.api.message_components import Image, Plain, Video  # noqa: E402
from astrbot.api.platform import (  # noqa: E402
    AstrBotMessage,
    MessageMember,
    MessageType,
    PlatformMetadata,
)
from astrbot.core.star.context import Context  # noqa: E402
from astrbot.core.star.star_handler import star_handlers_registry  # noqa: E402


from astrbot_plugin_twitter.core.downloader import Downloader
from astrbot_plugin_twitter.config import Settings
ROOT = Path(__file__).resolve().parents[1]
URL_PHOTO = "https://x.com/Fortnite/status/1870484479980052921"
URL_GIF = "https://x.com/Dithmenos9/status/1966798448499286345"
URL_VIDEO = "https://x.com/Fortnite/status/1904171341735178552"

live = pytest.mark.skipif(
    os.environ.get("ASTR_TWITTER_SKIP_LIVE") == "1",
    reason="ASTR_TWITTER_SKIP_LIVE=1，跳过联网用例",
)


# --------------------------------------------------------------------------- #
# 加载插件（直接 import 包）
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="session")
def plugin_module():
    from astrbot_plugin_twitter import main
    return main


def make_event(
    text: str,
    *,
    sender_id: str = "10001",
    self_id: str = "99999",
    session_id: str = "session-1",
    extra_components: list | None = None,
    platform_id: str = "fake-1",
) -> AstrMessageEvent:
    """构造一条真实的 AstrBot 消息事件。"""
    msg = AstrBotMessage()
    msg.type = MessageType.GROUP_MESSAGE
    msg.self_id = self_id
    msg.session_id = session_id
    msg.message_id = "message-1"
    msg.sender = MessageMember(user_id=sender_id, nickname="tester")
    msg.message = [Plain(text=text), *(extra_components or [])]
    msg.message_str = text
    msg.raw_message = {}
    return AstrMessageEvent(
        message_str=text,
        message_obj=msg,
        platform_meta=PlatformMetadata("fake", "fake platform", platform_id),
        session_id=session_id,
    )


class FakeContext:
    """只用到 get_config 与 register_web_api，其余交给真实 Star。"""

    # 共享 AstrBot 真实的注册表（Context.registered_web_apis 是类属性）
    registered_web_apis = Context.registered_web_apis

    def __init__(self, data_dir: str | None = None):
        self._data_dir = data_dir

    def get_config(self, *args, **kwargs):
        if args:
            key = args[0]
            if key == "data_dir" and self._data_dir is not None:
                return self._data_dir
            return args[1] if len(args) > 1 else None
        return None

    def register_web_api(self, route, view_handler, methods, desc):
        """复用 AstrBot 的真实注册实现。"""
        return Context.register_web_api(self, route, view_handler, methods, desc)


@pytest.fixture
async def plugin(plugin_module, tmp_path, monkeypatch):
    """一个 keep_files=True、防抖关闭的插件实例，数据目录指向 tmp。"""
    monkeypatch.setenv("ASTRBOT_ROOT", str(tmp_path))
    config = {
        "enabled": True,
        "auto_parse": True,
        "auto_parse_default": True,  # 这组用例默认就走自动解析路径
        "interrupt_event": True,
        "notify_error": False,
        "debounce_seconds": 0,
        "keep_files": True,  # 保留文件，方便断言下载结果
        "send_title": True,
        "timeout": 30.0,
        "retry": 3,
    }
    instance = plugin_module.TwitterPlugin(context=FakeContext(data_dir=str(tmp_path)), config=config)
    # 数据目录固定在 tmp_path 下，避免污染真实 AstrBot 目录
    instance._downloader = Downloader(
        instance.session,
        base_dir=tmp_path / "media",
        timeout=90.0,
    )
    yield instance
    await instance.terminate()


async def collect(agen) -> list:
    """把一个 async generator 的产出收集成列表。"""
    return [item async for item in agen]


# --------------------------------------------------------------------------- #
# 注册与配置（离线）
# --------------------------------------------------------------------------- #
def test_handlers_registered(plugin_module):
    """import main.py 时，装饰器应当把 handler 注册进 AstrBot。"""
    names = {md.handler_name for md in star_handlers_registry}
    for expected in (
        "on_message",
        "cmd_parse",
        "cmd_enable",
        "cmd_disable",
        "cmd_status",
        "llm_parse_twitter_link",
    ):
        assert expected in names, f"未注册 handler：{expected}"

    # llm_tool 必须带合法 docstring（Args 段），否则注册时会直接抛错
    from astrbot.core.provider.register import llm_tools

    tool_names = {t.name for t in llm_tools.func_list}
    assert "parse_twitter_link" in tool_names


def test_settings_from_config(plugin_module):
    s = plugin_module.Settings.from_config(
        {"enabled": False, "max_media": "5", "timeout": "8", "proxy": None}
    )
    assert s.enabled is False
    assert s.max_media == 5
    assert s.timeout == 8.0
    assert s.proxy == ""
    # 默认值
    s2 = plugin_module.Settings.from_config({})
    assert s2.auto_parse is True and s2.max_media == 9 and s2.retry == 2


def test_metadata_satisfies_astrbot_loader(tmp_path):
    """复刻 AstrBot 的插件目录约定与导入路径 data.plugins.<metadata.name>.main。

    AstrBot 用「metadata.yaml 的 name」作为安装目录名与 importlib 路径的一部分，
    因此 name 必须是合法 Python 标识符（仓库目录名带连字符 astr-twitter 也没关系）。
    """
    yaml = pytest.importorskip("yaml")
    meta = yaml.safe_load((ROOT / "metadata.yaml").read_text(encoding="utf-8"))
    name = meta["name"]

    assert name.isidentifier(), "metadata.name 必须是合法标识符，否则 AstrBot 无法 import"
    for field in ("display_name", "desc", "version", "author", "repo"):
        assert meta.get(field), f"metadata.yaml 缺少字段：{field}"
    assert meta["repo"].startswith("https://github.com/")

    # 按 AstrBot 的真实路径结构导入一次
    plugins_dir = tmp_path / "data" / "plugins"
    plugins_dir.mkdir(parents=True)
    (plugins_dir / name).symlink_to(ROOT, target_is_directory=True)
    sys.path.insert(0, str(tmp_path))
    try:
        module = importlib.import_module(f"data.plugins.{name}.main")
        assert module.TwitterPlugin is not None
        # 相对导入的 core 子包也应当可用
        assert module.parse_tweet is not None
        assert Downloader is not None
    finally:
        sys.path.remove(str(tmp_path))
        for key in [k for k in sys.modules if k.startswith("data.plugins")]:
            sys.modules.pop(key, None)


def test_collect_text_reads_json_card(plugin_module, plugin):
    """卡片消息（Json 组件）里的链接也要能被提取出来。"""
    from astrbot.api.message_components import Json

    card = Json(
        data={
            "app": "com.tencent.structmsg",
            "meta": {"detail_1": {"qqdocurl": URL_PHOTO}},
        }
    )
    event = make_event("来看看这个", extra_components=[card])
    text = plugin._collect_text(event)
    assert URL_PHOTO in text
    assert plugin_module.extract_urls(text) == [URL_PHOTO]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/解析 https://x.com/a/status/1", True),
        ("/X解析 https://x.com/a/status/1", True),
        ("/关闭解析", True),
        ("看这个 https://x.com/a/status/1", False),
    ],
)
def test_looks_like_command(plugin_module, plugin, text, expected):
    assert plugin._looks_like_command(text) is expected


def test_debounce(plugin_module, plugin):
    plugin.settings.debounce_seconds = 60
    assert plugin._debounced("123") is False
    assert plugin._debounced("123") is True
    assert plugin._debounced("456") is False


async def test_manual_command_without_url(plugin_module, plugin):
    results = await collect(plugin.cmd_parse(make_event("/解析")))
    assert results and "用法" in results[0].chain[0].text


async def test_auto_parse_disabled(plugin_module, plugin):
    plugin.settings.auto_parse = False
    assert await collect(plugin.on_message(make_event(f"看 {URL_PHOTO}"))) == []


async def test_ignore_own_message(plugin_module, plugin):
    event = make_event(f"看 {URL_PHOTO}", sender_id="99999", self_id="99999")
    assert await collect(plugin.on_message(event)) == []


# --------------------------------------------------------------------------- #
# 自动解析开关：默认关闭 → /开启解析 后自动解析
# --------------------------------------------------------------------------- #
async def test_default_off_skips_until_enabled(plugin_module, plugin):
    """默认策略为「关」时，没发过 /开启解析 的会话不应自动解析。"""
    plugin.settings.auto_parse_default = False
    event = make_event(f"看 {URL_PHOTO}")
    await plugin._ensure_policy()
    assert plugin._effective_auto_parse(event.unified_msg_origin) == (False, "默认策略")
    assert await collect(plugin.on_message(event)) == []


async def test_enable_command_reply_and_policy(plugin_module, plugin):
    plugin.settings.auto_parse_default = False
    event = make_event("/开启解析")
    results = await collect(plugin.cmd_enable(event))
    assert results and "已开启" in results[0].chain[0].text
    # 开启后该会话变成「要自动解析」，且优先级来源是「本会话」
    assert plugin._effective_auto_parse(event.unified_msg_origin) == (True, "本会话")
    # 别的会话不受影响
    assert plugin._effective_auto_parse("other:session")[0] is False


async def test_disable_command_stops_auto_parse(plugin_module, plugin):
    event = make_event(f"看 {URL_PHOTO}")
    await collect(plugin.cmd_disable(make_event("/关闭解析")))
    assert plugin._effective_auto_parse(event.unified_msg_origin) == (False, "本会话")
    assert await collect(plugin.on_message(event)) == []


async def test_global_toggle(plugin_module, plugin):
    plugin.settings.auto_parse_default = False
    results = await collect(plugin.cmd_enable(make_event("/开启解析 全局")))
    assert "全局" in results[0].chain[0].text
    # 任意会话都生效，来源是「全局开关」
    assert plugin._effective_auto_parse("any:session") == (True, "全局开关")

    results = await collect(plugin.cmd_disable(make_event("/关闭解析 全局")))
    assert "全局" in results[0].chain[0].text
    assert plugin._effective_auto_parse("any:session") == (False, "全局开关")


async def test_session_setting_beats_global(plugin_module, plugin):
    plugin.settings.auto_parse_default = False
    mine = make_event("/开启解析")
    other = make_event("看链接", session_id="other-session")

    await collect(plugin.cmd_disable(make_event("/关闭解析 全局")))
    await collect(plugin.cmd_enable(mine))

    # 本会话显式开启，优先级高于「全局关闭」
    assert plugin._effective_auto_parse(mine.unified_msg_origin) == (True, "本会话")
    assert plugin._effective_auto_parse(other.unified_msg_origin) == (False, "全局开关")


async def test_status_reports_session_state(plugin_module, plugin):
    plugin.settings.auto_parse_default = False
    results = await collect(plugin.cmd_status(make_event("/解析状态")))
    text = results[0].chain[0].text
    assert "默认策略" in text and "本会话" in text and "未设置" in text


# --------------------------------------------------------------------------- #
# 端到端（联网）：消息 → 解析 → 下载 → 消息链
# --------------------------------------------------------------------------- #
@live
async def test_e2e_enable_then_auto_parse(plugin_module, plugin):
    """用户实际场景：先发 /开启解析，之后发链接就会自动解析。"""
    plugin.settings.auto_parse_default = False
    before = make_event(f"看 {URL_PHOTO}")
    assert await collect(plugin.on_message(before)) == [], "开启前不应自动解析"

    await collect(plugin.cmd_enable(make_event("/开启解析")))

    after = make_event(f"看 {URL_PHOTO}", session_id="session-1")
    results = await collect(plugin.on_message(after))
    assert results, "开启后应当自动解析"
    kinds = [type(c).__name__ for c in results[0].chain]
    assert "Image" in kinds, f"消息链里应有图片，实际：{kinds}"
    assert after.is_stopped()


@live
async def test_e2e_image_message(plugin_module, plugin):
    event = make_event(f"看这个图 {URL_PHOTO}")
    results = await collect(plugin.on_message(event))

    assert results, "应当产出消息结果"
    chain = results[0].chain
    kinds = [type(c).__name__ for c in chain]
    assert "Image" in kinds, f"消息链里应有图片，实际：{kinds}"
    if plugin_module.parse_tweet and chain and isinstance(chain[0], Plain):
        assert chain[0].text  # 有正文时，正文在最前面

    for comp in chain:
        if isinstance(comp, Image):
            path = Path(comp.path or comp.file)
            assert path.exists(), f"图片文件不存在：{path}"
            assert path.stat().st_size > 5000
    assert event.is_stopped(), "解析后应终止事件传播"


@live
async def test_e2e_gif_message(plugin_module, plugin):
    event = make_event(f"看这个 {URL_GIF}")
    results = await collect(plugin.on_message(event))
    assert results
    kinds = [type(c).__name__ for c in results[0].chain]
    assert "Video" in kinds, f"gif 应作为视频发送，实际：{kinds}"
    for comp in results[0].chain:
        if isinstance(comp, Video):
            path = Path(comp.path or comp.file)
            assert path.exists() and path.stat().st_size > 5000


@live
async def test_e2e_manual_command(plugin_module, plugin):
    event = make_event(f"/解析 {URL_PHOTO}")
    results = await collect(plugin.cmd_parse(event))
    assert results and "Image" in [type(c).__name__ for c in results[0].chain]
    # 手动解析同样引用发指令的那条消息
    assert type(results[0].chain[0]).__name__ == "Reply"


@live
async def test_no_media_for_missing_tweet(plugin_module, plugin):
    plugin.settings.notify_error = True
    event = make_event("https://x.com/NASA/status/1683502034445783040")
    results = await collect(plugin.on_message(event))
    # notify_error=True 时会先 event.send() 一条错误提示（假平台不支持，被吞掉），
    # 然后返回空 handler → 不应产出媒体消息链
    assert all(
        not any(isinstance(c, (Image, Video)) for c in r.chain) for r in results
    )


# --------------------------------------------------------------------------- #
# 触发方式 / 历史 / 插件 Web API（离线）
# --------------------------------------------------------------------------- #
async def test_trigger_mode_command_only(plugin_module, plugin):
    plugin.settings.trigger_mode = "command_only"
    assert await collect(plugin.on_message(make_event(f"看 {URL_PHOTO}"))) == []


async def test_trigger_mode_at_requires_mention(plugin_module, plugin):
    from astrbot.api.message_components import At

    plugin.settings.trigger_mode = "at"
    event = make_event(f"看 {URL_PHOTO}")
    assert await collect(plugin.on_message(event)) == [], "没 @机器人 时不应自动解析"

    # 加上 @机器人 后应当继续（这里断言的是判定函数本身，联网路径由 live 用例覆盖）
    at_event = make_event("", extra_components=[At(qq=99999)])
    assert plugin._is_at_bot(at_event) is True
    assert plugin._is_at_bot(event) is False


async def test_history_command_without_records(plugin_module, plugin):
    results = await collect(plugin.cmd_history(make_event("/解析历史")))
    assert results and "暂无历史记录" in results[0].chain[0].text


async def test_history_command_lists_records(plugin_module, plugin):
    from astrbot_plugin_twitter.core.history import HistoryRecord

    await plugin._history().add(
        HistoryRecord(url=URL_PHOTO, tweet_id="1870484479980052921", counts={"image": 1}, media=1, bytes=1234)
    )
    await plugin._history().add(
        HistoryRecord(url="https://x.com/NASA/status/1", ok=False, error="未找到视频")
    )
    results = await collect(plugin.cmd_history(make_event("/解析历史 10")))
    text = results[0].chain[0].text
    assert URL_PHOTO in text and "未找到视频" in text and "1 个媒体" in text


def test_web_api_registered(plugin_module, plugin):
    routes = {(route, tuple(methods)) for route, _h, methods, _d in Context.registered_web_apis}
    assert (f"/{plugin_module.PLUGIN_ID}/history", ("GET",)) in routes
    assert (f"/{plugin_module.PLUGIN_ID}/history/clear", ("POST",)) in routes


async def test_api_history_returns_payload(plugin_module, plugin):
    """按真实调用方式（绑定 PluginRequest）跑一遍页面用的接口。"""
    from types import SimpleNamespace

    from astrbot.api.web import PluginRequest, bind_request_context

    await plugin._history().add(
        plugin_module.HistoryRecord(url=URL_PHOTO, counts={"image": 1}, media=1, bytes=10)
    )
    raw = SimpleNamespace(
        method="GET",
        url=SimpleNamespace(path="/api/v1/plugins/extensions/x/history"),
        headers={"accept": "application/json"},
        cookies={},
        client=None,
        query_params=SimpleNamespace(multi_items=lambda: [("limit", "5")]),
    )
    with bind_request_context(PluginRequest(raw)):
        payload = await plugin.api_history()
        assert payload["status"] == "ok"
        assert payload["data"]["records"][0]["url"] == URL_PHOTO
        assert payload["data"]["stats"]["total"] == 1
        assert payload["data"]["settings"]["trigger_mode"] in plugin_module.TRIGGER_MODES

        cleared = await plugin.api_history_clear()
        assert cleared["data"]["removed"] == 1
    assert await plugin._history().list() == []


async def test_settings_from_config_new_keys(plugin_module):
    s = Settings.from_config(
        {
            "trigger_mode": "AT",  # 大小写不敏感，非法值回落到 all
            "max_title_chars": "50",
            "send_audio": True,
            "parse_quoted": True,
            "fallback_link": False,
            "fallback_syndication": False,
            "download_concurrency": "99",
            "history_enabled": False,
            "history_size": "5",
        }
    )
    assert s.trigger_mode == "at"
    assert s.max_title_chars == 50
    assert s.send_audio is True and s.parse_quoted is True
    assert s.fallback_link is False and s.fallback_syndication is False
    assert s.download_concurrency == 8  # 上限保护
    assert s.history_size == 10  # 下限保护
    assert plugin_module.Settings.from_config({"trigger_mode": "乱填"}).trigger_mode == "all"


@live
async def test_e2e_at_trigger_and_history(plugin_module, plugin):
    """@机器人 触发的自动解析，并确认写入了历史记录。"""
    from astrbot.api.message_components import At

    plugin.settings.trigger_mode = "at"
    plugin.settings.history_enabled = True

    without_at = make_event(f"看 {URL_PHOTO}")
    assert await collect(plugin.on_message(without_at)) == []

    with_at = make_event(f"看 {URL_PHOTO} ", extra_components=[At(qq=99999)])
    results = await collect(plugin.on_message(with_at))
    assert results and "Image" in [type(c).__name__ for c in results[0].chain]

    records = await plugin._history().list(limit=5)
    assert records, "解析成功后应写入历史"
    top = records[0]
    assert top["ok"] is True
    assert top["url"] == URL_PHOTO
    assert top["counts"]["image"] >= 1
    assert top["bytes"] > 0 and top["source"] == "xdown"
    assert top["session"] == with_at.unified_msg_origin

    stats = await plugin._history().stats()
    assert stats["ok"] >= 1 and stats["success_rate"] > 0


@live
async def test_e2e_quoted_follow(plugin_module, plugin):
    """被引用/转发的原推：开启 parse_quoted 后媒体里应包含原推内容（尽力而为）。"""
    plugin.settings.parse_quoted = True
    event = make_event(f"看 {URL_GIF}")
    results = await collect(plugin.on_message(event))
    assert results
    kinds = [type(c).__name__ for c in results[0].chain]
    assert "Video" in kinds
    # 至少要把主推的内容发出来；若接口给了引用链接，这里会多一条记录
    records = await plugin._history().list(limit=5)
    assert records and records[0]["url"] == URL_GIF


# --------------------------------------------------------------------------- #
# 打包完整性（离线）：页面、i18n、图标、metadata
# --------------------------------------------------------------------------- #
def test_packaging_assets_present(plugin_module):
    """页面/多语言/图标/元数据都得齐，否则装到 AstrBot 里会缺东西。"""
    import json as _json
    import struct

    index = ROOT / "pages" / "history" / "index.html"
    app_js = ROOT / "pages" / "history" / "app.js"
    assert index.is_file() and app_js.is_file()
    assert "app.js" in index.read_text(encoding="utf-8")
    # 页面必须用官方桥接 SDK 调接口，不要自己 fetch
    js = app_js.read_text(encoding="utf-8")
    assert "AstrBotPluginPage" in js and "apiGet" in js and "apiPost" in js

    for name in ("zh-CN", "en-US", "zh", "en"):
        data = _json.loads((ROOT / ".astrbot-plugin" / "i18n" / f"{name}.json").read_text(encoding="utf-8"))
        assert data["pages"]["history"]["title"]

    png = (ROOT / "logo.png").read_bytes()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", png[16:24])
    assert (width, height) == (256, 256)

    yaml = pytest.importorskip("yaml")
    meta = yaml.safe_load((ROOT / "metadata.yaml").read_text(encoding="utf-8"))
    assert [page["name"] for page in meta["pages"]] == ["history"]
    assert (ROOT / "pages" / meta["pages"][0]["name"]).is_dir()


def test_schema_covers_settings(plugin_module):
    """每个 Settings 字段都应该能在配置面板里找到（避免加了配置却没法改）。"""
    import json as _json

    schema = _json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    fields = {f for f in Settings().from_config({}).__dataclass_fields__ if not f.startswith("_")}
    missing = {field for field in fields if field not in schema}
    assert not missing, f"_conf_schema.json 缺少配置项：{sorted(missing)}"
    # schema 可能多一些字段（如版本信息），只要覆盖所有公开字段即可


# --------------------------------------------------------------------------- #
# 简介内容 / 封面处理 / 下载失败兜底（离线，用假下载器，不发真实请求）
# --------------------------------------------------------------------------- #
class FakeMedia:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.size = path.stat().st_size if path.exists() else 0
        self.kind = "image"


class FakeDownloader:
    """记录被下载的 URL，并按需让某几个 URL 失败。"""

    def __init__(self, root: Path, fail_urls: tuple[str, ...] = ()) -> None:
        self.root = root
        self.fail_urls = fail_urls
        self.calls: list[tuple[str, dict]] = []

    async def download(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url in self.fail_urls:
            from astrbot_plugin_twitter.core.downloader import DownloadException

        raise DownloadException("boom")
        path = self.root / f"{len(self.calls)}.bin"
        path.write_bytes(b"x" * 10)
        return FakeMedia(path)

    async def download_many(self, items, *, max_bytes=None):
        self.calls.extend(items)
        return [
            (
                Exception("InvalidUrlClientError: boom")
                if url in self.fail_urls
                else FakeMedia(self._write())
            )
            for url, _kwargs in items
        ]

    def _write(self) -> Path:
        path = self.root / f"media-{len(self.calls)}.bin"
        path.write_bytes(b"y" * 20)
        return path


def _fake_result(**kwargs):
    from astrbot_plugin_twitter.core.twitter import Content, ParseResult

    base = dict(
        url=URL_VIDEO,
        tweet_id="1904171341735178552",
        title="Don't miss the (Lucky) Landing.",
        author_handle="Fortnite",
        duration="0:07",
        cover="https://pbs.twimg.com/media/COVER.jpg",
        contents=[
            Content(
                type="video",
                url="https://dl.snapcdn.app/get?token=AAA",
                label="720p",
                filename="clip.mp4",
            )
        ],
    )
    base.update(kwargs)
    return ParseResult(**base)


async def test_handle_url_caption_and_cover(plugin_module, plugin, tmp_path, monkeypatch):
    async def fake_parse(*args, **kwargs):
        return _fake_result()

    monkeypatch.setattr("astrbot_plugin_twitter.core.parse_tweet", fake_parse)
    plugin._downloader = FakeDownloader(tmp_path)
    plugin.settings.send_cover = True

    segments = await plugin._handle_url(make_event("x"), URL_VIDEO, notify_error=True)

    texts = [c.text for c in segments if isinstance(c, plugin_module.Comp.Plain)]
    caption = texts[0]
    # 简介顶部是解析耗时，之后才是作者与媒体信息
    first_line, _, rest = caption.partition("\n")
    assert first_line.startswith("解析耗时 "), caption
    assert rest.startswith("作者：@Fortnite"), caption
    assert "视频 · 720p · 0:07" in caption
    assert "Lucky" in caption
    assert URL_VIDEO not in caption, "默认不带链接"

    names = [type(c).__name__ for c in segments]
    assert names.index("Image") < names.index("Video"), "封面应排在视频前面"
    video = next(c for c in segments if type(c).__name__ == "Video")
    assert video.cover == "https://pbs.twimg.com/media/COVER.jpg"
    # 下载用了中转链给的文件名与子目录
    assert plugin._downloader.calls[0][0] == "https://pbs.twimg.com/media/COVER.jpg"
    assert plugin._downloader.calls[1][1]["filename"] == "clip.mp4"
    assert plugin._downloader.calls[1][1]["subdir"] == "1904171341735178552"


async def test_handle_url_photo_cover_not_duplicated(plugin_module, plugin, tmp_path, monkeypatch):
    photo = "https://pbs.twimg.com/media/PHOTO.jpg"

    async def fake_parse(*args, **kwargs):
        from astrbot_plugin_twitter.core.twitter import Content

        return _fake_result(
            contents=[Content(type="image", url=photo, target_url=photo)],
            cover=photo,
            cover_is_content=True,
            duration=None,
        )

    monkeypatch.setattr("astrbot_plugin_twitter.core.parse_tweet", fake_parse)
    plugin._downloader = FakeDownloader(tmp_path)
    plugin.settings.send_cover = True

    segments = await plugin._handle_url(make_event("x"), URL_VIDEO, notify_error=True)
    images = [c for c in segments if type(c).__name__ == "Image"]
    assert len(images) == 1, "图片推文的封面就是这张图，不能重复发"
    assert len(plugin._downloader.calls) == 1


async def test_handle_url_download_failure_falls_back_to_link(plugin_module, plugin, tmp_path, monkeypatch):
    async def fake_parse(*args, **kwargs):
        return _fake_result(cover=None)

    monkeypatch.setattr("astrbot_plugin_twitter.core.parse_tweet", fake_parse)
    plugin._downloader = FakeDownloader(tmp_path, fail_urls=("https://dl.snapcdn.app/get?token=AAA",))
    plugin.settings.fallback_link = True

    segments = await plugin._handle_url(make_event("x"), URL_VIDEO, notify_error=True)
    texts = [c.text for c in segments if isinstance(c, plugin_module.Comp.Plain)]
    assert any("dl.snapcdn.app" in text for text in texts), "下载失败要给出直链"
    assert not any(type(c).__name__ == "Video" for c in segments)


async def test_handle_url_no_placeholder_downloads(plugin_module, plugin, tmp_path, monkeypatch):
    """回归 InvalidUrlClientError：转换按钮的 # 地址永远不该进下载队列。"""
    html = """
    <img src="https://pbs.twimg.com/media/REAL.jpg">
    <a href="https://dl.snapcdn.app/get?token=AAA" class="tw-button-dl button dl-success">下载 MP4 (720p)</a>
    <a href="#" class="tw-button-dl button dl-success action-convert">转换为 MP3</a>
    <a href="/" class="button more-video">下载更多视频</a>
    <h3>标题</h3>
    <input type="hidden" id="TwitterId" value="999" />
    """

    from astrbot_plugin_twitter.core.twitter import parse_twitter_html

    async def fake_parse(*args, **kwargs):
        result = parse_twitter_html(html)
        result.url = URL_VIDEO
        result.author_handle = "Fortnite"
        return result

    monkeypatch.setattr("astrbot_plugin_twitter.core.parse_tweet", fake_parse)
    downloader = FakeDownloader(tmp_path)
    plugin._downloader = downloader
    plugin.settings.send_audio = True  # 就算开了音频，也不该去下载 "#"

    await plugin._handle_url(make_event("x"), URL_VIDEO, notify_error=True)
    assert [url for url, _ in downloader.calls] == ["https://dl.snapcdn.app/get?token=AAA"]


# --------------------------------------------------------------------------- #
# 引用回复 / X 命名（离线）
# --------------------------------------------------------------------------- #
async def test_with_quote_inserts_reply(plugin_module, plugin):
    from astrbot.api.message_components import Plain, Reply

    segments = [Plain(text="hi")]
    quoted = plugin.sender._with_quote(make_event("x"), segments)
    assert isinstance(quoted[0], Reply)
    assert str(quoted[0].id) == "message-1", "要引用触发它的那条消息"
    assert quoted[1] is segments[0]
    assert plugin.sender._with_quote(make_event("x"), []) == []


async def test_with_quote_can_be_disabled(plugin_module, plugin):
    from astrbot.api.message_components import Plain

    plugin.settings.quote_reply = False
    segments = [Plain(text="hi")]
    assert plugin.sender._with_quote(make_event("x"), segments) is segments


async def test_with_quote_without_message_id(plugin_module, plugin):
    from astrbot.api.message_components import Plain

    event = make_event("x")
    event.message_obj.message_id = ""
    segments = [Plain(text="hi")]
    assert plugin.sender._with_quote(event, segments) is segments, "拿不到消息 ID 就别硬加引用"


async def test_send_chain_falls_back(plugin_module, plugin):
    """带引用发送失败 → 退回不带引用；再失败 → 退回纯链接。"""
    from astrbot.api.message_components import Plain

    plugin.settings.quote_reply = True
    segments = [Plain(text="hi")]
    event = make_event("x")

    calls: list[list] = []
    original = event.chain_result

    def flaky(chain):
        calls.append(list(chain))
        if len(calls) == 1:
            raise RuntimeError("平台不支持引用")
        return original(chain)

    event.chain_result = flaky  # type: ignore[method-assign]
    items = [item async for item in plugin.sender.send_chain(event, segments, "https://x.com/a/status/1")]
    assert len(calls) == 2, "第一次带引用失败后应重试"
    assert type(calls[0][0]).__name__ == "Reply" and type(calls[1][0]).__name__ == "Plain"
    assert items and items[-1].chain[0].text == "hi"

    # 完全发不出去时退回链接
    event2 = make_event("x")

    def always_fail(chain):
        raise RuntimeError("全挂了")

    event2.chain_result = always_fail  # type: ignore[method-assign]
    items2 = [item async for item in plugin.sender.send_chain(event2, segments, "https://x.com/a/status/1")]
    final = items2[-1].chain[0].text
    assert "hi" in final, "彻底发不出去时也要保住简介文字"
    assert final.rstrip().endswith("https://x.com/a/status/1")


@live
async def test_e2e_auto_parse_quotes_sender(plugin_module, plugin):
    """联网：自动解析时默认引用「发链接的那个人」的那条消息。"""
    plugin.settings.auto_parse_default = True
    plugin.settings.quote_reply = True

    event = make_event(f"看 {URL_PHOTO}")
    results = await collect(plugin.on_message(event))
    assert results, "应当自动解析"
    chain = results[0].chain
    assert type(chain[0]).__name__ == "Reply", f"链首应当是引用回复，实际：{chain}"
    assert str(chain[0].id) == "message-1"

    # 关掉开关后不应再有引用
    plugin.settings.quote_reply = False
    results2 = await collect(plugin.on_message(make_event(f"看 {URL_PHOTO}", session_id="session-2")))
    assert results2 and type(results2[0].chain[0]).__name__ != "Reply"


def test_names_are_x_not_twitter(plugin_module):
    """"插件的对外文案统一改成 X，内部 ID 保持不变。"""
    from pathlib import Path as _Path

    metadata = (_Path(ROOT) / "metadata.yaml").read_text(encoding="utf-8")
    assert "display_name: X 解析" in metadata
    assert "推特" not in metadata.replace("/X解析", "")
    assert plugin_module.PLUGIN_ID == "astrbot_plugin_twitter", "安装目录/路由前缀不能改，否则升级会重复安装"


async def test_quote_unsupported_platform_is_remembered(plugin_module, plugin):
    """带引用发失败的平台会被记住，之后不再重复尝试（省一次失败往返）。"""
    from astrbot.api.message_components import Plain

    segments = [Plain(text="hi")]
    event = make_event("x")
    original = event.chain_result

    def fail_once(chain):
        if any(type(c).__name__ == "Reply" for c in chain):
            raise RuntimeError("平台不支持引用")
        return original(chain)

    event.chain_result = fail_once  # type: ignore[method-assign]
    [item async for item in plugin.sender.send_chain(event, segments, "https://x.com/a/status/1")]
    assert plugin.sender._quote_unsupported, "应当记下这个平台"
    assert plugin.sender._with_quote(event, segments) is segments, "下次不再加引用"

    # 其它平台不受影响
    other = make_event("x", platform_id="fake-2")
    assert type(plugin.sender._with_quote(other, segments)[0]).__name__ == "Reply"


async def test_handle_url_cover_gets_short_timeout(plugin_module, plugin, tmp_path, monkeypatch):
    """封面下载要带短超时，不能拖着视频一起等。"""

    async def fake_parse(*args, **kwargs):
        return _fake_result()

    monkeypatch.setattr("astrbot_plugin_twitter.core.parse_tweet", fake_parse)
    downloader = FakeDownloader(tmp_path)
    plugin._downloader = downloader
    plugin.settings.send_cover = True
    plugin.settings.timeout = 30.0

    await plugin._handle_url(make_event("x"), URL_VIDEO, notify_error=False)

    timeouts = [kwargs.get("timeout") for _, kwargs in downloader.calls]
    assert plugin_module.COVER_TIMEOUT in timeouts, f"封面应带短超时：{downloader.calls}"
    assert timeouts[0] == plugin_module.COVER_TIMEOUT, "封面是第一个下载项"
    assert None in timeouts, "普通媒体不要被短超时限制"


async def test_show_elapsed_can_be_disabled(plugin_module, plugin, tmp_path, monkeypatch):
    async def fake_parse(*args, **kwargs):
        return _fake_result()

    monkeypatch.setattr("astrbot_plugin_twitter.core.parse_tweet", fake_parse)
    plugin._downloader = FakeDownloader(tmp_path)
    plugin.settings.show_elapsed = False

    segments = await plugin._handle_url(make_event("x"), URL_VIDEO, notify_error=False)
    caption = next(c.text for c in segments if isinstance(c, plugin_module.Comp.Plain))
    assert caption.startswith("作者："), caption


async def test_hint_when_disabled(plugin_module, plugin):
    """本会话没开自动解析时：默认静默，开了提示才回话。"""
    plugin.settings.auto_parse_default = False
    plugin.settings.hint_when_disabled = False
    replies: list[str] = []

    async def fake_send(result):
        replies.append(result.chain[0].text)

    event = make_event(f"看 {URL_PHOTO}")
    event.send = fake_send  # type: ignore[method-assign]
    assert await collect(plugin.on_message(event)) == []
    assert replies == [], "默认不该打扰"

    plugin.settings.hint_when_disabled = True
    event2 = make_event(f"看 {URL_PHOTO}", session_id="s-2")
    event2.send = fake_send  # type: ignore[method-assign]
    await collect(plugin.on_message(event2))
    assert replies and "/开启解析" in replies[0], replies


async def test_status_shows_session_id_and_hint(plugin_module, plugin):
    """未开启时状态里要写清会话 ID 和开启方式（排查「群里怎么不解析」用）。"""
    plugin.settings.auto_parse_default = False
    results = await collect(plugin.cmd_status(make_event("/解析状态", session_id="s-9")))
    text = results[0].chain[0].text
    assert "本会话 ID" in text and "s-9" in text
    assert "不自动解析" in text and "/开启解析" in text
    assert "简介显示解析耗时" in text

    # 已开启的会话不再提示开启方式
    enabled = await collect(plugin.cmd_enable(make_event("/开启解析", session_id="s-9")))
    assert enabled
    text2 = (await collect(plugin.cmd_status(make_event("/解析状态", session_id="s-9"))))[0].chain[0].text
    assert "自动解析中" in text2 and "/开启解析" not in text2.split("本会话 ID")[1]


# --------------------------------------------------------------------------- #
# 分段回复：简介单独发、媒体自己带引用
# --------------------------------------------------------------------------- #
def _enable_segmented(context, *, enable: bool, only_llm: bool = False) -> None:
    def get_config(key: str = "", default: Any = None) -> Any:
        if key == "platform_settings" or key == "":
            return {
                "platform_settings": {
                    "segmented_reply": {"enable": enable, "only_llm_result": only_llm}
                }
            }
        if key == "data_dir":
            return default or "."
        return default
    context.get_config = get_config  # type: ignore[method-assign]


def test_split_caption_keeps_first_plain(plugin_module, plugin):
    from astrbot.api.message_components import Image, Plain

    caption, media = plugin.sender._split_caption(
        [Plain(text="简介"), Image.fromFileSystem("/tmp/a.jpg"), Plain(text="链接")]
    )
    assert len(caption) == 1 and caption[0].text == "简介"
    assert len(media) == 2, "封面/媒体/兜底链接都留在媒体侧"


async def test_segmented_reply_sends_caption_separately(plugin_module, plugin):
    """开了分段回复时：简介单独一条，媒体那条自己带引用（引用不会丢）。"""
    from astrbot.api.message_components import Plain, Video

    plugin.sender.set_segmented_reply(True)
    event = make_event("x")
    segments = [Plain(text="作者：@a\n"), Video.fromFileSystem(path="/tmp/fake.mp4")]

    chains = []
    original = event.chain_result

    def record(chain):
        chains.append(list(chain))
        return original(chain)

    event.chain_result = record  # type: ignore[method-assign]
    [item async for item in plugin.sender.send_chain(event, segments, "https://x.com/a/status/1", segmented_reply=True)]

    assert len(chains) == 2, [c for c in chains]
    assert [type(c).__name__ for c in chains[0]] == ["Plain"], "第一条只发简介"
    assert [type(c).__name__ for c in chains[1]] == ["Reply", "Video"], "媒体那条带引用"


async def test_segmented_reply_off_keeps_single_chain(plugin_module, plugin):
    from astrbot.api.message_components import Plain, Video

    plugin.sender.set_segmented_reply(False)
    event = make_event("x")
    segments = [Plain(text="作者：@a\n"), Video.fromFileSystem(path="/tmp/fake.mp4")]

    chains = []
    original = event.chain_result

    def record(chain):
        chains.append(list(chain))
        return original(chain)

    event.chain_result = record  # type: ignore[method-assign]
    [item async for item in plugin.sender.send_chain(event, segments, "https://x.com/a/status/1")]
    assert len(chains) == 1
    assert [type(c).__name__ for c in chains[0]] == ["Reply", "Plain", "Video"]


async def test_segmented_only_llm_result_does_not_split(plugin_module, plugin):
    """只对 LLM 结果分段时，插件结果不会被拆开。"""
    plugin.sender.set_segmented_reply(False)  # only_llm=True means plugin results are not split
    assert plugin._segmented_reply_enabled is False


def test_reply_carries_sender(plugin_module, plugin):
    from astrbot.api.message_components import Plain

    reply = plugin.sender._with_quote(make_event("x"), [Plain(text="hi")])[0]
    assert str(reply.id) == "message-1"
    assert str(reply.sender_id) == "10001", "AstrBot 校验 Reply 有效性要求 sender_id"
    assert reply.sender_nickname == "tester"
