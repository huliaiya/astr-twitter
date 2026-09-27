/* AstrBot 插件页面桥接 SDK 的协议桩（只在 devtools/test-page.mjs 里用）。
 * 真实实现见 AstrBot 仓库 astrbot/dashboard/plugin_page_bridge.js，
 * 这里只保留页面用到的部分：ready / getContext / t / onContext / apiGet / apiPost。 */
(function attachAstrBotPluginPageBridgeStub() {
  const CHANNEL = "astrbot-plugin-page";
  const pending = new Map();
  let context = null;
  let resolveReady;
  const readyPromise = new Promise((resolve) => { resolveReady = resolve; });
  let counter = 0;

  window.addEventListener("message", (event) => {
    if (event.source !== window.parent) return;
    const message = event.data;
    if (!message || message.channel !== CHANNEL) return;
    if (message.kind === "context") {
      context = { ...(context || {}), ...message.context };
      if (resolveReady) { resolveReady(context); resolveReady = null; }
      return;
    }
    if (message.kind === "response") {
      const entry = pending.get(message.requestId);
      if (!entry) return;
      pending.delete(message.requestId);
      message.ok ? entry.resolve(message.data) : entry.reject(new Error(message.error || "failed"));
    }
  });

  function request(action, payload) {
    counter += 1;
    const requestId = `plugin_req_${counter}`;
    return new Promise((resolve, reject) => {
      pending.set(requestId, { resolve, reject });
      window.parent.postMessage({ channel: CHANNEL, kind: "request", requestId, action, ...payload }, window.location.origin);
    });
  }

  window.AstrBotPluginPage = {
    ready: () => readyPromise,
    getContext: () => context,
    getLocale: () => (context && context.locale) || "zh-CN",
    getI18n: () => (context && context.i18n) || {},
    t(key, fallback) {
      const parts = String(key).split(".");
      let value = context && context.i18n;
      for (const part of parts) {
        if (!value || typeof value !== "object" || !(part in value)) return fallback || "";
        value = value[part];
      }
      return typeof value === "string" ? value : fallback || "";
    },
    onContext: () => () => {},
    apiGet: (endpoint, params) => request("api:get", { endpoint, params }),
    apiPost: (endpoint, body) => request("api:post", { endpoint, body }),
  };

  window.parent.postMessage({ channel: CHANNEL, kind: "ready" }, window.location.origin);
})();
