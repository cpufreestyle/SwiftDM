// showPanel 回归测试：三个页签都要能切换，且只有一个 tab 高亮 / 一个面板可见。
// 曾经漏了 tabTasks / panelTasks，「任务」页在真实扩展里点不开。
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const POPUP = fs.readFileSync(path.join(__dirname, "..", "extension", "popup.js"), "utf8");

function makeEl(tag) {
  return {
    tagName: tag, children: [], listeners: {}, className: "", title: "",
    disabled: false, style: {}, _text: "", _html: "", _attrs: {},
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
    appendChild(child) { this.children.push(child); return child; },
    setAttribute(name, value) { this._attrs[name] = String(value); },
    getAttribute(name) { return name in this._attrs ? this._attrs[name] : null; },
    set textContent(v) { this._text = String(v); },
    get textContent() { return this._text; },
    set innerHTML(v) { this._html = String(v); this.children.length = 0; },
    get innerHTML() { return this._html; },
  };
}

function makeSandbox(handler) {
  const elements = {};
  const messages = [];
  const sandbox = {
    console, setTimeout, clearTimeout,
    setInterval: () => 1, clearInterval: () => {},
    URL, URLSearchParams,
    chrome: {
      runtime: { sendMessage: (msg, cb) => { messages.push(msg); if (cb) cb(handler ? handler(msg) : undefined); } },
      storage: { local: { get(keys, cb) { cb({}); }, set() {} } },
      tabs: { query(q, cb) { cb([{ id: 3, url: "https://site/v", title: "T" }]); } },
    },
    document: {
      addEventListener() {},
      getElementById(id) { return elements[id] || (elements[id] = makeEl("div#" + id)); },
      createElement: (tag) => makeEl(tag),
    },
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(POPUP, sandbox, { filename: "popup.js" });
  return { sandbox, elements, messages };
}

const PANELS = ["capture", "media", "tasks"];
const TABS = PANELS.map((p) => "tab" + p[0].toUpperCase() + p.slice(1));
const BODIES = PANELS.map((p) => "panel" + p[0].toUpperCase() + p.slice(1));

// ① 每个页签都能切过去：tab 高亮 + 对应面板显隐，且三者互斥
{
  const { sandbox, elements, messages } = makeSandbox((msg) =>
    msg.action === "getTasks" ? { tasks: [], stats: {} } : undefined);
  PANELS.forEach((name) => {
    sandbox.showPanel(name);
    const active = TABS.filter((id) => elements[id].className.indexOf("active") >= 0);
    assert.deepStrictEqual(active, ["tab" + name[0].toUpperCase() + name.slice(1)],
                           `${name} 应只有一个 tab 高亮`);
    const shown = BODIES.filter((id) => elements[id].className === "");
    assert.deepStrictEqual(shown, ["panel" + name[0].toUpperCase() + name.slice(1)],
                           `${name} 应只有一个面板可见`);
  });

  // 切到媒体/任务页会各拉一次数据
  assert.ok(messages.some((m) => m.action === "getTasks"), "任务页要拉任务列表");

  // 非法面板名直接忽略，不把界面刷成空白
  sandbox.showPanel("capture");
  sandbox.showPanel("nope");
  assert.strictEqual(elements.tabCapture.className, "tab active");
  assert.strictEqual(elements.panelTasks.className, "hidden");
}

// ② 初始 HTML 里必须存在 panelTasks（否则切换逻辑再对也找不到节点）
{
  const html = fs.readFileSync(path.join(__dirname, "..", "extension", "popup.html"), "utf8");
  for (const id of [...TABS, ...BODIES]) {
    assert.ok(html.indexOf(`id="${id}"`) >= 0, `${id} 必须存在于 popup.html`);
  }
  assert.ok(html.indexOf('id="tabTasks"') > 0 && html.indexOf('id="panelTasks"') > 0);
}


// ④ 页签要能 Tab 到、focus-visible 要有可见焦点环，切页同步 aria-selected
{
  const html = fs.readFileSync(path.join(__dirname, "..", "extension", "popup.html"), "utf8");
  assert.ok(/:focus-visible\s*\{[^}]*outline:\s*2px solid var\(--accent\)/.test(html),
            "焦点环要落在 :focus-visible 上且用强调色令牌");
  assert.ok(html.indexOf("outline-offset: 2px") >= 0, "焦点环要偏移，别贴边");
  // 选中页签底色就是强调色，同色描边会糊在一起，得换亮一号的令牌
  assert.ok(/\.tab\.active:focus-visible\s*\{[^}]*var\(--accent2\)/.test(html));

  assert.ok(html.indexOf('role="tablist"') >= 0 && html.indexOf('aria-label="面板切换"') >= 0,
            "页签容器要有 tablist 语义");
  for (const id of TABS) {
    let seg = html.slice(html.indexOf('id="' + id + '"'));
    seg = seg.slice(0, seg.indexOf(">"));
    assert.ok(seg.indexOf('role="tab"') >= 0, id + " 要声明 role=tab");
    assert.ok(seg.indexOf("aria-controls=") >= 0, id + " 要指向对应面板");
  }
  for (const id of BODIES) {
    let seg = html.slice(html.indexOf('id="' + id + '"'));
    seg = seg.slice(0, seg.indexOf(">"));
    assert.ok(seg.indexOf('role="tabpanel"') >= 0, id + " 要声明 role=tabpanel");
    assert.ok(seg.indexOf("aria-labelledby=") >= 0, id + " 要指回页签");
  }
}

// ⑤ showPanel 要同步 aria-selected，否则读屏用户听到的还是上一个面板
{
  const { sandbox, elements } = makeSandbox((msg) =>
    msg.action === "getTasks" ? { tasks: [], stats: {} } : undefined);
  sandbox.showPanel("tasks");
  assert.strictEqual(elements.tabTasks.getAttribute("aria-selected"), "true",
                    "切到任务页后 aria-selected 要是 true");
  for (const id of ["tabCapture", "tabMedia"]) {
    assert.strictEqual(elements[id].getAttribute("aria-selected"), "false",
                      id + " 应该被取消选中");
  }
  sandbox.showPanel("capture");
  assert.strictEqual(elements.tabCapture.getAttribute("aria-selected"), "true");
  assert.strictEqual(elements.tabTasks.getAttribute("aria-selected"), "false");
}

// ⑥ 暂停/启用是同一个按钮的两种状态，aria-pressed 要跟着 updateUI 走
{
  const { sandbox, elements } = makeSandbox((msg) =>
    msg.action === "getStatus" ? { enabled: false, sentCount: 0 } : undefined);
  // loadStatus 才会把 enabled 置成后端返回值，桩里的 DOMContentLoaded 不会自动触发
  sandbox.loadStatus();
  sandbox.updateUI();
  assert.strictEqual(elements.toggleBtn.getAttribute("aria-pressed"), "false",
                    "监控被暂停时 aria-pressed 要是 false");
  assert.strictEqual(elements.toggleBtn.textContent, "启用");

  const on = makeSandbox((msg) =>
    msg.action === "getStatus" ? { enabled: true, sentCount: 3 } : undefined);
  on.sandbox.loadStatus();
  on.sandbox.updateUI();
  assert.strictEqual(on.elements.toggleBtn.getAttribute("aria-pressed"), "true",
                    "监控启用时 aria-pressed 要是 true");
  assert.strictEqual(on.elements.toggleBtn.textContent, "暂停");
}

// ⑦ 列表里的动作按钮必须带对象名：一屏几十行都写着「下载」，读屏用户听到一个裸动词
// 根本不知道落在哪一条上；可见文字会变成「已添加」，aria-label 也得跟着走。
{
  const { sandbox } = makeSandbox((msg) =>
    msg.action === "getTasks" ? { tasks: [], stats: {} } : undefined);

  const dl = sandbox.renderItem({ url: "https://cdn.example.com/a/ep1.mp4", kind: "hls", bytes: 4096 },
                                0, "https://cdn.example.com/list");
  const dlBtn = dl.children[dl.children.length - 1];
  assert.strictEqual(dlBtn.textContent, "下载");
  assert.strictEqual(dlBtn.getAttribute("aria-label"), "下载 ep1.mp4",
                    "下载按钮要报出是哪一条媒体");

  // blob: 地址取不到文件名，至少别在名字尾部留个空格
  const mse = sandbox.renderItem({ url: "blob:https://x/abc", is_mse: true, kind: "mse" },
                                 0, "https://site.com/watch?v=1");
  const parseBtn = mse.children[mse.children.length - 1];
  assert.strictEqual(parseBtn.getAttribute("aria-label"), "解析本页",
                    "解析本页按钮也要有可读名字");
  assert.strictEqual(parseBtn.getAttribute("aria-label"), parseBtn.textContent);

  // 分片型 hls：button 走解析本页分支，名字照旧从 URL 末段取
  const seg = sandbox.renderItem({ url: "https://site.com/v/master.m3u8", kind: "hls_segments", bytes: 0 },
                                 0, "https://site.com/v/master.m3u8");
  assert.strictEqual(seg.children[seg.children.length - 1].getAttribute("aria-label"),
                    "解析本页 master.m3u8");

  // 点下去之后文案会变，aria-label 必须同步，否则读屏报的还是「下载 xxx」
  dlBtn.listeners.click[0]();
  assert.strictEqual(dlBtn.getAttribute("aria-label"), "重试 ep1.mp4",
                    "失败回流到「重试」时 aria-label 也要跟着变");
  assert.strictEqual(dlBtn.textContent, "重试");

  const ok = makeSandbox((msg) =>
    msg.action === "downloadMedia" ? { success: true } : undefined);
  const okRow = ok.sandbox.renderItem({ url: "https://cdn.example.com/a/ep2.mp4", kind: "hls" },
                                      0, "https://cdn.example.com/list");
  const okBtn = okRow.children[okRow.children.length - 1];
  okBtn.listeners.click[0]();
  assert.strictEqual(okBtn.textContent, "已添加");
  assert.strictEqual(okBtn.getAttribute("aria-label"), "已添加 ep2.mp4",
                    "按钮变成「已添加」时 aria-label 仍要带着对象名");
}

// ⑧ 失败任务的重试按钮同样要带任务名
{
  const { sandbox, elements } = makeSandbox((msg) =>
    msg.action === "getTasks" ? { tasks: [], stats: {} } : undefined);
  sandbox.renderTasks({ tasks: [
    { task_id: "t1", filename: "movie.mkv", status: "failed", error: "网络中断" },
  ] });
  const row = elements.taskList.children[0];
  const retry = row.children[1];
  assert.strictEqual(retry.getAttribute("aria-label"), "重试 movie.mkv",
                    "重试按钮要报出是哪个任务");
  // 重试成功后 1.2s 还会自动刷新一次，把定时器摘掉免得测试被拖住
  const done = makeSandbox((msg) => msg.action === "retryTask" ? { success: true, retried: 1 } : undefined);
  done.sandbox.setTimeout = () => 0;
  done.sandbox.renderTasks({ tasks: [{ task_id: "t9", filename: "b.iso", status: "cancelled" }] });
  const doneBtn = done.elements.taskList.children[0].children[1];
  doneBtn.listeners.click[0]();
  assert.strictEqual(doneBtn.getAttribute("aria-label"), "已重试 b.iso",
                    "重试成功后 aria-label 要跟着文案走");
}

// ⑨ 长文件名必须在 260px 的弹窗里被截断，而不是把下载按钮顶出可见区域；
//    截断之后还要能看全，所以 label 上要有悬浮提示。
{
  const html = fs.readFileSync(path.join(__dirname, "..", "extension", "popup.html"), "utf8");
  const nameRule = html.match(/\.media-name\s*\{([^}]*)\}/);
  assert.ok(nameRule, "popup.html 要有 .media-name 规则");
  assert.ok(/min-width:\s*0/.test(nameRule[1]),
            "flex 子项不把 min-width 归零，长文件名会把整行连同下载按钮一起顶出弹窗");
  assert.ok(!/text-overflow:\s*ellipsis/.test(nameRule[1]),
            "文字在子 div 里，省略号写在这一级不生效");
  // 省略号得落在真正包着文字的那个元素上
  const kid = html.match(/\.media-name\s*>\s*div\s*\{([^}]*)\}/);
  assert.ok(kid, "省略号要写在 .media-name 的子元素上");
  assert.ok(/text-overflow:\s*ellipsis/.test(kid[1]), "子元素要能截断");
  assert.ok(/white-space:\s*nowrap/.test(kid[1]), "子元素要不换行");

  const { sandbox } = makeSandbox((msg) =>
    msg.action === "getTasks" ? { tasks: [], stats: {} } : undefined);
  const row = sandbox.renderItem({ url: "https://cdn.example.com/a-very-long-name.mp4",
                                   kind: "hls", quality_hint: "1080p", bytes: 1048576 },
                                 0, "https://cdn.example.com/list");
  assert.strictEqual(row.children[1].title, "a-very-long-name.mp4 · 1080p · 1.0 MB",
                    "截断后悬浮要给出全文（名字 + 清晰度 + 大小）");

  const trow = sandbox.taskRow({ task_id: "t1", filename: "超长的任务文件名.mkv",
                                 status: "failed", error: "HTTP 403 Forbidden" });
  assert.ok(trow.children[0].title.indexOf("超长的任务文件名.mkv") >= 0, "任务行悬浮要有文件名");
  assert.ok(trow.children[0].title.indexOf("HTTP 403 Forbidden") >= 0, "任务行悬浮要有失败原因");
}

console.log("popup.js showPanel OK");
