# astr-twitter · AstrBot 推特解析插件

[![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.9.2-orange)](https://github.com/AstrBotDevs/AstrBot)
[![License](https://img.shields.io/badge/License-MIT-green)](./LICENSE)

在 AstrBot 里自动解析 **X / Twitter** 链接，把推文里的**视频、图片、GIF**直接发到会话。

消息里出现 `x.com/.../status/...` 或 `twitter.com/.../status/...` 就会自动触发（默认需要先开启），也可以手动 `/解析 <链接>`。
解析与发送逻辑抽取自 [`Zhalslar/astrbot_plugin_parser`](https://github.com/Zhalslar/astrbot_plugin_parser)
的 `core/parsers/twitter.py`，去掉硬耦合后重新组织为独立插件。

---

## ✨ 功能

- **自动解析（按需开启）**：默认不打扰任何会话，管理员发一次 `/开启解析`，之后**该会话**里出现推特链接就自动解析（含卡片/Json 组件里的链接）
- **触发方式可选**：`all`（有链接就解析）/ `at`（需要 @机器人）/ `command_only`（只用指令）
- **手动解析**：`/解析 <链接>`，别名 `/推特解析`、`/tw`、`/x解析`
- **视频 / 图片 / GIF / 音频**：视频自动取第一个 MP4（多为 720p）；GIF 以 mp4 发送；接口返回 MP3 时可选择以语音发送
- **引用/转发**：正文里的 `RT @user:` 自动清洗并标注「🔁 转发自」；可选 `parse_quoted` 再解析一层被引用的原推
- **失败兜底**：xdown 接口失败时自动改用 Twitter 官方 syndication 接口；媒体下载失败/超限时把直链作为文本发出
- **并发下载 + 同链接缓存**：多图推文并行下载（并发可配），同一 URL 进程内只下一次
- **会话 / 全局开关**：`/开启解析`、`/关闭解析` 作用于当前会话；加 `全局` 一次作用于所有会话；状态持久化
- **解析历史**：`/解析历史` 指令 + WebUI 插件页面「推特解析历史」（成功率、媒体数、流量、常解析作者，可一键清空）
- **防抖**：同一条推文在窗口期内只解析一次，避免刷屏
- **LLM Tool**：注册 `parse_twitter_link`，模型可在对话中按需调用
- **多语言**：`.astrbot-plugin/i18n/` 提供 zh-CN / en-US

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

## 🚀 快速开始

自动解析**默认是关的**（`auto_parse_default: false`），需要先开一次，之后才自动解析：

```
你（管理员）：/开启解析
机器人：已开启本会话的推特自动解析 ✅
        之后本会话里发推特链接就会自动解析。

你：https://x.com/Fortnite/status/1870484479980052921
机器人：[推文正文] + [图片/视频]
```

想让**所有会话**都自动解析（不用一个个群去开）：

```
/开启解析 全局      # 所有会话自动解析
/关闭解析 全局      # 所有会话都不自动解析
/解析状态           # 看当前生效的开关与来源
/解析历史 5         # 看最近 5 条解析记录（管理员）
```

不想用指令、希望装完就直接全局自动解析？把配置里的 **`auto_parse_default` 打开**即可。
任何时候都能用 `/解析 <链接>` 手动解析，不受会话开关影响。

## 🗂 目录结构

```
astr-twitter/
├─ main.py                      # 插件入口：Star 子类、事件监听、指令、LLM Tool、插件 Web API
├─ metadata.yaml                # 插件元数据（name 必须是合法 Python 标识符）
├─ _conf_schema.json            # WebUI 配置面板（24 项）
├─ requirements.txt             # 依赖：beautifulsoup4
├─ logo.png                     # 插件图标（由 devtools/make_logo.py 生成）
├─ core/
│  ├─ twitter.py                # 解析核心（xdown 通道，不依赖 AstrBot）
│  ├─ syndication.py            # 官方 syndication 后备通道（含 JS 一致的 token 算法）
│  ├─ downloader.py             # 媒体下载：流式限长 + 并发 + 缓存
│  └─ history.py                # 解析历史存储（JSON，带上限与统计）
├─ pages/history/               # WebUI 插件页面（历史与统计）
├─ .astrbot-plugin/i18n/        # zh-CN / en-US 文案
├─ tests/                       # pytest：离线 + 联网 + 真实 AstrBot 集成
└─ devtools/                    # 开发工具：Python CLI、Node 实现、离线 fixture、图标生成
```

## 💬 指令

| 指令 | 权限 | 说明 |
| --- | --- | --- |
| `/解析 <链接>` | 所有人 | 手动解析，任何时候都可用（别名：`/推特解析`、`/tw`、`/x解析`） |
| `/开启解析` | 管理员 | 开启**当前会话**的自动解析，之后本会话发链接就会自动解析 |
| `/开启解析 全局` | 管理员 | 开启**所有会话**的自动解析 |
| `/关闭解析` | 管理员 | 关闭**当前会话**的自动解析 |
| `/关闭解析 全局` | 管理员 | 关闭**所有会话**的自动解析 |
| `/解析状态` | 所有人 | 查看总开关、触发方式、全局开关、默认策略、本会话状态及生效来源 |
| `/解析历史 [条数]` | 管理员 | 查看最近的解析记录（含失败原因） |

开关优先级：**本会话显式设置 > 全局开关 > 配置里的 `auto_parse_default`**。
所以「全局打开了，但某个群用 `/关闭解析` 单独关掉」是生效的。

## ⚙️ 配置（WebUI → 插件 → 推特解析）

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 插件总开关 |
| `auto_parse` | `true` | 自动解析功能总开关（关闭后所有会话都不自动解析，手动 `/解析` 仍可用） |
| `auto_parse_default` | `false` | 新会话默认是否自动解析。**建议保持关闭**，用 `/开启解析` 按需开 |
| `trigger_mode` | `all` | `all` 有链接就解析 / `at` 需要 @机器人 / `command_only` 只用指令 |
| `interrupt_event` | `true` | 解析后终止事件传播，避免同一条消息再触发一次 LLM 回复 |
| `notify_error` | `false` | 失败时是否回复原因（默认只写日志，不打扰群聊） |
| `max_links` | `3` | 单条消息最多解析链接数 |
| `max_media` | `9` | 单条推文最多发送媒体数 |
| `max_title_chars` | `300` | 正文最大长度，`0` 不限制 |
| `send_title` | `true` | 是否附带推文正文 |
| `send_cover` | `false` | 是否额外发送封面（封面直链是 `pbs.twimg.com`） |
| `send_audio` | `false` | 接口返回 MP3 时是否以语音消息发送 |
| `parse_quoted` | `false` | 是否再解析一层被引用/转发的原推 |
| `keep_files` | `false` | 是否保留已下载文件（默认发送后删除） |
| `fallback_link` | `true` | 下载失败/超限时把媒体直链作为文本发出 |
| `max_video_mb` | `100` | 媒体大小上限（流式下载，超限立即中断） |
| `download_concurrency` | `3` | 多图并行下载并发数（1-8） |
| `debounce_seconds` | `300` | 同一推文防抖窗口，`0` 关闭 |
| `history_enabled` | `true` | 是否记录解析历史 |
| `history_size` | `200` | 历史条数上限（10-2000） |
| `api_endpoint` | `https://xdown.app/api/ajaxSearch` | 主解析接口 |
| `api_origin` | `https://xdown.app` | 接口 Origin/Referer（有校验，一般不改） |
| `cookie` | 空 | **xdown.app 的** Cookie（不是推特账号凭证），风控时才需要填 |
| `proxy` | 空 | `http://127.0.0.1:7890` 之类，用于接口请求与媒体下载 |
| `timeout` / `retry` | `20.0` / `2` | 接口超时与重试次数 |
| `fallback_syndication` | `true` | xdown 失败时是否改用官方 syndication 后备接口 |

## 🖥 WebUI 插件页面

安装后，AstrBot WebUI 的插件页会多出一个「**推特解析历史**」页面（`pages/history/`）：

- 顶部统计卡：总解析 / 成功 / 失败 / 成功率 / 视频 / 图片 / GIF / 流量
- 列表：时间、成功与否、媒体类型与数量、推文链接与标题、文件大小；失败项直接显示原因
- 常解析作者 Top10、当前接口与代理状态
- 「清空」按钮一键清除历史

页面通过官方桥接 SDK（`window.AstrBotPluginPage.apiGet/apiPost`）调用插件自己注册的 Web API：
`GET /api/v1/plugins/extensions/astrbot_plugin_twitter/history`、
`POST .../history/clear`。**AstrBot 版本较老、没有插件 Web API 时插件会自动跳过注册**，
页面打不开但不影响解析功能（配置项 `history_enabled` 也可整体关闭历史）。

## 🔧 工作原理

```
消息事件
  └─ 收集文本（message_str + Json 卡片 + url 字段）
      └─ 正则匹配 status 链接（x.com / twitter.com，与原插件正则一致）
          └─ 机器人自身消息过滤
              └─ 触发方式：trigger_mode（all / at / command_only）
                  └─ 开关判定：本会话显式开关 > 全局开关 > 默认策略（默认关）
                      └─ 防抖
                          └─ POST xdown.app/api/ajaxSearch  (q=<url>&lang=zh-cn)
                              ├─ 成功：解析返回 HTML
                              │   第一个 <img> → 封面；a.tw-button-dl / a.abutton →
                              │   视频 / 图片 / gif / MP3；第一个 <h3> → 正文（清洗 RT 前缀）
                              └─ 失败且开了 fallback_syndication：
                                  GET cdn.syndication.twimg.com/tweet-result?id=..&token=..
                                  → 解析 mediaDetails / video_info.variants
                                  └─ 并发下载媒体（流式 + 限长 + 缓存，落地
                                     data/plugin_data/<plugin>/twitter/<推文ID>/）
                                      └─ 写解析历史（history.json）
                                          └─ yield event.chain_result([...Video/Image/Record...])
```

## 🧪 测试

```bash
# 离线（URL 匹配、HTML fixture、文件头、下载器、历史、参数、页面接口…）
ASTR_TWITTER_SKIP_LIVE=1 pytest

# 全部（含真实解析、真实下载、真实 AstrBot 集成）
pytest
```

本仓库的测试情况（AstrBot 4.28.1 + Python 3.12，实测）：

```
84 passed in 139.31s
```

- **离线（60+ 项）**：
  - URL 匹配 10 项（`www.` / `mobile.` / `x.com/i/web/status/` / 夹带参数 / 反例）、3 份 xdown HTML fixture 断言
  - 正文清洗（`RT @user:` → 转发标记、t.co 去链）、截断、引用链接识别、音频按钮解析
  - **syndication token 与本机 Node（V8 `toString(36)`）的 8 组结果逐一比对**，含边界 ID
  - syndication JSON → ParseResult 映射（最高码率 mp4、忽略 m3u8、gif、已带参数的图片 URL、空体报错）
  - 下载器：落盘与类型嗅探、**超限中断不留残文件**、并发 `download_many` 的单点失败隔离、同 URL 缓存命中
  - 历史：上限淘汰、统计（成功率/媒体计数/常解析作者）、损坏文件容错、清空
  - 插件：开关优先级、触发方式 `at`/`command_only`、`/解析历史`、插件 Web API 注册与响应体（用
    真实 `PluginRequest` 绑定上下文调用）、**按 AstrBot 真实导入路径 `data.plugins.astrbot_plugin_twitter.main` 加载**的保真测试
- **联网**：真实推文的视频 / 单图 / 多图 / GIF / `twitter.com` 域名解析；不存在的推文正确抛 `ParseException`
- **集成**：用真实 AstrBot 的事件与消息组件跑完整链路（消息 → 解析 → 下载 → `chain_result`），
  断言消息链里出现 `Image` / `Video` 组件、文件真实存在且体积合理、`event.stop_event()` 被调用；
  覆盖「先 `/开启解析` 再发链接自动解析」「@机器人 触发 + 写入历史」「引用原推跟随」等场景；
  并断言 7 个 handler 与 `parse_twitter_link` 这个 LLM Tool 已成功注册。

命令行调试（不需要 AstrBot）：

```bash
python devtools/twitter_cli.py https://x.com/Fortnite/status/1870484479980052921
python devtools/twitter_cli.py "看这个 https://x.com/Dithmenos9/status/1966798448499286345" --download out
python devtools/make_logo.py          # 重新生成 logo.png
```

## ⚠️ 已知限制

1. **主接口依赖第三方 `xdown.app`**（原插件同样依赖）。接口改版或限流时需要更新 `api_endpoint`，
   或依赖 syndication 后备通道。
2. **syndication 后备接口的联网行为未在本机验证**：本开发环境中 `cdn.syndication.twimg.com`
   与 `pbs.twimg.com` 同属被墙的 Twitter CDN，无法连通，所以后备通道只有**离线单测**
   （token 算法、JSON 映射）与实际环境验证的空白；它只在主接口失败时启用，失败也只是继续报错。
3. **`pbs.twimg.com` 直链在部分服务器不可达**。媒体经 `dl.snapcdn.app` 中转时正常，
   但 `send_cover` 与 syndication 直链可能失败——日志里有提示，可用 `proxy` 解决。
4. **GIF 以 mp4 形式发送**（接口给的就是 mp4 容器）。
5. **音频需要接口返回 MP3 按钮**；当前 xdown 对普通视频推文只给 MP4，所以 `send_audio`
   多数情况下不会触发（离线用例用构造的 HTML 覆盖了这条分支）。
6. 视频类消息并非所有平台都能发送；失败时会降级为发送原链接。
7. 多清晰度只取第一个 MP4（与原插件一致，通常是 720p）。
8. `logo.png` 由脚本程序化生成（纯标准库），风格朴素，可按需替换同名文件。

## 📄 来源与致谢

- 解析逻辑抽取自 [`Zhalslar/astrbot_plugin_parser`](https://github.com/Zhalslar/astrbot_plugin_parser)（MIT）
- 该项目的核心又来自 [`fllesser/nonebot-plugin-parser`](https://github.com/fllesser/nonebot-plugin-parser)
- 测试用的真实推文链接取自 `nonebot-plugin-parser` 的测试套件
- syndication token 算法参考推特前端 / `react-tweet` 的公开实现

本项目以 MIT 协议发布，遵循原项目的许可与署名要求。
