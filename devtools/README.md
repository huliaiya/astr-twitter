# devtools：独立版X解析器（开发与回归工具）

> 这个目录是**工具与回归测试**，不是插件本体。插件本体见仓库根目录的 `main.py` + `core/`。

从 [`Zhalslar/astrbot_plugin_parser`](https://github.com/Zhalslar/astrbot_plugin_parser) 的
`core/parsers/twitter.py` 中**单独抽出来的X解析器**，去掉了 AstrBot 的全部依赖
（`BaseParser` / `PluginConfig` / `Downloader` / `CookieJar`），可以脱离 AstrBot 独立运行。
当初先用它把解析链路跑通并实测，之后才封装成 AstrBot 插件，因此这里保留了离线 fixture 与 Node 版自测。

```
devtools/
├─ twitter-parser.mjs            # Node.js 零依赖实现（早期验证用，已实测 32/32）
├─ test-twitter.mjs              # Node 自测：URL 匹配 + 离线 HTML 解析 + 真实接口 + 真实下载
├─ package.json                  # 上面的 npm 脚本（零依赖）
├─ twitter_cli.py                # Python CLI，直接调用插件 core 层（推荐用这个调试）
├─ make_logo.py                  # 用标准库把官方 X 字形栅格化成 logo.png（可 --preview 预览）
├─ vendor/x-logo.svg             # 官方 X 标志矢量路径（simple-icons，来源留档）
├─ test-page.mjs                 # 页面自检：DOM 桩 + 桥接协议，验证加载顺序与响应解包
├─ bridge-stub.js                # 桥接 SDK 的协议桩（没有 AstrBot 时用它跑页面自检）
└─ fixtures/                     # 离线回归用 xdown 返回 HTML 快照（Node 与 pytest 共用）
```

Python 版 CLI（不需要 AstrBot，依赖 aiohttp + bs4）：

```bash
python devtools/twitter_cli.py https://x.com/Fortnite/status/1870484479980052921
python devtools/twitter_cli.py "看这个 https://x.com/Dithmenos9/status/1966798448499286345" --download out
```


## 原插件的解析链路（已 1:1 复刻）

| 原插件代码 | 作用 | 本实现 |
| --- | --- | --- |
| `@handle("x.com", regex)` / `@handle("twitter.com", regex)` | 匹配 status 链接 | `matchTweetUrl()`，正则**逐字符相同** |
| `BaseParser.search_url()` | 关键词 + 正则双匹配 | `matchTweetUrl()` 里的 `keyword not in text` 短路 |
| `TwitterParser.__init__` | 设置 `Origin/Referer: xdown.app` 等请求头 | `XDOWN_HEADERS` |
| `_req_xdown_api()` | `POST https://xdown.app/api/ajaxSearch`，`q=<url>&lang=zh-cn` | `reqXdownApi()`（加了重试） |
| `parse_twitter_html()` | bs4 抽 封面/视频/图片/gif/标题 | `parseXdownHtml()`（正则实现，语义对齐） |
| `create_video_content / create_image_contents / create_dynamic_contents` | 构造内容 | `contents[]`，顺序：视频 → 图片 → gif |
| `self.result(...)` | 统一 ParseResult | `parseTweet()` 返回值 |
| `Downloader.download_*` | 下载媒体 | `downloadMedia()`（含文件头校验） |

### 关键细节（保持与原插件一致）

- 请求头必须带 `Origin`/`Referer: https://xdown.app`，否则接口会拒。
- 视频有 720p/360p/270p 多个清晰度，原插件遇到**第一个**「下载 MP4」就 `break` → 实际取 720p，本实现同样 break。
- gif 走 `下载 gif` 分支 → 最终是 **mp4 容器**（不是 .gif 文件）。
- 图片推文的下载按钮 class 是 `abutton`；视频/gif 推文是 `tw-button-dl`，两者都要扫（原插件用 `chain()`，本实现先 tw-button-dl 后 abutton，顺序一致）。
- 封面 = 返回 HTML 里**第一个 `<img>`**；作者名原插件就是硬编码 `"无用户名"`。
- gif 推文里那条「下载图片」是缩略图，原插件也会当成一张图片加入内容，本实现保留。
- 接口返回 `status != "ok"` → `ParseException(接口 msg)`；`status == "ok"` 但 `data` 为空 → `ParseException("解析失败, 数据为空")`（与原插件行为相同）。

## 用法（Node，已实测）

```bash
# 只解析，输出 JSON
node devtools/twitter-parser.mjs https://x.com/Fortnite/status/1870484479980052921

# 从一段消息文本里提取链接
node devtools/twitter-parser.mjs "看这个 https://x.com/Dithmenos9/status/1966798448499286345"

# 解析并真实下载全部媒体（校验文件头）
node devtools/twitter-parser.mjs https://x.com/Fortnite/status/1904171341735178552 --download out
```

作为模块使用：

```js
import { parseTweet, matchTweetUrl, downloadMedia } from "./twitter-parser.mjs";

if (matchTweetUrl(messageStr)) {
  const r = await parseTweet(messageStr);
  console.log(r.title, r.counts);                 // { video: 1, image: 0, dynamic: 0 }
  for (const c of r.contents) await downloadMedia(c.url, "downloads");
}
```

测试：

```bash
node devtools/test-twitter.mjs            # 全部（含联网 + 真实下载）
node devtools/test-twitter.mjs --offline  # 只跑 URL 匹配 + 离线 HTML fixture
```

## 测试结果（本机实际跑出来）

```
通过 32 / 失败 0
```

- **URL 匹配**：x.com / www.x.com / twitter.com / mobile.twitter.com / `x.com/i/web/status/...` /
  文本中夹带链接与 `?s=46&t=...` 参数 → 全部匹配；`example.com`、`notx.com`、无 `status/<id>` → 全部拒绝。
- **离线 HTML 解析**：3 份 fixture（gif / video / photo）→ 封面、MP4、图片、gif、标题、tweet_id 抽取全部正确。
- **真实接口解析**（用上游 nonebot-plugin-parser 测试套件里的真实推文）：
  | 用例 | 链接 | 结果 |
  | --- | --- | --- |
  | 视频 | `x.com/Fortnite/status/1904171341735178552` | `{video:1}`，标题 `Don't miss the (Lucky) Landing...`，封面 OK |
  | 单图 | `x.com/Fortnite/status/1870484479980052921` | `{image:1}` |
  | 多图 | `x.com/chitose_yoshino/status/1841416254810378314` | `{image:3}` |
  | GIF | `x.com/Dithmenos9/status/1966798448499286345` | `{image:1, dynamic:1}` |
  | twitter.com 域名 | `twitter.com/Fortnite/status/1870484479980052921` | `{image:1}` |
  | 不存在的推文 | `x.com/NASA/status/1683502034445783040` | 正确抛 `ParseException` |
- **真实下载校验**（下载到 `downloads/`，按魔数判断类型）：
  - 视频 `1,857,890 bytes` → `ftyp` 魔数 = **mp4** ✅
  - 图片 `122,214 bytes` → `FF D8 FF` = **jpeg** ✅
  - GIF `141,146 bytes` → **mp4** 容器 ✅

复现命令与原始输出：

```bash
$ node devtools/test-twitter.mjs
...
=== 4. 真实下载校验 ===
  ✅ 视频可下载且是 mp4 — 1857890 bytes, kind=mp4
  ✅ 图片可下载且是图片 — 122214 bytes, kind=jpeg
  ✅ GIF 可下载且是视频容器 — 141146 bytes, kind=mp4
================ 结果 ================
通过 32 / 失败 0
```

## 已知限制 / 注意

1. **依赖第三方站 `xdown.app`**（原插件也是）：接口挂了/改版了，这里就一起失效。请求头里的
   `Origin`/`Referer` 不能删。存在限流可能，代码里加了 3 次重试 + 退避。
2. **`pbs.twimg.com` 直链在本机被网络策略拦截**（DNS 解析到 `128.121.243.228`，curl/Node 均连接超时），
   所以 CLI 里下载**封面**会报 `fetch failed`；而媒体经由 `dl.snapcdn.app` 中转是可下的——
   上面 3 个下载用例证明了这一点。这不是解析器的问题，换到能直连 `pbs.twimg.com` 的机器即可。
3. 原插件不支持转发推文（repost）与 MP3 转换（HTML 里有「转换为 MP3」按钮但没处理）；本实现保持一致。
4. 单条推文里若有**多个视频**，原插件只取第一个 MP4（有 `break`），本实现同样只取第一个。
5. `twitter_parser_standalone.py` 是等价 Python 版（aiohttp + beautifulsoup4，与原插件同依赖），
   逻辑照着原文件写，但**本机没有 Python 运行时，未执行过**；已实测的是 Node 版。

## 想搬回 AstrBot 插件里？

- 只想用解析逻辑：把 `parseXdownHtml()` 里那套规则抄进你的 parser 即可，`@handle` 正则原样保留。
- 想保留插件结构：把 `parseTweet()` 换成 `TwitterParser._parse()`，用 `self.create_video_content(...)`
  / `self.create_image_contents(...)` / `self.create_dynamic_contents(...)` 构造 `contents`。
- 想脱离 AstrBot 单独跑 Python 版：`pip install aiohttp beautifulsoup4` 后
  `python twitter_parser_standalone.py <链接> --download out`。
