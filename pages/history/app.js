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

  // 页面里所有插入 DOM 的字符串都走这里，避免 XSS 或属性引号问题
  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function renderStats(stats) {
    var counts = stats.counts || {};
    var cards = [
      { k: t("pages.history.total", "总解析"), v: stats.total || 0, cls: "" },
      { k: t("pages.history.ok", "成功"), v: stats.ok || 0, cls: "ok" },
      { k: t("pages.history.failed", "失败"), v: stats.failed || 0, cls: "bad" },
      { k: t("pages.history.rate", "成功率"), v: (stats.success_rate || 0) + "%", cls: "accent" },
      { k: t("pages.history.video", "视频"), v: counts.video || 0, cls: "" },
      { k: t("pages.history.image", "图片"), v: counts.image || 0, cls: "" },
      { k: t("pages.history.gif", "GIF"), v: counts.dynamic || 0, cls: "" },
      { k: t("pages.history.bytes", "流量"), v: humanSize(stats.bytes), cls: "" },
    ];
    el("cards").innerHTML = cards
      .map(function (item) {
        return (
          '<div class="stat-card"><div class="k">' +
          esc(item.k) +
          '</div><div class="v' +
          (item.cls ? " " + item.cls : "") +
          '">' +
          esc(item.v) +
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

  function renderCards(records) {
    var list = el("recordsList");
    if (!records || records.length === 0) {
      list.innerHTML = "";
      el("empty").classList.remove("hide");
      return;
    }
    el("empty").classList.add("hide");

    list.innerHTML = records
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
          ? '<div class="pills">' + (pills || "—") + "</div>"
          : '<span class="bad">' + esc(record.error || t("pages.history.failed", "失败")) + "</span>";
        var title = esc((record.title || "").replace(/\n/g, " ").slice(0, 200));
        var statusCls = record.ok ? "ok" : "bad";
        var statusIcon = record.ok ? "✅" : "❌";

        return (
          '<div class="record-card" data-id="' + esc(record.url) + '">' +
          '<div class="record-header" onclick="toggleCard(this)">' +
          '<div class="status-badge ' + statusCls + '">' + statusIcon + '</div>' +
          '<div class="record-main">' +
          '<div class="record-time">' + esc(record.time || "") + '</div>' +
          '<div class="record-url"><a href="' + esc(record.url) + '" target="_blank" rel="noreferrer" onclick="event.stopPropagation()">' + esc(record.url) + '</a></div>' +
          '</div>' +
          '<span class="expand-icon">▼</span>' +
          '</div>' +
          '<div class="record-details">' +
          (detail ? '<div class="detail-row"><span class="detail-label">媒体</span><div class="detail-value">' + detail + '</div></div>' : '') +
          (title ? '<div class="detail-row"><span class="detail-label">标题</span><div class="detail-value">' + title + '</div></div>' : '') +
          '<div class="detail-row"><span class="detail-label">大小</span><div class="detail-value">' + esc(record.bytes ? humanSize(record.bytes) : "—") + '</div></div>' +
          '<div class="detail-row"><span class="detail-label">来源</span><div class="detail-value">' + esc(record.source || "—") + '</div></div>' +
          '</div>' +
          '</div>'
        );
      })
      .join("");

    // 点击展开/折叠
    window.toggleCard = function (header) {
      var card = header.closest(".record-card");
      card.classList.toggle("expanded");
    };
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
        renderCards(data.records || []);
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