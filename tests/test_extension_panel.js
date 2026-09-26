// popup.js「任务」页测试：失败任务筛选/渲染 + 一键重试的消息往返。
// popup.js 依赖 chrome.* 与 DOM，这里用最小桩件在 vm 里跑真实源码，
// 覆盖纯函数（failedTasksOf）与消息接线（retryTask / retryAllTasks）。
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const POPUP = fs.readFileSync(path.join(__dirname, "..", "extension", "popup.js"), "utf8");

const TASKS_PAYLOAD = {
  tasks: [
    { task_id: "a1", filename: "old.iso", status: "failed", error: "HTTP 404" },
    { task_id: "a2", filename: "run.mp4", status: "downloading" },
    { task_id: "a3", filename: "done.zip", status: "completed" },
    { task_id: "a4", filename: "new.bin", status: "failed", error: "" },
    { task_id: "a5", filename: "stop.torrent", status: "cancelled" },
  ],
  stats: { failed: 2 },
};

function makeEl(tag) {
  return {
    tagName: tag,
    children: [],
    listeners: {},
    className: "",
    title: "",
    disabled: false,
    style: {},
    _text: "",
    _html: "",
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
    appendChild(child) { this.children.push(child); return child; },
    set textContent(v) { this._text = String(v); },
    get textContent() { return this._text; },
    set innerHTML(v) { this._html = String(v); this.children.length = 0; },
    get innerHTML() { return this._html; },
  };
}

function makeSandbox(handler) {
  const elements = {};
  const messages = [];
  const intervals = [];
  let domReady = null;
  const sandbox = {
    console,
    setTimeout,
    clearTimeout,
    setInterval: (fn, ms) => { intervals.push({ fn, ms }); return intervals.length; },
    clearInterval: () => {},
    URL,
    URLSearchParams,
    chrome: {
      runtime: {
        sendMessage: (msg, cb) => { messages.push(msg); if (cb) cb(handler ? handler(msg) : undefined); },
      },
      storage: { local: { get(keys, cb) { cb({}); }, set() {} } },
      tabs: { query(q, cb) { cb([{ id: 3, url: "https://site/v", title: "T" }]); } },
    },
    document: {
      addEventListener(type, fn) { if (type === 'DOMContentLoaded') domReady = fn; },
      getElementById(id) { return elements[id] || (elements[id] = makeEl("div#" + id)); },
      createElement: (tag) => makeEl(tag),
    },
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(POPUP, sandbox, { filename: "popup.js" });
  return { sandbox, elements, messages, intervals, openPopup: () => domReady() };
}

// ① 纯函数：只留 failed/cancelled，最新失败排最前，最多 8 条
{
  const { sandbox } = makeSandbox();
  // Array.from：vm 里的数组是另一个 realm 的 Array，深比较前先搬回来
  const failed = Array.from(sandbox.failedTasksOf(TASKS_PAYLOAD));
  assert.deepStrictEqual(failed.map((t) => t.task_id), ["a5", "a4", "a1"]);
  assert.deepStrictEqual(Array.from(sandbox.failedTasksOf(null)), []);
  assert.deepStrictEqual(Array.from(sandbox.failedTasksOf({})), []);
  const many = { tasks: Array.from({ length: 12 }, (_, i) => ({ task_id: "t" + i, status: "failed" })) };
  const capped = Array.from(sandbox.failedTasksOf(many));
  assert.strictEqual(capped.length, 8);
  assert.strictEqual(capped[0].task_id, "t11", "最新的失败排最前");
}

// ② 渲染：失败行含文件名与错误，带重试按钮；文件名/错误必须转义
{
  const { sandbox } = makeSandbox();
  const row = sandbox.taskRow({ task_id: "a4", filename: "new.bin", status: "failed", error: "连接超时" });
  assert.strictEqual(row.children.length, 2);
  assert.ok(row.children[0].innerHTML.indexOf("new.bin") >= 0);
  assert.ok(row.children[0].innerHTML.indexOf("连接超时") >= 0);
  assert.strictEqual(row.children[1].textContent, "重试");
  const cancelled = sandbox.taskRow({ task_id: "a5", filename: "stop.torrent", status: "cancelled", error: "" });
  assert.ok(cancelled.children[0].innerHTML.indexOf("已取消") >= 0, "已取消要有兜底文案");
  const evil = sandbox.taskRow({ task_id: "x", filename: "<img src=x onerror=alert(1)>", status: "failed", error: "<b>" });
  assert.ok(evil.children[0].innerHTML.indexOf("<img") < 0, "文件名必须转义");
  assert.ok(evil.children[0].innerHTML.indexOf("&lt;img") >= 0);
}

// ③ 打开弹窗即刷角标；点重试发消息并回写按钮状态
{
  const { sandbox, elements, messages } = makeSandbox((msg) => {
    if (msg.action === "getTasks") return TASKS_PAYLOAD;
    if (msg.action === "retryTask") return { success: true, task: { task_id: msg.taskId } };
    return undefined;
  });
  sandbox.loadTasks();
  assert.ok(messages.some((m) => m.action === "getTasks"), "应拉取 /api/tasks");
  assert.strictEqual(elements.taskFailCount.textContent, "3", "失败+已取消都算");
  assert.strictEqual(elements.taskList.children.length, 3);
  assert.strictEqual(elements.retryAllBtn.style.visibility, "visible", "多条失败才显示全部重试");

  const firstRow = elements.taskList.children[0];
  const btn = firstRow.children[1];
  btn.listeners.click[0]();
  const retry = messages.filter((m) => m.action === "retryTask").pop();
  assert.ok(retry, "应发送 retryTask 消息");
  assert.strictEqual(retry.taskId, "a5", "带上是哪一条任务");
  assert.strictEqual(btn.textContent, "已重试");
  assert.strictEqual(btn.disabled, true);
}

// ④ 重试失败：按钮回到可点并带上原因；只有一条失败时隐藏「全部重试」
{
  const { sandbox, elements, messages } = makeSandbox((msg) => {
    if (msg.action === "getTasks") {
      return { tasks: [{ task_id: "a1", filename: "old.iso", status: "failed", error: "404" }] };
    }
    if (msg.action === "retryTask") return { success: false, error: "当前状态不支持重试" };
    return undefined;
  });
  sandbox.loadTasks();
  assert.strictEqual(elements.taskList.children.length, 1);
  assert.strictEqual(elements.retryAllBtn.style.visibility, "hidden");
  const btn = elements.taskList.children[0].children[1];
  btn.listeners.click[0]();
  assert.strictEqual(btn.textContent, "重试");
  assert.strictEqual(btn.disabled, false);
  assert.ok(btn.title.indexOf("不支持重试") >= 0, "失败原因要回填到按钮提示");
  assert.ok(messages.some((m) => m.action === "retryTask"));
}

// ⑤ 连不上 SwiftDM：给提示而不是空白面板
{
  const { sandbox, elements } = makeSandbox(() => undefined);
  sandbox.loadTasks();
  assert.strictEqual(elements.taskFailCount.textContent, "0");
  assert.ok(elements.taskList.innerHTML.indexOf("连不上") >= 0);
  assert.strictEqual(elements.retryAllBtn.style.visibility, "hidden");
}

// ⑥ 全部重试：发 retryAllTasks，按钮文案带上重试数量
{
  const { sandbox, elements, messages } = makeSandbox((msg) => {
    if (msg.action === "retryAllTasks") return { success: true, retried: 2 };
    return undefined;
  });
  sandbox.retryAllTasks();
  assert.ok(messages.some((m) => m.action === "retryAllTasks"));
  assert.strictEqual(elements.retryAllBtn.textContent, "已重试 2");
}

// ⑦ 实时状态：优先用后端 stats；老后端没给 stats 时按任务列表现算
{
  const { sandbox } = makeSandbox();
  const withStats = {
    tasks: [{ task_id: "a", status: "downloading", speed: 100 }],
    stats: { active: 1, failed: 3, total_speed: 2048 },
  };
  // active / speed 用后端 stats；failed 必须和「任务」页角标同口径（失败 + 已取消），
  // 不能直接用 stats.failed（那只数 failed，会和角标对不上）
  assert.deepStrictEqual({ ...sandbox.liveStatsOf(withStats) }, { active: 1, failed: 0, speed: 2048 });
  const mixed = {
    tasks: [{ task_id: "a", status: "downloading" }, { task_id: "b", status: "failed" },
            { task_id: "c", status: "cancelled" }],
    stats: { active: 5, failed: 0, total_speed: 10 },
  };
  assert.deepStrictEqual({ ...sandbox.liveStatsOf(mixed) }, { active: 5, failed: 2, speed: 10 });
  const noStats = { tasks: [
    { task_id: "a", status: "downloading", speed: 1024 },
    { task_id: "b", status: "paused", speed: 999 },
    { task_id: "c", status: "failed" },
    { task_id: "d", status: "cancelled" },
  ] };
  assert.deepStrictEqual({ ...sandbox.liveStatsOf(noStats) }, { active: 1, failed: 2, speed: 1024 },
    "speed 只累加下载中的任务");
  assert.deepStrictEqual({ ...sandbox.liveStatsOf(null) }, { active: 0, failed: 0, speed: 0 });
  assert.strictEqual(sandbox.formatSpeed(0), "0 B/s");
  assert.strictEqual(sandbox.formatSpeed(1536), "1.5 KB/s");
}

// ⑧ loadLive：一次请求同时写实时状态与失败角标
{
  const { sandbox, elements, messages } = makeSandbox((msg) => {
    if (msg.action === "getTasks") return TASKS_PAYLOAD;
    return undefined;
  });
  sandbox.loadLive();
  assert.strictEqual(messages.filter((m) => m.action === "getTasks").length, 1,
    "实时状态与失败列表应共用一次请求");
  assert.strictEqual(elements.liveActive.textContent, "1");
  assert.strictEqual(elements.liveSpeed.textContent, "0 B/s");
  assert.strictEqual(elements.liveFailed.textContent, "3", "失败含已取消");
  assert.strictEqual(elements.taskFailCount.textContent, "3");

  // 掉线：保留上一次的数字，不清空（避免用户看到突然归零）
  sandbox.loadLive();
  assert.strictEqual(elements.liveActive.textContent, "1");
  assert.strictEqual(elements.taskFailCount.textContent, "3");
}

// ⑨ 打开弹窗会拉一次实时状态，并按期刷新
{
  let reply = { tasks: [], stats: { active: 2, failed: 0, total_speed: 3072 } };
  const { sandbox, elements, intervals, openPopup } = makeSandbox((msg) => {
    if (msg.action === "getTasks") return reply;
    return undefined;
  });
  openPopup();
  assert.strictEqual(elements.liveActive.textContent, "2");
  assert.strictEqual(elements.liveSpeed.textContent, "3.0 KB/s");
  assert.strictEqual(intervals.length, 1, "打开 popup 应启动一个轮询定时器");
  assert.strictEqual(intervals[0].ms, 2000);
  intervals[0].fn();  // 定时器触发：再拉一次并刷新
  assert.strictEqual(elements.liveActive.textContent, "2");
}

console.log("popup.js tasks panel OK");

// ⑤ 服务器地址显示真正连通过的 base；
// 之前 popup 写死 127.0.0.1:5001（那是监控端口），主程序端口顺延时就是错的。
{
  const { sandbox } = makeSandbox();
  const strip = (b) => sandbox.serverBaseOf({ __base: b });
  assert.strictEqual(strip('http://127.0.0.1:5003'), '127.0.0.1:5003');
  assert.strictEqual(strip('http://127.0.0.1:5003/'), '127.0.0.1:5003/');
  // getStatus 路径给的字段名叫 base
  assert.strictEqual(sandbox.serverBaseOf({ base: 'http://127.0.0.1:5002' }), '127.0.0.1:5002');
  // 未知/无效时不能拼出来一个地址
  assert.strictEqual(sandbox.serverBaseOf(null), '');
  assert.strictEqual(sandbox.serverBaseOf({}), '');
  assert.strictEqual(sandbox.serverBaseOf({ __base: '' }), '');
}

{
  const { sandbox, elements } = makeSandbox();
  sandbox.renderServerBase({ __base: 'http://127.0.0.1:5004' });
  assert.strictEqual(elements.serverAddr.textContent, '127.0.0.1:5004');
  sandbox.renderServerBase({ __base: '' });
  assert.strictEqual(elements.serverAddr.textContent, '未连接');
}

// 打开弹窗时两个轮询都要回写地址（否则首屏会卡在“检测中”）
{
  const { openPopup, elements, messages } = makeSandbox((msg) => {
    if (msg.action === 'getStatus') return { enabled: true, sentCount: 1, base: 'http://127.0.0.1:5000' };
    if (msg.action === 'getTasks') return { tasks: [], stats: {}, __base: 'http://127.0.0.1:5000' };
    return undefined;
  });
  openPopup();
  assert.deepStrictEqual(messages.map((m) => m.action).slice(0, 2), ['getStatus', 'getTasks']);
  assert.strictEqual(elements.serverAddr.textContent, '127.0.0.1:5000');
}

// 本次请求失败不能再报 lastGoodBase，否则会显示“连着”却没数据
{
  const { sandbox } = makeSandbox();
  assert.strictEqual(sandbox.serverBaseOf({ tasks: [], ok: false, error: 'SwiftDM 未运行' }), '');
}
