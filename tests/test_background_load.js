// background.js 依赖 chrome.*。这里把 sniff.js + background.js 拼成一段脚本（等同 MV3 的
// importScripts 效果）在 vm 里跑，用最小桩件验证嗅探上报与 popup 消息接线。
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const EXT = (f) => fs.readFileSync(path.join(__dirname, "..", "extension", f), "utf8");
const listeners = { webRequest: [], runtime: [], tabs: [] };
const store = { enabled: true };
const fetchCalls = [];

const chrome = {
  downloads: { onCreated: { addListener() {} }, cancel() {}, erase() {} },
  webRequest: { onHeadersReceived: { addListener: (fn) => listeners.webRequest.push(fn) } },
  runtime: {
    onMessage: { addListener: (fn) => listeners.runtime.push(fn) },
    getURL: (p) => "chrome-extension://x/" + p,
    lastError: null,
  },
  storage: {
    local: {
      get(keys, cb) { cb(Object.assign({}, store)); },
      set(obj) { Object.assign(store, obj); },
    },
  },
  tabs: {
    get(id, cb) { cb({ id, url: "https://site/v", title: "视频页" }); },
    onRemoved: { addListener: (fn) => listeners.tabs.push(fn) },
  },
  cookies: { getAll(filter, cb) { cb([{ name: "sid", value: "abc", domain: ".site", path: "/" }]); } },
  notifications: { create() {} },
};

const sandbox = {
  chrome, console, setTimeout, clearTimeout, setInterval: () => 0, URL, URLSearchParams,
  AbortController: class { constructor() { this.signal = {}; } abort() {} },
  fetch: (url, opt) => {
    fetchCalls.push({ url, opt });
    const body = url.indexOf("/api/media/list") >= 0
      ? { ok: true, items: [{ url: "https://cdn.site/hls/1080p/index.m3u8", kind: "hls" }] }
      : url.indexOf("/api/tasks") >= 0
        ? { tasks: [{ task_id: "a1", filename: "old.iso", status: "failed", error: "404" }], stats: {} }
        : { success: true, ok: true, added: 1, task: { filename: "index.mp4" } };
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
  },
};
sandbox.globalThis = sandbox;
sandbox.self = sandbox;
vm.createContext(sandbox);

const backgroundSource = EXT("background.js");
assert.ok(/importScripts\(['"]sniff\.js['"]\)/.test(backgroundSource),
  "background.js 必须引入 sniff.js");
vm.runInContext(EXT("sniff.js") + "\n" +
  backgroundSource.replace(/importScripts\([^)]*\);/, ""), sandbox,
  { filename: "background.js" });

(async () => {
  assert.ok(sandbox.SwiftDMSniff, "sniff.js 应把规则挂到 globalThis");
  assert.strictEqual(listeners.runtime.length, 1, "只应有一个 onMessage 监听器（消息在同一个 handler 内分流）");
  assert.strictEqual(listeners.webRequest.length, 2, "文件下载检测与媒体嗅探各一个监听器");

  // ① 清单响应 → 上报 discover
  const sniffListener = listeners.webRequest[1];
  await Promise.resolve(sniffListener({
    url: "https://cdn.site/hls/1080p/index.m3u8", tabId: 7, type: "xmlhttprequest",
    status: 200, responseHeaders: [{ name: "Content-Type", value: "application/vnd.apple.mpegurl" }],
  }));
  await new Promise((r) => setTimeout(r, 30));
  const posted = fetchCalls.find((c) => c.url.indexOf("/api/media/discover") >= 0);
  assert.ok(posted, "应向 /api/media/discover 上报");
  const body = JSON.parse(posted.opt.body);
  assert.strictEqual(body.tab_id, "7");
  assert.strictEqual(body.page_url, "https://site/v");
  assert.strictEqual(body.items[0].kind, "hls");
  assert.strictEqual(body.items[0].quality_hint, "1080P");

  // ② popup 拉列表
  const handler = listeners.runtime[0];
  let listResponse = null;
  handler({ action: "getMedia", tabId: 7 }, {}, (r) => { listResponse = r; });
  await new Promise((r) => setTimeout(r, 30));
  assert.ok(listResponse && listResponse.ok === true, "getMedia 应回 ok:true");

  // ③ popup 点下载：带 kind / referer / Cookie
  let addResponse = null;
  handler({
    action: "downloadMedia", tabId: 7, pageUrl: "https://site/v",
    item: { url: "https://cdn.site/hls/1080p/index.m3u8", kind: "hls" },
  }, {}, (r) => { addResponse = r; });
  await new Promise((r) => setTimeout(r, 30));
  const add = fetchCalls.find((c) => c.url.indexOf("/api/add") >= 0);
  assert.ok(add, "downloadMedia 应调用 /api/add");
  const payload = JSON.parse(add.opt.body);
  assert.strictEqual(payload.kind, "hls");
  assert.strictEqual(payload.referer, "https://site/v");
  assert.ok(/Netscape HTTP Cookie File/.test(payload.cookies_netscape), "应随任务上传 Cookie");
  assert.ok(addResponse && addResponse.success === true);

  // ④ 分片摘要条目不可下载：必须在扩展侧就拒绝，不发 /api/add
  const before = fetchCalls.length;
  let segResponse = null;
  handler({
    action: "downloadMedia", tabId: 7, pageUrl: "https://site/v",
    item: { url: "https://cdn.site/hls/seg-9.ts", kind: "hls_segments" },
  }, {}, (r) => { segResponse = r; });
  await new Promise((r) => setTimeout(r, 30));
  assert.strictEqual(fetchCalls.length, before, "hls_segments 不该发请求");
  assert.ok(segResponse && segResponse.success === false && /清单/.test(segResponse.error));

  // ⑤ MSE 条目同样不可下载
  let mseResponse = null;
  handler({
    action: "downloadMedia", tabId: 7, pageUrl: "https://site/v",
    item: { url: "blob:https://site/9c0b", kind: "mse", is_mse: true },
  }, {}, (r) => { mseResponse = r; });
  await new Promise((r) => setTimeout(r, 30));
  assert.ok(mseResponse && mseResponse.success === false && /MSE/.test(mseResponse.error));

  // ⑥ content script 的 DOM 结果也走同一个上报通道
  const domItems = [{ url: "blob:https://site/vid", kind: "mse", is_mse: true }];
  handler({ type: "swiftdm-dom-media", items: domItems }, { tab: { id: 7, url: "https://site/v", title: "T" } }, () => {});
  await new Promise((r) => setTimeout(r, 30));
  const domPost = fetchCalls.filter((c) => c.url.indexOf("/api/media/discover") >= 0).pop();
  assert.deepStrictEqual(JSON.parse(domPost.opt.body).items, domItems);

  // ⑦ popup「任务」页：拉任务列表（GET，不带 body）
  let tasksResponse = null;
  handler({ action: 'getTasks' }, {}, (r) => { tasksResponse = r; });
  await new Promise((r) => setTimeout(r, 30));
  const tasksGet = fetchCalls.find((c) => c.url.indexOf('/api/tasks') >= 0);
  assert.ok(tasksGet, 'getTasks 应请求 /api/tasks');
  assert.strictEqual(tasksGet.opt.method, undefined, '/api/tasks 是 GET');
  assert.ok(tasksResponse && Array.isArray(tasksResponse.tasks), 'getTasks 应回传 tasks 数组');

  // ⑧ 一键重试 / 全部重试：taskId 必须 URL 编码（真实 id 可能带空格等字符）
  let retryResponse = null;
  handler({ action: 'retryTask', taskId: 'dl 12/x' }, {}, (r) => { retryResponse = r; });
  await new Promise((r) => setTimeout(r, 30));
  const retryPost = fetchCalls.filter((c) => c.url.indexOf('/api/retry/') >= 0).pop();
  assert.ok(retryPost, 'retryTask 应请求 /api/retry/<id>');
  assert.strictEqual(decodeURIComponent(retryPost.url.split('/api/retry/')[1]), 'dl 12/x');
  assert.strictEqual(retryPost.opt.method, 'POST');
  assert.ok(retryResponse && retryResponse.success === true);

  // ⑨ 缺 taskId 直接拒绝，不发请求
  const beforeRetry = fetchCalls.length;
  let badRetry = null;
  handler({ action: 'retryTask' }, {}, (r) => { badRetry = r; });
  assert.strictEqual(fetchCalls.length, beforeRetry, '缺 taskId 不该发请求');
  assert.ok(badRetry && badRetry.success === false && badRetry.error);

  // ⑩ 全部重试
  let allResponse = null;
  handler({ action: 'retryAllTasks' }, {}, (r) => { allResponse = r; });
  await new Promise((r) => setTimeout(r, 30));
  const retryAll = fetchCalls.filter((c) => c.url.indexOf('/api/retry_all') >= 0).pop();
  assert.ok(retryAll, 'retryAllTasks 应请求 /api/retry_all');
  assert.ok(allResponse && allResponse.success === true);

  console.log("background.js wiring OK");
})().catch((e) => { console.error(e); process.exit(1); });
