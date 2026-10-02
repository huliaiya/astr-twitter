# -*- coding: utf-8 -*-
"""回归测试：严格按 AstrBot 的真实方式加载插件。

AstrBot 的加载流程（astrbot/core/star/star_manager.py）：
  1. 在 ``data/plugins/<dir>/`` 下寻找 ``main.py``（找不到才退回 ``<dir>.py``）；
  2. ``__import__("data.plugins.<dir>.main", fromlist=["main"])``；
  3. 用**加载路径**去查 handler：``star_handlers_registry``
     ``.get_handlers_by_module_name(metadata.module_path)``。

这里锁住两个曾经被一次「模块化重构」同时打破的不变式：

  A. ``main.py`` 内部只能使用**相对导入**。
     AstrBot 不会把插件目录加进 ``sys.path``，所以 ``from astrbot_plugin_twitter
     import ...`` 这类绝对导入会直接让插件加载失败：
     ``No module named 'astrbot_plugin_twitter'``。

  B. 带 ``@filter`` 装饰器的插件类必须定义在 ``main.py`` 里。
     handler 归属是用 ``handler.__module__`` 与加载路径做**精确相等**比较的
     （``get_handlers_by_module_name`` 里是 ``==``），把类放进子模块会让插件
     「加载成功但所有监听与指令全部失效」。
"""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

import pytest

pytest.importorskip("astrbot", reason="需要安装 AstrBot")

ROOT = Path(__file__).resolve().parents[1]

# 与 metadata.yaml 的 name / 实际安装目录保持一致
PLUGIN_DIR_NAME = "astrbot_plugin_twitter"
MODULE_PATH = f"data.plugins.{PLUGIN_DIR_NAME}.main"

EXPECTED_HANDLERS = (
    "on_message",
    "cmd_parse",
    "cmd_enable",
    "cmd_disable",
    "cmd_status",
    "llm_parse_twitter_link",
)


@pytest.fixture(scope="module")
def loaded(tmp_path_factory):
    """在临时目录复刻 ``data/plugins/<name>/`` 结构，再按 AstrBot 的方式导入。"""
    shim = tmp_path_factory.mktemp("astrbot-plugins")
    plugin_dir = shim / "data" / "plugins" / PLUGIN_DIR_NAME
    plugin_dir.parent.mkdir(parents=True)
    plugin_dir.symlink_to(ROOT, target_is_directory=True)

    inserted = str(shim)
    sys.path.insert(0, inserted)
    before = {key for key in sys.modules if key.startswith("data.plugins")}
    try:
        module = importlib.import_module(MODULE_PATH)
        yield module
    finally:
        # 相对导入会往 sys.modules 里塞一串 data.plugins.<name>.*，用完清掉
        for key in [k for k in sys.modules if k.startswith("data.plugins") and k not in before]:
            sys.modules.pop(key, None)
        if inserted in sys.path:
            sys.path.remove(inserted)


# --------------------------------------------------------------------------- #
# A. 以 AstrBot 的真实路径导入必须成功
# --------------------------------------------------------------------------- #
def test_plugin_importable_at_astrbot_path(loaded):
    """按 data.plugins.<name>.main 导入必须成功（相对导入没写错）。"""
    assert loaded.TwitterPlugin is not None


def test_subpackage_resolves_inside_plugin_package(loaded):
    """子包必须解析到插件包内部，而不是顶层同名模块。"""
    assert loaded.Downloader.__module__.startswith(f"data.plugins.{PLUGIN_DIR_NAME}.")
    assert loaded.HistoryRecord.__module__.startswith(f"data.plugins.{PLUGIN_DIR_NAME}.")


# --------------------------------------------------------------------------- #
# B. handler 必须归属到加载路径（否则插件"加载成功但不工作"）
# --------------------------------------------------------------------------- #
def test_plugin_class_module_equals_loader_path(loaded):
    """插件类的 __module__ 必须等于 AstrBot 的加载路径。

    star_manager 会把 ``metadata.module_path = path`` 并以该路径精确匹配 handler；
    ``Star.__init_subclass__`` 又用 ``cls.__module__`` 做 star_map 的键。两者必须一致。
    """
    assert loaded.TwitterPlugin.__module__ == MODULE_PATH


def test_handlers_are_bound_to_loader_path(loaded):
    """所有 handler 都必须能被加载路径查到（精确相等匹配）。"""
    from astrbot.core.star.star_handler import star_handlers_registry

    found = {
        md.handler_name
        for md in star_handlers_registry.get_handlers_by_module_name(MODULE_PATH)
    }
    missing = [name for name in EXPECTED_HANDLERS if name not in found]
    assert not missing, (
        f"以下 handler 未能归属到 {MODULE_PATH}：{missing}。"
        "插件类必须定义在 main.py 里，而不是子模块。"
    )


# --------------------------------------------------------------------------- #
# C. 静态检查：把上面两个不变式钉死在源码层面（不依赖 AstrBot 也能跑）
# --------------------------------------------------------------------------- #
def _main_source() -> str:
    return (ROOT / "main.py").read_text(encoding="utf-8")


def test_main_py_exists():
    """AstrBot 只认 main.py 或 <目录名>.py，别把入口挪走。"""
    assert (ROOT / "main.py").is_file()


@pytest.mark.parametrize(
    "banned",
    [
        "from astrbot_plugin_twitter",
        "import astrbot_plugin_twitter",
        "from twitter_core",
        "import twitter_core",
        "from core",
        "import core",
    ],
)
def test_main_has_no_absolute_self_imports(banned):
    """main.py 里不允许出现指向插件自身的绝对导入。

    这些在测试里（仓库根目录被塞进 sys.path）能跑通，但在 AstrBot 里必然
    加载失败 —— 正是这次线上故障的根因。
    """
    offenders = [
        line.strip()
        for line in _main_source().splitlines()
        # 只匹配行首的 import 语句，避免误伤注释/字符串
        if line.lstrip().startswith(banned)
    ]
    assert not offenders, (
        f"main.py 不能使用绝对导入 {banned!r}（AstrBot 不会把插件目录加进 sys.path）：{offenders}"
    )


def test_plugin_class_is_defined_in_main():
    """带 @filter 的插件类必须直接定义在 main.py 的模块顶层。"""
    tree = ast.parse(_main_source())
    top_level_classes = {node.name for node in tree.body if isinstance(node, ast.ClassDef)}
    assert "TwitterPlugin" in top_level_classes, (
        "TwitterPlugin 必须定义在 main.py 里 —— handler 归属按 __module__ 精确匹配，"
        "放到子模块会导致插件加载成功但所有功能失效。"
    )


def test_plugin_class_subclasses_star():
    """确认入口类确实继承了 AstrBot 的 Star。"""
    tree = ast.parse(_main_source())
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "TwitterPlugin":
            bases = [ast.unparse(base) for base in node.bases]
            assert any("Star" in base for base in bases), f"TwitterPlugin 应当继承 Star，实际基类：{bases}"
            return
    pytest.fail("main.py 中未找到 TwitterPlugin 类")
