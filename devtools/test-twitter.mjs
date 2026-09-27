// test-twitter.mjs
// X解析器自测：URL 匹配（离线） + 真实接口解析（在线） + 真实下载校验（在线）
//
// 运行： node test-twitter.mjs            # 全部
//        node test-twitter.mjs --offline  # 只跑离线用例（不联网）

import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  matchTweetUrl,
  parseTweet,
  parseXdownHtml,
  downloadMedia,
  ParseException,
} from "./twitter-parser.mjs";

const __dir = dirname(fileURLToPath(import.meta.url));
const OFFLINE = process.argv.includes("--offline");

let pass = 0;
let fail = 0;
const failures = [];

function ok(name, cond, detail = "") {
  if (cond) {
    pass++;
    console.log(`  ✅ ${name}${detail ? ` — ${detail}` : ""}`);
  } else {
    fail++;
    failures.push(name);
    console.log(`  ❌ ${name}${detail ? ` — ${detail}` : ""}`);
  }
}

function group(title) {
  console.log(`\n=== ${title} ===`);
}

/* ---------------------------------------------------------------- */
/* 1. URL 匹配（对应插件 @handle 正则）                              */
/* ---------------------------------------------------------------- */
group("1. URL 匹配");
{
  const cases = [
    ["https://x.com/Fortnite/status/1870484479980052921", "https://x.com/Fortnite/status/1870484479980052921"],
    ["https://www.x.com/Fortnite/status/1870484479980052921", "https://www.x.com/Fortnite/status/1870484479980052921"],
    ["https://twitter.com/Fortnite/status/1870484479980052921", "https://twitter.com/Fortnite/status/1870484479980052921"],
    ["https://mobile.twitter.com/a/status/123", "https://mobile.twitter.com/a/status/123"],
    ["https://x.com/i/web/status/1234567890", "https://x.com/i/web/status/1234567890"],
    ["看这个 https://x.com/a_b/status/999?s=46&t=xyz 哈哈", "https://x.com/a_b/status/999"],
  ];
  for (const [input, expected] of cases) {
    const m = matchTweetUrl(input);
    ok(`匹配 ${input.slice(0, 52)}`, m?.url === expected, m ? `→ ${m.url}` : "未匹配");
  }
  const negatives = [
    "https://example.com/a/status/123",
    "https://notx.com/a/status/123",   // 前缀必须不是字母数字或点
    "https://x.com/Fortnite",          // 没有 status/<id>
    "",
  ];
  for (const n of negatives) {
    ok(`拒绝 ${JSON.stringify(n)}`, matchTweetUrl(n) === null);
  }
}

/* ---------------------------------------------------------------- */
/* 2. HTML 解析（离线 fixture，对应 parse_twitter_html）             */
/* ---------------------------------------------------------------- */
group("2. HTML 解析（离线 fixture）");
for (const fixture of ["gif", "video", "photo"]) {
  let html;
  try {
    html = await readFile(join(__dir, "fixtures", `xdown-${fixture}.html`), "utf8");
  } catch {
    ok(`fixture xdown-${fixture}.html`, false, "文件不存在（先运行 fetch-fixtures.mjs）");
    continue;
  }
  const p = parseXdownHtml(html);
  if (fixture === "gif") {
    ok("gif: 抽出 1 个 gif", p.gifs.length === 1, `gifs=${p.gifs.length}`);
    ok("gif: gif 链接是 snapcdn", (p.gifs[0] ?? "").includes("dl.snapcdn.app"), p.gifs[0]?.slice(0, 48) + "…");
    ok("gif: 有封面", !!p.cover && p.cover.startsWith("https://pbs.twimg.com"), p.cover);
    ok("gif: 标题非空", !!p.title, p.title);
    ok("gif: 顺带抽到缩略图图片", p.images.length === 1, `images=${p.images.length}`);
  }
  if (fixture === "video") {
    ok("video: 抽到 1 个 MP4（首个=720p）", p.video !== null, (p.video ?? "").slice(0, 48) + "…");
    ok("video: 标题含推文正文", !!p.title && p.title.includes("Lucky"), p.title?.slice(0, 40));
    ok("video: 封面存在", !!p.cover, p.cover);
  }
  if (fixture === "photo") {
    ok("photo: 抽到图片", p.images.length >= 1, `images=${p.images.length}`);
    ok("photo: 无视频", p.video === null);
  }
  ok(`fixture ${fixture}: tweet id`, p.tweetId === null || /^\d+$/.test(p.tweetId), String(p.tweetId));
}

/* ---------------------------------------------------------------- */
/* 3. 真实接口解析（在线）                                            */
/* ---------------------------------------------------------------- */
if (!OFFLINE) {
  group("3. 真实解析（xdown 接口）");

  const live = [
    { name: "视频", url: "https://x.com/Fortnite/status/1904171341735178552", expect: (r) => r.counts.video === 1 && !!r.title && !!r.cover },
    { name: "单图", url: "https://x.com/Fortnite/status/1870484479980052921", expect: (r) => r.counts.image >= 1 },
    { name: "多图", url: "https://x.com/chitose_yoshino/status/1841416254810378314", expect: (r) => r.counts.image >= 2 },
    { name: "GIF", url: "https://x.com/Dithmenos9/status/1966798448499286345", expect: (r) => r.counts.dynamic === 1 },
    { name: "twitter.com 域名", url: "https://twitter.com/Fortnite/status/1870484479980052921", expect: (r) => r.counts.image >= 1 },
  ];

  /** @type {Record<string, any>} */
  const results = {};
  for (const t of live) {
    try {
      const r = await parseTweet(t.url);
      results[t.name] = r;
      ok(`解析「${t.name}」`, t.expect(r), `${JSON.stringify(r.counts)} title=${JSON.stringify(r.title)?.slice(0, 28)}`);
    } catch (e) {
      ok(`解析「${t.name}」`, false, `${e.name}: ${e.message}`);
    }
  }

  // 错误路径：不存在的推文应抛 ParseException 且带出接口 msg
  try {
    await parseTweet("https://x.com/NASA/status/1683502034445783040");
    ok("不存在的推文应报错", false, "竟然解析成功了");
  } catch (e) {
    ok("不存在的推文应报错", e instanceof ParseException, `${e.name}: ${e.message}`);
  }

  /* -------------------------------------------------------------- */
  /* 4. 真实下载 + 文件头校验                                        */
  /* -------------------------------------------------------------- */
  group("4. 真实下载校验");
  const outDir = join(__dir, "downloads");

  const video = results["视频"];
  if (video?.contents?.[0]) {
    try {
      const info = await downloadMedia(video.contents[0].url, outDir, { filename: `test-video-${video.tweet_id}.mp4` });
      ok("视频可下载且是 mp4", info.kind === "mp4" && info.bytes > 50_000, `${info.bytes} bytes, kind=${info.kind}, ${info.path}`);
    } catch (e) {
      ok("视频可下载且是 mp4", false, e.message);
    }
  }

  const img = results["多图"] ?? results["单图"];
  if (img?.contents?.[0]) {
    try {
      const info = await downloadMedia(img.contents[0].url, outDir, { filename: `test-image-${img.tweet_id}.jpg` });
      ok("图片可下载且是图片", ["jpeg", "png", "webp"].includes(info.kind) && info.bytes > 5_000, `${info.bytes} bytes, kind=${info.kind}, ${info.path}`);
    } catch (e) {
      ok("图片可下载且是图片", false, e.message);
    }
  }

  const gif = results["GIF"];
  if (gif?.contents?.find((c) => c.type === "dynamic")) {
    try {
      const info = await downloadMedia(gif.contents.find((c) => c.type === "dynamic").url, outDir, { filename: `test-gif-${gif.tweet_id}.mp4` });
      ok("GIF 可下载且是视频容器", info.kind === "mp4" && info.bytes > 5_000, `${info.bytes} bytes, kind=${info.kind}=${info.kind}`);
    } catch (e) {
      ok("GIF 可下载且是视频容器", false, e.message);
    }
  }
}

/* ---------------------------------------------------------------- */
console.log(`\n================ 结果 ================`);
console.log(`通过 ${pass} / 失败 ${fail}`);
if (fail) {
  console.log("失败项:\n" + failures.map((f) => ` - ${f}`).join("\n"));
}
process.exit(fail ? 1 : 0);
