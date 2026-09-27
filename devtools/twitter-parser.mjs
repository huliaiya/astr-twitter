// twitter-parser.mjs
// 从 astrbot_plugin_parser 的 core/parsers/twitter.py 单独抽出的X解析器（零依赖 Node 实现）
//
// 原实现链路：
//   1. BaseParser.search_url() 用「关键词 + 正则」匹配 x.com / twitter.com 的 status 链接
//   2. TwitterParser._parse() 把链接丢给 https://xdown.app/api/ajaxSearch（POST 表单 q=<url>&lang=zh-cn）
//   3. xdown 返回 {status:"ok", data:"<html>"}
//   4. parse_twitter_html() 从 HTML 里抽出 封面 / 视频 / 图片 / gif，构造 ParseResult
//
// 本文件把 2~4 步完整复刻，并把 1 步的正则也内置，可独立运行。

import { createWriteStream } from "node:fs";
import { mkdir, writeFile } from "node:fs/promises";
import { basename, join } from "node:path";
import { randomUUID } from "node:crypto";

/* ------------------------------------------------------------------ */
/* 1. URL 匹配（对应 twitter.py 的两个 @handle 正则）                   */
/* ------------------------------------------------------------------ */

// 与插件保持一致： (?<![A-Za-z0-9.-])(?:(?:www|mobile)\.)?twitter\.com/(?:[A-Za-z0-9_]+/)*status/\d+
const TWITTER_RE =
  /(?<![A-Za-z0-9.-])(?:(?:www|mobile)\.)?twitter\.com\/(?:[A-Za-z0-9_]+\/)*status\/\d+/;
// 与插件保持一致： (?<![A-Za-z0-9.-])(?:www\.)?x\.com/(?:[A-Za-z0-9_]+/)*status/\d+
const X_RE = /(?<![A-Za-z0-9.-])(?:www\.)?x\.com\/(?:[A-Za-z0-9_]+\/)*status\/\d+/;

/**
 * 从任意文本里提取X status 链接（对应 BaseParser.search_url 的匹配部分）
 * @param {string} text
 * @returns {{keyword: string, url: string, id: string}|null}
 */
export function matchTweetUrl(text) {
  if (!text) return null;
  for (const [keyword, re] of [
    ["x.com", X_RE],
    ["twitter.com", TWITTER_RE],
  ]) {
    // 关键词必须出现，否则直接跳过（沿用插件的 keyword not in url 优化）
    if (!text.includes(keyword)) continue;
    const m = re.exec(text);
    if (m) {
      const id = m[0].match(/status\/(\d+)$/)[1];
      return { keyword, url: `https://${m[0]}`, id };
    }
  }
  return null;
}

/** 从文本中提取所有X链接（群聊消息可能带多条） */
export function matchAllTweetUrls(text) {
  const out = [];
  for (const re of [X_RE, TWITTER_RE]) {
    for (const m of String(text ?? "").matchAll(new RegExp(re.source, "g"))) {
      const id = m[0].match(/status\/(\d+)$/)[1];
      out.push({ url: `https://${m[0]}`, id });
    }
  }
  return out;
}

/* ------------------------------------------------------------------ */
/* 2. 调用 xdown API（对应 _req_xdown_api）                            */
/* ------------------------------------------------------------------ */

const XDOWN_URL = "https://xdown.app/api/ajaxSearch";

// 对应 __init__ 里 update 的 headers
const XDOWN_HEADERS = {
  Accept: "application/json, text/plain, */*",
  "Content-Type": "application/x-www-form-urlencoded",
  Origin: "https://xdown.app",
  Referer: "https://xdown.app/",
  "User-Agent":
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
};

export class ParseException extends Error {
  constructor(message = "解析失败") {
    super(message);
    this.name = "ParseException";
  }
}

/**
 * 请求 xdown 接口，返回 JSON（对应 _req_xdown_api，含重试）
 * @param {string} url X链接
 * @param {{retry?: number, timeout?: number, fetchImpl?: typeof fetch}} [opts]
 */
export async function reqXdownApi(url, opts = {}) {
  const { retry = 2, timeout = 20000, fetchImpl = fetch } = opts;
  let lastErr;
  for (let attempt = 0; attempt <= retry; attempt++) {
    try {
      const resp = await fetchImpl(XDOWN_URL, {
        method: "POST",
        headers: XDOWN_HEADERS,
        body: new URLSearchParams({ q: url, lang: "zh-cn" }).toString(),
        signal: AbortSignal.timeout(timeout),
        redirect: "follow",
      });
      if (resp.status >= 400) {
        throw new Error(`xdown API ${resp.status} ${resp.statusText}`);
      }
      return await resp.json();
    } catch (e) {
      lastErr = e;
      if (attempt < retry) await new Promise((r) => setTimeout(r, 800 * (attempt + 1)));
    }
  }
  throw new ParseException(`xdown 请求失败: ${lastErr?.message ?? lastErr}`);
}

/* ------------------------------------------------------------------ */
/* 3. 解析 xdown 返回的 HTML（对应 parse_twitter_html）                 */
/* ------------------------------------------------------------------ */

const ENTITIES = {
  "&amp;": "&",
  "&lt;": "<",
  "&gt;": ">",
  "&quot;": '"',
  "&#39;": "'",
  "&apos;": "'",
  "&nbsp;": " ",
};

function decodeEntities(s) {
  return String(s).replace(/&(?:amp|lt|gt|quot|#39|apos|nbsp);/g, (m) => ENTITIES[m] ?? m);
}

function attr(tagHtml, name) {
  const m = tagHtml.match(new RegExp(`${name}\\s*=\\s*("([^"]*)"|'([^']*)')`, "i"));
  return m ? decodeEntities(m[2] ?? m[3] ?? "") : null;
}

function collectTags(html, tagName) {
  const re = new RegExp(`<${tagName}\\b[^>]*>[\\s\\S]*?</${tagName}>`, "gi");
  return [...html.matchAll(re)].map((m) => m[0]);
}

/**
 * 复刻 bs4 的 find/find_all 语义，但只用正则，零依赖。
 * @param {string} html xdown 返回的 data 字段
 */
export function parseXdownHtml(html) {
  if (typeof html !== "string" || !html.trim()) {
    throw new ParseException("解析失败, 数据为空");
  }

  // 1. 封面：第一个 <img src=...>
  const imgMatch = html.match(/<img\b[^>]*>/i);
  const cover = imgMatch ? attr(imgMatch[0], "src") : null;

  // 2. 下载链接：tw-button-dl 与 abutton 两类锚点
  //    插件里是 chain(find_all("a", class_="tw-button-dl"), find_all("a", class_="abutton"))
  const classesOf = (tagHtml) => (attr(tagHtml, "class") ?? "").split(/\s+/).filter(Boolean);
  const anchors = collectTags(html, "a");
  const primary = anchors.filter((a) => classesOf(a).includes("tw-button-dl"));
  const secondary = anchors.filter((a) => classesOf(a).includes("abutton"));

  let videoUrl = null;
  const images = [];
  const gifs = [];

  for (const tag of [...primary, ...secondary]) {
    const href = attr(tag, "href");
    if (!href) continue;
    const text = decodeEntities(tag.replace(/<[^>]*>/g, "")).trim();
    if (text.includes("下载 MP4")) {
      videoUrl = href;
      break; // 插件遇到第一个 MP4 就 break（多清晰度时取第一个，通常 720p）
    } else if (text.includes("下载图片")) {
      images.push(href);
    } else if (text.includes("下载 gif")) {
      gifs.push(href);
    }
  }

  // 3. 标题：第一个 <h3>
  const h3 = html.match(/<h3\b[^>]*>([\s\S]*?)<\/h3>/i);
  const title = h3 ? decodeEntities(h3[1].replace(/<[^>]*>/g, "")).trim() : null;

  // 4. 插件里被注释掉的 tweet id，这里作为附加信息输出
  const idInput = html.match(/<input\b[^>]*id\s*=\s*"TwitterId"[^>]*>/i);
  const tweetId = idInput ? attr(idInput[0], "value") : null;

  return { title, cover, video: videoUrl, images, gifs, tweetId };
}

/* ------------------------------------------------------------------ */
/* 4. 组装 ParseResult（对应 self.result(...) / create_*_content）      */
/* ------------------------------------------------------------------ */

/**
 * 解析一条X链接，返回与插件 ParseResult 对齐的结构
 * @param {string} input 链接或包含链接的文本
 * @param {{retry?: number, timeout?: number, fetchImpl?: typeof fetch, raw?: boolean}} [opts]
 */
export async function parseTweet(input, opts = {}) {
  const matched = matchTweetUrl(input) ?? (input?.includes?.("status/") ? null : null);
  const url = matched ? matched.url : null;
  if (!url) throw new ParseException("无法匹配 URL");

  const resp = await reqXdownApi(url, opts);
  if (resp?.status !== "ok") {
    // 沿用插件逻辑：非 ok 一律 ParseException，把接口 msg 带出来便于排查
    throw new ParseException(resp?.msg || "解析失败");
  }
  const data = resp?.data;
  if (data == null) throw new ParseException("解析失败, 数据为空");

  const htmlParts = parseXdownHtml(data);

  // 内容顺序与插件一致：视频 → 图片 → gif(动态)
  const contents = [];
  if (htmlParts.video) {
    contents.push({ type: "video", url: htmlParts.video, cover: htmlParts.cover, duration: 0 });
  }
  for (const u of htmlParts.images) {
    contents.push({ type: "image", url: u, cover: null });
  }
  for (const u of htmlParts.gifs) {
    // create_dynamic_contents() → DynamicContent，下载走 download_video
    contents.push({ type: "dynamic", url: u, cover: htmlParts.cover, note: "gif" });
  }

  const result = {
    platform: { name: "twitter", display_name: "X" },
    url,
    tweet_id: htmlParts.tweetId ?? matched?.id ?? null,
    title: htmlParts.title,
    author: { name: "无用户名", avatar: null }, // 插件里就是硬编码 "无用户名"
    cover: htmlParts.cover,
    contents,
    counts: {
      video: htmlParts.video ? 1 : 0,
      image: htmlParts.images.length,
      dynamic: htmlParts.gifs.length,
    },
  };

  if (opts.raw) result._raw_html = data;
  return result;
}

/* ------------------------------------------------------------------ */
/* 5. 可选：真实下载媒体，验证链接可用（原插件的 Downloader 部分）      */
/* ------------------------------------------------------------------ */

const MAGIC = [
  { kind: "jpeg", bytes: [0xff, 0xd8, 0xff] },
  { kind: "png", bytes: [0x89, 0x50, 0x4e, 0x47] },
  { kind: "gif", bytes: [0x47, 0x49, 0x46, 0x38] },
  { kind: "webp", bytes: [0x52, 0x49, 0x46, 0x46] },
];

export function sniffKind(buf) {
  for (const { kind, bytes } of MAGIC) {
    if (bytes.every((b, i) => buf[i] === b)) return kind;
  }
  if (buf.length > 12 && buf.subarray(4, 8).toString("latin1") === "ftyp") return "mp4";
  return "unknown";
}

/**
 * 下载一个媒体 URL 到本地，返回校验信息。
 * @param {string} url
 * @param {string} outDir
 * @param {{timeout?: number, fetchImpl?: typeof fetch, filename?: string}} [opts]
 */
export async function downloadMedia(url, outDir, opts = {}) {
  const { timeout = 60000, fetchImpl = fetch } = opts;
  await mkdir(outDir, { recursive: true });

  const resp = await fetchImpl(url, {
    redirect: "follow",
    signal: AbortSignal.timeout(timeout),
    headers: { "User-Agent": XDOWN_HEADERS["User-Agent"] },
  });
  if (!resp.ok) throw new Error(`下载失败 HTTP ${resp.status} ${resp.statusText}`);

  const buf = Buffer.from(await resp.arrayBuffer());

  // 最终 URL 的扩展名优先，否则按魔数猜
  let finalUrl = resp.url || url;
  let ext = "";
  try {
    ext = "." + (new URL(finalUrl).pathname.split(".").pop() || "");
  } catch {}
  const kind = sniffKind(buf);
  if (!/^\.(jpe?g|png|gif|mp4|webp|mov)$/i.test(ext)) {
    ext = kind === "unknown" ? ".bin" : `.${kind === "jpeg" ? "jpg" : kind}`;
  }
  const filename = opts.filename ?? `${Date.now()}-${randomUUID().slice(0, 8)}${ext}`;
  const path = join(outDir, basename(filename));
  await writeFile(path, buf);

  return {
    path,
    bytes: buf.length,
    kind,
    contentType: resp.headers.get("content-type"),
    finalUrl,
  };
}

/* ------------------------------------------------------------------ */
/* 6. CLI                                                             */
/* ------------------------------------------------------------------ */

function usage() {
  console.log(`用法:
  node twitter-parser.mjs <X链接或包含链接的文本> [--download [目录]] [--raw]

参数:
  --download [目录]  解析后真实下载全部媒体到指定目录（默认 ./downloads），并校验文件头
  --raw              输出中附带 xdown 返回的原始 HTML（调试用）
  --help             显示帮助

示例:
  node twitter-parser.mjs https://x.com/Fortnite/status/1870484479980052921
  node twitter-parser.mjs "看这个 https://x.com/Dithmenos9/status/1966798448499286345" --download out`);
}

async function main() {
  const argv = process.argv.slice(2);
  if (!argv.length || argv.includes("--help") || argv.includes("-h")) return usage();

  const dlIdx = argv.indexOf("--download");
  let dlDir = "downloads";
  if (dlIdx >= 0) {
    const next = argv[dlIdx + 1];
    if (next && !next.startsWith("--")) dlDir = next;
  }
  const positional = argv.filter((a, i) => !a.startsWith("--") && !(dlIdx >= 0 && i === dlIdx + 1));
  const input = positional.join(" ");

  try {
    const result = await parseTweet(input, { raw: argv.includes("--raw") });
    console.log(JSON.stringify(result, null, 2));

    if (dlIdx >= 0) {
      console.log(`\n=== 下载到 ${dlDir}/ ===`);
      for (const c of result.contents) {
        try {
          const info = await downloadMedia(c.url, dlDir);
          console.log(`[${c.type}] ${info.path} | ${info.bytes} bytes | ${info.kind} | ${info.finalUrl}`);
        } catch (e) {
          console.log(`[${c.type}] 下载失败: ${e.message}`);
          process.exitCode = 1;
        }
      }
      if (result.cover) {
        try {
          const info = await downloadMedia(result.cover, dlDir, { filename: `cover-${result.tweet_id}.jpg` });
          console.log(`[cover] ${info.path} | ${info.bytes} bytes | ${info.kind}`);
        } catch (e) {
          console.log(`[cover] 下载失败: ${e.message}`);
        }
      }
    }
  } catch (e) {
    console.error(`${e.name}: ${e.message}`);
    process.exitCode = 1;
  }
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main();
}
