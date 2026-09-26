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

console.log("popup.js showPanel OK");
