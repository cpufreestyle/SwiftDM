// Web 端快捷键测试：抽出 shortcutAction 在 vm 里跑，覆盖 Ctrl+N / Ctrl+F / Esc
// 与「输入框里按普通键不触发」，并验证 runShortcut 真的操作了对应元素。
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const HTML = fs.readFileSync(
  path.join(__dirname, "..", "templates", "index.html"), "utf8");
const START = HTML.indexOf("// ===== 快捷键（与桌面端一致）=====");
const END = HTML.indexOf("function extractDropUrls");
assert.ok(START > 0 && END > START, "快捷键代码段必须存在");
const CODE = HTML.slice(START, HTML.indexOf("document.addEventListener(\"keydown\", runShortcut);"))
  + "\n;({ shortcutAction, runShortcut });";

function makeSandbox(openModals = []) {
  const focused = [];
  const selected = [];
  const classes = { detailModal: new Set(openModals.includes("detail") ? ["open"] : []),
                    settingsModal: new Set(openModals.includes("settings") ? ["open"] : []) };
  const closed = [];
  const sandbox = {
    console,
    closeDetail: () => closed.push("detail"),
    closeSettings: () => closed.push("settings"),
    document: {
      getElementById: (id) => (
        id === "urlInput" ? { focus: () => focused.push("url"),
                              select: () => selected.push("url") }
        : id === "searchInput" ? { focus: () => focused.push("search") }
        : classes[id] ? {
            classList: { contains: (c) => classes[id].has(c),
                         add: (c) => classes[id].add(c),
                         remove: (c) => classes[id].delete(c) }
          } : null),
    },
    _detailTaskId: "t1",
  };
  const api = vm.runInNewContext(CODE, sandbox, { filename: "shortcut-section.js" });
  return { api, focused, selected, classes, closed };
}

// Ctrl/⌘+N、Ctrl+F 才触发；大小写与修饰键都认
{
  const { api } = makeSandbox();
  assert.strictEqual(api.shortcutAction({ key: "n", ctrlKey: true }), "focus_url");
  assert.strictEqual(api.shortcutAction({ key: "N", ctrlKey: true }), "focus_url");
  assert.strictEqual(api.shortcutAction({ key: "n", metaKey: true }), "focus_url");
  assert.strictEqual(api.shortcutAction({ key: "f", ctrlKey: true }), "focus_search");
  // 无修饰键 / 其它组合一律不拦
  assert.strictEqual(api.shortcutAction({ key: "n" }), null);
  assert.strictEqual(api.shortcutAction({ key: "f" }), null);
  assert.strictEqual(api.shortcutAction({ key: "s", ctrlKey: true }), null);
  assert.strictEqual(api.shortcutAction(null), null);
}

// Esc：详情优先于设置，都没有则不动
{
  assert.strictEqual(makeSandbox(["detail", "settings"]).api
    .shortcutAction({ key: "Escape" }), "close_detail");
  assert.strictEqual(makeSandbox(["settings"]).api
    .shortcutAction({ key: "Escape" }), "close_settings");
  assert.strictEqual(makeSandbox([]).api.shortcutAction({ key: "Escape" }), null);
}

// runShortcut 真的聚焦/关闭
{
  const { api, focused, selected, closed } = makeSandbox(["detail"]);
  api.runShortcut(ev("n", true));
  assert.deepStrictEqual(focused, ["url"]);
  assert.deepStrictEqual(selected, ["url"]);   // 全选，方便直接覆盖粘贴
  api.runShortcut(ev("f", true));
  assert.deepStrictEqual(focused, ["url", "search"]);
  api.runShortcut(ev("Escape"));
  assert.deepStrictEqual(closed, ["detail"]);   // 只关详情，不碰设置
}

// 非快捷键按键不该被 preventDefault 拦掉
{
  const { api } = makeSandbox();
  assert.strictEqual(ev("a", true).prevented, false);
  api.runShortcut(ev("a", true));
  assert.strictEqual(ev("a", true).prevented, false);
  api.runShortcut(ev("n", true));
  assert.strictEqual(ev("a", true).prevented, false);
}

// 带 preventDefault 记录的事件工厂
function ev(key, ctrl) {
  const e = { key, ctrlKey: !!ctrl, metaKey: false, prevented: false,
              preventDefault() { this.prevented = true; } };
  return e;
}

console.log("test_web_shortcuts: all ok");
