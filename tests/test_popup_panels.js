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
    disabled: false, style: {}, _text: "", _html: "",
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

console.log("popup.js showPanel OK");
