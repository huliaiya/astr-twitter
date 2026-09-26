# astr-twitter · AstrBot 推特解析插件

[![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.9.2-orange)](https://github.com/AstrBotDevs/AstrBot)
[![License](https://img.shields.io/badge/License-MIT-green)](./LICENSE)

在 AstrBot 里自动解析 **X / Twitter** 链接，把推文里的**视频、图片、GIF**直接发到会话。

消息里出现 `x.com/.../status/...` 或 `twitter.com/.../status/...` 就会自动触发，也可以手动 `/解析 <链接>`。
解析与发送逻辑抽取自 [`Zhalslar/astrbot_plugin_parser`](https://github.com/Zhalslar/astrbot_plugin_parser)
的 `core/parsers/twitter.py`，去掉了对 AstrBot 的硬耦合后重新组织为独立插件。

---

## ✨ 功能

- **自动解析**：消息（含卡片/Json 组件）里的推特链接自动解析并发送媒体
- **手动解析**：`/解析 <链接>`，别名 `/推特解析`、`/tw`、`/x解析`
- **视频 / 图片 / GIF**：视频多清晰度自动取第一个（720p）；GIF 以 mp4 形式发送
- **会话开关**：管理员可用 `/开启解析`、`/关闭解析` 按会话控制，持久化保存
- **防抖**：同一条推文在窗口期内只解析一次，避免刷屏
- **LLM Tool**：注册 `parse_twitter_link`，模型可在对话中按需调用
- **可配置**：接口地址、Cookie、代理、超时、重试、媒体数量/大小上限等都在 WebUI 配置面板里

## 📦 安装

**方式一：AstrBot 插件市场 / 仓库安装**

在 WebUI 的「插件」页安装本仓库：

```
https://github.com/huliaiya/astr-twitter
```

**方式二：手动 clone**

```bash
cd AstrBot/data/plugins
git clone https://github.com/huliaiya/astr-twitter
```

AstrBot 会按 `metadata.yaml` 里的 `name`（`astrbot_plugin_twitter`）作为插件目录名与 import 路径，
仓库目录名带连字符 `astr-twitter` 不影响加载。安装后在插件页点「重载插件」即可。

依赖只有 `beautifulsoup4`（见 `requirements.txt`），`aiohttp` 由 AstrBot 本体提供。

## 🗂 目录结构

```
astr-twitter/
├─ main.py                # 插件入口：Star 子类、事件监听、指令、LLM Tool
├─ metadata.yaml          # 插件元数据（name 必须是合法 Python 标识符）
├─ _conf_schema.json      # WebUI 配置面板
├─ requirements.txt       # 依赖：beautifulsoup4
├─ core/
│  ├─ twitter.py          # 解析核心（不依赖 AstrBot，可独立测试）
│  └─ downloader.py       # 媒体下载 + 文件头校验
├─ tests/                 # pytest：离线用例 + 联网用例 + 真实 AstrBot 集成测试
└─ devtools/              # 开发工具：Python CLI、Node 版实现、离线 fixture
```

## 💬 指令

| 指令 | 权限 | 说明 |
| --- | --- | --- |
| `/解析 <链接>` | 所有人 | 手动解析（别名：`/推特解析`、`/tw`、`/x解析`） |
| `/开启解析` | 管理员 | 开启当前会话的自动解析 |
| `/关闭解析` | 管理员 | 关闭当前会话的自动解析 |
| `/解析状态` | 所有人 | 查看插件与本会话状态 |

## ⚙️ 配置（WebUI → 插件 → 推特解析）

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 插件总开关 |
| `auto_parse` | `true` | 自动解析消息中的链接 |
| `interrupt_event` | `true` | 解析后终止事件传播，避免同一条消息再触发一次 LLM 回复 |
| `notify_error` | `false` | 失败时是否回复原因（默认只写日志，不打扰群聊） |
| `max_links` | `3` | 单条消息最多解析链接数 |
| `max_media` | `9` | 单条推文最多发送媒体数 |
| `max_video_mb` | `100` | 媒体大小上限 |
| `send_title` | `true` | 是否附带推文正文 |
| `send_cover` | `false` | 是否额外发送封面 |
| `keep_files` | `false` | 是否保留已下载文件（默认发送后删除） |
| `debounce_seconds` | `300` | 同一推文防抖窗口，`0` 关闭 |
| `api_endpoint` | `https://xdown.app/api/ajaxSearch` | 解析接口地址 |
| `api_origin` | `https://xdown.app` | 接口 Origin/Referer（有校验，一般不改） |
| `cookie` | 空 | 接口 Cookie（风控时使用，面板里以密码框显示） |
| `proxy` | 空 | `http://127.0.0.1:7890` 之类，用于接口请求与媒体下载 |
| `timeout` / `retry` | `20.0` / `2` | 接口超时与重试次数 |

## 🔧 工作原理

```
消息事件
  └─ 收集文本（message_str + Json 卡片 + url 字段）
      └─ 正则匹配 status 链接（x.com / twitter.com，与原插件正则一致）
          └─ 防抖 / 会话开关 / 机器人自身消息过滤
              └─ POST xdown.app/api/ajaxSearch  (q=<url>&lang=zh-cn)
                  └─ 解析返回 HTML：第一个 <img> → 封面；
                     a.tw-button-dl / a.abutton → 视频 / 图片 / gif；第一个 <h3> → 正文
                      └─ 下载媒体（校验文件头，落地到 data/plugin_data/<plugin>/twitter/<推文ID>/）
                          └─ yield event.chain_result([...Video/Image...]) → 发送
```

## 🧪 测试

```bash
# 离线（URL 匹配、HTML fixture、文件头嗅探、参数解析、卡片提取…）
ASTR_TWITTER_SKIP_LIVE=1 pytest

# 全部（含真实接口解析、真实下载、真实 AstrBot 集成）
pytest
```

本仓库的测试情况（AstrBot 4.28.1 + Python 3.12，实测）：

```
44 passed in 76.06s
```

- **离线**：URL 匹配 10 项（含 `www.` / `mobile.` / `x.com/i/web/status/` / 夹带参数 / 反例）、
  3 份 xdown HTML fixture 的解析断言、文件头嗅探 6 项、配置与指令判定、卡片链接提取、
  以及**按 AstrBot 真实导入路径 `data.plugins.astrbot_plugin_twitter.main` 加载**的保真测试。
- **联网**：真实推文的视频 / 单图 / 多图 / GIF / `twitter.com` 域名解析；不存在的推文正确抛 `ParseException`。
- **集成**：用真实 AstrBot 的事件与消息组件跑完整链路（消息 → 解析 → 下载 → `chain_result`），
  断言消息链里出现 `Image` / `Video` 组件、文件真实存在且体积合理、`event.stop_event()` 被调用；
  同时断言 6 个 handler 与 `parse_twitter_link` 这个 LLM Tool 已成功注册。

命令行调试（不需要 AstrBot）：

```bash
python devtools/twitter_cli.py https://x.com/Fortnite/status/1870484479980052921
python devtools/twitter_cli.py "看这个 https://x.com/Dithmenos9/status/1966798448499286345" --download out
```

## ⚠️ 已知限制

1. **依赖第三方接口 `xdown.app`**（原插件同样依赖）。接口改版或限流时需要更新 `api_endpoint`，
   或等待上游修复。
2. **`pbs.twimg.com` 直链在部分服务器不可达**（封面是直链）。此时视频/图片经 `dl.snapcdn.app`
   中转仍可正常下载，只有 `send_cover` 开封面时可能失败——日志里会有提示，不影响主流程。
3. **GIF 以 mp4 形式发送**（接口给的就是 mp4 容器）。
4. 暂不支持**转发推文（repost）**与**转换为 MP3**（接口有该按钮，原插件也未处理）。
5. 视频类消息并非所有平台都能发送；失败时会降级为发送原链接。
6. 多清晰度只取第一个 MP4（与原插件一致，通常是 720p）。

## 📄 来源与致谢

- 解析逻辑抽取自 [`Zhalslar/astrbot_plugin_parser`](https://github.com/Zhalslar/astrbot_plugin_parser)（MIT）
- 该项目的核心又来自 [`fllesser/nonebot-plugin-parser`](https://github.com/fllesser/nonebot-plugin-parser)
- 测试用的真实推文链接取自 `nonebot-plugin-parser` 的测试套件

本项目以 MIT 协议发布，遵循原项目的许可与署名要求。
