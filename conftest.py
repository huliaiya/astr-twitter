# -*- coding: utf-8 -*-
"""测试环境准备：

1. 把插件根目录放进 sys.path，使 `import core` 可用；
2. 在导入 AstrBot 之前把 ASTRBOT_ROOT 指到临时目录，
   避免 AstrBot 初始化时在仓库根目录创建 data/（cmd_config.json、data_v4.db 等）。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 必须在 import astrbot 之前设置
os.environ.setdefault("ASTRBOT_ROOT", tempfile.mkdtemp(prefix="astrbot-test-root-"))
