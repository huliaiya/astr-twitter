/* astr-twitter 插件页面：X 解析历史
 *
 * 通过 AstrBot 注入的 window.AstrBotPluginPage 桥接 SDK 调用插件 Web API：
 *   apiGet("history")        → GET  /api/v1/plugins/extensions/<plugin>/history
 *   apiPost("history/clear") → POST /api/v1/plugins/extensions/<plugin>/history/clear
 *
 * 两个必须注意的坑（都是实测踩过的）：
 *   1. AstrBot 是在页面 </body> 前注入 bridge-sdk.js 的，也就是在本文件之后才加载，
 *      所以不能在脚本顶层直接读 window.AstrBotPluginPage —— 要等 DOMContentLoaded。
 *   2. 桥接层会把后端响应解包成 payload.data（axios 的 data 再取一层 data），
 *      所以 apiGet 的结果就是业务数据本身，不要再 .data 一次。
 */
(function () {
  "use strict";

  var bridge = null;
  var state = { settings: {}, loaded: false };

  function t(key, fallback) {
    try {
      if (bridge && bridge.t) {
        return bridge.t(key, fallback);
      }
    } catch (error) {
      /* 忽略：退回 fallback */
    }
    return fallback;
  }

  function el(id) {
    return document.getElementById(id);
  }

  function humanSize(bytes) {
    bytes = Number(bytes) || 0;
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
    if (bytes < 1024 * 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + " MB";
    return (bytes / 1024 / 1024 / 1024).toFixed(2) + " GB";
  }

  function renderStats(stats) {
    var counts = stats.counts || {};
    var cards = [
      [t("pages.history.total", "总解析"), stats.total || 0],
      [t("pages.history.ok", "成功"), stats.ok || 0],
      [t("pages.history.failed", "失败"), stats.failed || 0],
      [t("pages.history.rate", "成功率"), (stats.success_rate || 0) + "%"],
      [t("pages.history.video", "视频"), counts.video || 0],
      [t("pages.history.image", "图片"), counts.image || 0],
      [t("pages.history.gif", "GIF"), counts.dynamic || 0],
      [t("pages.history.bytes", "流量"), humanSize(stats.bytes)],
    ];
    el("cards").innerHTML = cards
      .map(function (item) {
        return (
          '<div class="card"><div class="k">' +
          item[0] +
          '</div><div class="v">' +
          item[1] +
          "</div></div>"
        );
      })
      .join("");

    var authors = (stats.top_authors || [])
      .map(function (a) {
        return "@" + a.name + " ×" + a.count;
      })
      .join("　");
    var settings = state.settings || {};
    el("meta").textContent =
      (authors ? t("pages.history.topAuthors", "常解析：") + authors + "　|　" : "") +
      "接口：" + (settings.api_endpoint || "-") +
      "　代理：" + (settings.proxy ? "已配置" : "未配置") +
      "　触发：" + (settings.trigger_mode || "-") +
      "　历史上限：" + (settings.history_size || "-");
  }

  function renderRows(records) {
    el("rows").innerHTML = records
      .map(function (record) {
        var counts = record.counts || {};
        var pills = Object.keys(counts)
          .filter(function (key) {
            return counts[key];
          })
          .map(function (key) {
            return '<span class="pill">' + key + " ×" + counts[key] + "</span>";
          })
          .join("");
        var detail = record.ok
          ? pills || "—"
          : '<span class="bad">' + (record.error || t("pages.history.failed", "失败")) + "</span>";
        var title = (record.title || "").replace(/\n/g, " ").slice(0, 60);
        return (
          "<tr>" +
          "<td>" + (record.time || "") + "</td>" +
          '<td class="' + (record.ok ? "ok" : "bad") + '">' + (record.ok ? "✅" : "❌") + "</td>" +
          "<td>" + detail + "</td>" +
          '<td class="url"><a href="' + record.url + '" target="_blank" rel="noreferrer">' +
          record.url + "</a>" +
          (title ? '<div class="meta">' + title + "</div>" : "") +
          "</td>" +
          "<td>" + (record.bytes ? humanSize(record.bytes) : "—") + "</td>" +
          "</tr>"
        );
      })
      .join("");
    el("empty").classList.toggle("hide", records.length > 0);
  }

  function showMessage(text, isError) {
    var box = el("empty");
    box.classList.remove("hide");
    box.textContent = text;
    box.style.color = isError ? "var(--danger)" : "var(--muted)";
  }

  function load() {
    // 桥接层已经解包，response 就是后端的 data 字段
    return bridge
      .apiGet("history", { limit: 100 })
      .then(function (response) {
        var data = response || {};
        state.settings = data.settings || {};
        state.loaded = true;
        renderStats(data.stats || {});
        renderRows(data.records || []);
      })
      .catch(function (error) {
        showMessage(
          t("pages.history.loadFailed", "加载失败：") + ((error && error.message) || error),
          true,
        );
      });
  }

  function clearAll() {
    if (!window.confirm(t("pages.history.confirmClear", "确定清空全部解析历史吗？"))) return;
    bridge
      .apiPost("history/clear", {})
      .then(load)
      .catch(function (error) {
        window.alert(
          t("pages.history.clearFailed", "清空失败：") + ((error && error.message) || error),
        );
      });
  }

  function boot() {
    bridge = window.AstrBotPluginPage;
    if (!bridge) {
      showMessage("请在 AstrBot WebUI 中打开本页面。", false);
      return;
    }

    var context = bridge.getContext() || {};
    el("title").textContent = context.pageTitle || t("pages.history.title", "X 解析历史");
    document.documentElement.setAttribute("data-theme", context.isDark ? "dark" : "light");
    el("refresh").textContent = t("pages.history.refresh", "刷新");
    el("clear").textContent = t("pages.history.clear", "清空");
    el("refresh").addEventListener("click", load);
    el("clear").addEventListener("click", clearAll);
    bridge.onContext(function (next) {
      document.documentElement.setAttribute(
        "data-theme",
        next && next.isDark ? "dark" : "light",
      );
    });

    showMessage(t("pages.history.loading", "正在加载…"), false);
    // ready() 要等父页面回一条 context；父页面没应答时也别一直空着
    var timer = window.setTimeout(function () {
      if (!state.loaded) {
        showMessage(t("pages.history.waiting", "正在等待 AstrBot WebUI 响应…"), false);
      }
    }, 2500);

    bridge.ready().then(function () {
      window.clearTimeout(timer);
      return load();
    });
  }

  if (document.readyState === "loading") {
    // bridge-sdk.js 在本文件之后注入，等 DOM 解析完再启动
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
