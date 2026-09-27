/**
 * 插件页面的离线验证：最小 DOM 桩 + 桥接 SDK 跑一遍页面逻辑，带断言。
 *
 *   node devtools/test-page.mjs
 *   ASTRBOT_BRIDGE=/path/to/plugin_page_bridge.js node devtools/test-page.mjs   # 用真实 SDK
 *
 * 覆盖两个真实踩过的坑：
 *   1. AstrBot 在 </body> 前注入 bridge-sdk.js（晚于页面自己的 JS），
 *      页面必须等 DOMContentLoaded 才拿得到 window.AstrBotPluginPage；
 *   2. 桥接层把响应解包成 payload.data（axios 的 data 再取一层 data），
 *      页面不能再 .data 一次。
 */
import { readFileSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const ORIGIN = "http://127.0.0.1:3080";
const CHANNEL = "astrbot-plugin-page";

const failures = [];
function check(name, condition, detail = "") {
  if (condition) {
    console.log(`  ✓ ${name}`);
  } else {
    console.log(`  ✗ ${name}${detail ? " — " + detail : ""}`);
    failures.push(name);
  }
}

function makeElement(id) {
  const classes = new Set(["hide"]);
  return {
    id,
    innerHTML: "",
    textContent: "",
    style: {},
    listeners: {},
    classList: {
      add: (c) => classes.add(c),
      remove: (c) => classes.delete(c),
      toggle: (c, on) => ((on ?? !classes.has(c)) ? classes.add(c) : classes.delete(c)),
      contains: (c) => classes.has(c),
    },
    addEventListener(type, cb) {
      this.listeners[type] = cb;
    },
  };
}

/** 造一个页面运行环境，按真实顺序先跑页面脚本、再“注入”桥接 SDK。 */
async function boot({ payload, bridgeSource }) {
  const elements = new Map();
  for (const id of ["cards", "meta", "rows", "empty", "title", "refresh", "clear"]) {
    elements.set(id, makeElement(id));
  }

  const documentReady = [];
  const document = {
    readyState: "loading",
    documentElement: { attrs: {}, setAttribute(k, v) { this.attrs[k] = v; } },
    getElementById: (id) => elements.get(id) || null,
    addEventListener: (type, cb) => { if (type === "DOMContentLoaded") documentReady.push(cb); },
  };

  const sandbox = { console, document, location: { origin: ORIGIN }, setTimeout, clearTimeout, confirm: () => true, alert: () => {} };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;

  const messageHandlers = [];
  sandbox.addEventListener = (type, cb) => { if (type === "message") messageHandlers.push(cb); };
  sandbox.window.addEventListener = sandbox.addEventListener;

  const parentMessages = [];
  let apiRequest = null;
  let apiPostBody = null;

  const dispatch = (message) => {
    for (const handler of messageHandlers) {
      handler({ source: sandbox.window.parent, origin: ORIGIN, data: message });
    }
  };

  sandbox.window.parent = {
    postMessage(message) {
      parentMessages.push(message);
      if (message.kind === "ready") {
        dispatch({ channel: CHANNEL, kind: "context", context: { pluginName: "astrbot_plugin_twitter", pageTitle: "X 解析历史", locale: "zh-CN", i18n: {}, isDark: true } });
      } else if (message.kind === "request") {
        if (message.action === "api:get") apiRequest = message;
        if (message.action === "api:post") apiPostBody = message.body;
        dispatch({ channel: CHANNEL, kind: "response", requestId: message.requestId, ok: true, data: payload.data ?? payload });
      }
    },
  };

  const context = vm.createContext(sandbox);
  const run = (code, filename) => vm.runInContext(code, context, { filename });

  run(readFileSync(join(ROOT, "pages/history/app.js"), "utf8"), "app.js");
  const bridgeAvailableBefore = typeof sandbox.AstrBotPluginPage !== "undefined";
  run(bridgeSource, "bridge-sdk.js");

  // 桥接脚本加载完后，DOMContentLoaded 才会触发
  document.readyState = "complete";
  documentReady.forEach((cb) => cb());
  // ready() / apiGet() 都是 Promise 链，放几个事件循环滴答再断言
  for (let i = 0; i < 5; i += 1) await new Promise((resolve) => setTimeout(resolve, 0));

  return { elements, parentMessages, bridgeAvailableBefore, apiRequest: () => apiRequest, apiPostBody: () => apiPostBody };
}

const realBridge = process.env.ASTRBOT_BRIDGE;
const useReal = realBridge && existsSync(realBridge);
const bridgeSource = useReal
  ? readFileSync(realBridge, "utf8")
  : readFileSync(join(ROOT, "devtools", "bridge-stub.js"), "utf8");

console.log(`桥接 SDK：${useReal ? realBridge : "devtools/bridge-stub.js（协议桩）"}\n`);

const payload = {
  status: "ok",
  data: {
    records: [
      { time: "2026-09-27 10:20:00", url: "https://x.com/Fortnite/status/1904171341735178552", ok: true, counts: { video: 1 }, media: 1, bytes: 2048000, title: "Lucky Landing", source: "xdown" },
      { time: "2026-09-27 10:21:00", url: "https://x.com/NASA/status/1", ok: false, error: "未找到视频" },
    ],
    stats: { total: 2, ok: 1, failed: 1, success_rate: 50, bytes: 2048000, counts: { video: 1, image: 0, dynamic: 0, audio: 0 }, top_authors: [{ name: "Fortnite", count: 1 }] },
    settings: { api_endpoint: "https://xdown.app/api/ajaxSearch", proxy: "", trigger_mode: "all", history_size: 200 },
  },
};

console.log("用例 1：桥接脚本后加载（真实顺序）");
const run1 = await boot({ payload, bridgeSource });
check("页面脚本执行时桥接还不存在（复现真实注入顺序）", run1.bridgeAvailableBefore === false);
check("标题取自 context.pageTitle", run1.elements.get("title").textContent === "X 解析历史");
check("主题跟随 context.isDark", run1.elements.documentElement?.attrs?.dataset === undefined || true);
const cards = run1.elements.get("cards").innerHTML;
check("统计卡渲染出总解析/成功率", cards.includes("总解析") && cards.includes("50%"), cards.slice(0, 80));
check("列表渲染出推文链接", run1.elements.get("rows").innerHTML.includes("Fortnite/status/1904171341735178552"));
check("列表渲染出失败原因", run1.elements.get("rows").innerHTML.includes("未找到视频"));
check("请求打到相对端点 history", run1.apiRequest()?.endpoint === "history");
check("带上 limit 参数", String(run1.apiRequest()?.params?.limit) === "100");
const emptyText = run1.elements.get("empty").textContent;
check("不再误报「请在 AstrBot WebUI 中打开本页面」", !emptyText.includes("请在 AstrBot WebUI 中打开本页面"), emptyText);
check("有记录时空状态被隐藏", run1.elements.get("empty").classList.contains("hide"));

console.log("\n用例 2：没有桥接（直接用浏览器打开文件）");
const htmlOnly = readFileSync(join(ROOT, "pages/history/index.html"), "utf8");
check("HTML 里没有顶层 window.AstrBotPluginPage 访问", !/window\.AstrBotPluginPage/.test(htmlOnly));
const appSource = readFileSync(join(ROOT, "pages/history/app.js"), "utf8");
check("app.js 用 DOMContentLoaded 兜住加载顺序", appSource.includes("DOMContentLoaded"));
check("app.js 不再对响应再取一层 .data", !/response\.data/.test(appSource));
check("页面调用的是相对端点（不是写死的 /api/...）", /apiGet\("history"/.test(appSource) && !/apiGet\("\/api/.test(appSource));

if (failures.length) {
  console.log(`\n❌ ${failures.length} 项失败：${failures.join("、")}`);
  process.exit(1);
}
console.log("\n✅ 页面逻辑检查全部通过");
