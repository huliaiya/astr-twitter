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
from astrbot.core.star.star_handler import star_handlers_registry  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
URL_PHOTO = "https://x.com/Fortnite/status/1870484479980052921"
URL_GIF = "https://x.com/Dithmenos9/status/1966798448499286345"

live = pytest.mark.skipif(
    os.environ.get("ASTR_TWITTER_SKIP_LIVE") == "1",
    reason="ASTR_TWITTER_SKIP_LIVE=1，跳过联网用例",
)


# --------------------------------------------------------------------------- #
# 加载插件（相对导入需要按「包」的方式加载）
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="session")
def plugin_module():
    name = "astr_twitter_plugin"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name,
        ROOT / "main.py",
        submodule_search_locations=[str(ROOT)],
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def make_event(
    text: str,
    *,
    sender_id: str = "10001",
    self_id: str = "99999",
    session_id: str = "session-1",
    extra_components: list | None = None,
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
        platform_meta=PlatformMetadata("fake", "fake platform", "fake-1"),
        session_id=session_id,
    )


class FakeContext:
    """只用到 get_config，其余交给真实 Star。"""

    def get_config(self):
        return None


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
    instance = plugin_module.TwitterPlugin(context=FakeContext(), config=config)
    # 数据目录固定在 tmp_path 下，避免污染真实 AstrBot 目录
    instance._downloader = plugin_module.Downloader(
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
        assert module.Downloader is not None
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
        ("/推特解析 https://x.com/a/status/1", True),
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
