/* astr-twitter 插件页面：解析历史
 *
 * 通过 AstrBot 注入的 window.AstrBotPluginPage 桥接 SDK 调用插件 Web API：
 *   apiGet("history")        → GET  /api/v1/plugins/extensions/<plugin>/history
 *   apiPost("history/clear") → POST /api/v1/plugins/extensions/<plugin>/history/clear
 * 页面本身是静态文件，不需要打包工具。
 */
(function () {
  "use strict";

  var bridge = window.AstrBotPluginPage;

  function t(key, fallback) {
    try {
      return bridge && bridge.t ? bridge.t(key, fallback) : fallback;
    } catch (error) {
      return fallback;
    }
  }

  function humanSize(bytes) {
    bytes = Number(bytes) || 0;
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
    if (bytes < 1024 * 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + " MB";
    return (bytes / 1024 / 1024 / 1024).toFixed(2) + " GB";
  }

  function el(id) {
    return document.getElementById(id);
  }

  function renderStats(stats) {
    var counts = stats.counts || {};
    var cards = [
      ["总解析", stats.total || 0],
      ["成功", stats.ok || 0],
      ["失败", stats.failed || 0],
      ["成功率", (stats.success_rate || 0) + "%"],
      ["视频", counts.video || 0],
      ["图片", counts.image || 0],
      ["GIF", counts.dynamic || 0],
      ["流量", humanSize(stats.bytes)],
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
    var rows = records.map(function (record) {
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
        : '<span class="bad">' + (record.error || "失败") + "</span>";
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
    });
    el("rows").innerHTML = rows.join("");
    el("empty").classList.toggle("hide", records.length > 0);
  }

  var state = { settings: {} };

  function load() {
    return bridge
      .apiGet("history", { limit: 100 })
      .then(function (response) {
        var data = (response && response.data) || {};
        state.settings = data.settings || {};
        renderStats(data.stats || {});
        renderRows(data.records || []);
      })
      .catch(function (error) {
        el("rows").innerHTML = "";
        el("empty").classList.remove("hide");
        el("empty").textContent = t("pages.history.loadFailed", "加载失败：") + (error && error.message);
      });
  }

  function clearAll() {
    if (!window.confirm(t("pages.history.confirmClear", "确定清空全部解析历史吗？"))) return;
    bridge
      .apiPost("history/clear", {})
      .then(load)
      .catch(function (error) {
        window.alert(t("pages.history.clearFailed", "清空失败：") + (error && error.message));
      });
  }

  function boot() {
    el("title").textContent = t("pages.history.title", "推特解析历史");
    var ctx = (bridge.getContext && bridge.getContext()) || {};
    document.documentElement.setAttribute("data-theme", ctx.isDark ? "dark" : "light");
    el("refresh").textContent = t("pages.history.refresh", "刷新");
    el("clear").textContent = t("pages.history.clear", "清空");
    el("refresh").addEventListener("click", load);
    el("clear").addEventListener("click", clearAll);
    bridge.onContext(function (context) {
      document.documentElement.setAttribute("data-theme", context && context.isDark ? "dark" : "light");
    });
    load();
  }

  if (bridge) {
    bridge.ready().then(boot);
  } else {
    // 直接打开文件时（没有 WebUI 桥）给出提示，方便调试
    el("empty").classList.remove("hide");
    el("empty").textContent = "请在 AstrBot WebUI 中打开本页面。";
  }
})();
