// Web 端快捷键测试：抽出 shortcutAction 在 vm 里跑，覆盖 Ctrl+N / Ctrl+F / Esc
// 与「输入框里按普通键不触发」，并验证 runShortcut 真的操作了对应元素。
// 另覆盖 ↑↓/Enter 的键盘导航：focusableCardIds / stepFocus / setFocusTask 与输入控件让位。
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
  + "\n;({ shortcutAction, runShortcut, stepFocus, setFocusTask, focusableCardIds });";

function makeSandbox(openModals = [], cardIds = []) {
  const focused = [];
  const selected = [];
  const classes = { detailModal: new Set(openModals.includes("detail") ? ["open"] : []),
                    settingsModal: new Set(openModals.includes("settings") ? ["open"] : []) };
  const closed = [];
  const opened = [];
  // 卡片桩：只有 id / classList / scrollIntoView，够脚本用
  const cards = cardIds.map((cid) => {
    const card = { id: "task-" + cid, scrolled: 0, _cls: new Set() };
    card.classList = { add: (c) => card._cls.add(c),
                      remove: (c) => card._cls.delete(c),
                      contains: (c) => card._cls.has(c) };
    card.scrollIntoView = () => { card.scrolled += 1; };
    return card;
  });
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
          } : (id.indexOf("task-") === 0
                 ? cards.find((c) => c.id === id) || null
                 : null)),
      querySelectorAll: (sel) => {
        if (sel.indexOf(".focused") >= 0) return cards.filter((c) => c._cls.has("focused"));
        if (sel.indexOf(".task-card") >= 0) return cards.slice();
        return [];
      },
    },
    _detailTaskId: "t1",
    _focusTaskId: null,
    openFile: (id) => opened.push(id),
  };
  const api = vm.runInNewContext(CODE, sandbox, { filename: "shortcut-section.js" });
  return { api, focused, selected, opened, cards, classes, closed, sandbox };
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

// ===== 键盘导航：↑↓ 移动焦点、Enter 打开（与桌面端一致）=====
// shortcutAction 只在不带修饰键时认这三个键，避免和 Ctrl+N/Ctrl+F 抢
{
  const { api, sandbox } = makeSandbox([], ["t1", "t2"]);
  assert.strictEqual(api.shortcutAction({ key: "ArrowUp" }), "focus_prev");
  assert.strictEqual(api.shortcutAction({ key: "ArrowDown" }), "focus_next");
  assert.strictEqual(api.shortcutAction({ key: "arrowdown" }), "focus_next"); // 大小写不敏感
  // 没有焦点任务时 Enter 不拦截（让输入框/按钮自己处理）
  assert.strictEqual(api.shortcutAction({ key: "Enter" }), null);
  sandbox._focusTaskId = "t2";
  assert.strictEqual(api.shortcutAction({ key: "Enter" }), "open_focused");
  // 带修饰键时不走键盘导航分支
  assert.strictEqual(api.shortcutAction({ key: "ArrowUp", ctrlKey: true }), null);
  assert.strictEqual(api.shortcutAction({ key: "Enter", metaKey: true }), null);
}

// stepFocus：到头钳制不循环、焦点失效落到首/尾
{
  const { api, sandbox } = makeSandbox([], ["t1", "t2", "t3"]);
  assert.deepEqual(api.focusableCardIds(), ["t1", "t2", "t3"]);  // 跨 realm 数组用 deepEqual
  assert.strictEqual(api.stepFocus(1), "t1");   // 无焦点时 ↓ 落到第一个
  assert.strictEqual(api.stepFocus(1), "t2");
  assert.strictEqual(api.stepFocus(1), "t3");
  assert.strictEqual(api.stepFocus(1), "t3");   // 到底钳制，不循环回第一个
  assert.strictEqual(api.stepFocus(-1), "t2");
  assert.strictEqual(api.stepFocus(-1), "t1");
  assert.strictEqual(api.stepFocus(-1), "t1");  // 到顶同样钳制
  // 筛选/搜索后焦点卡片不见了：↓ 落第一个，↑ 落最后一个
  sandbox._focusTaskId = "gone";
  assert.strictEqual(api.stepFocus(1), "t1");
  sandbox._focusTaskId = "gone";
  assert.strictEqual(api.stepFocus(-1), "t3");
  // 卡片已被摘掉（id 找不到）时清空焦点
  api.setFocusTask("ghost");
  assert.strictEqual(sandbox._focusTaskId, null);
}

// stepFocus：空序列清空焦点（与桌面端 _step_selection 返回 None 一致）
{
  const { api, sandbox, cards } = makeSandbox([]);
  assert.deepEqual(api.focusableCardIds(), []);
  assert.strictEqual(api.stepFocus(1), null);
  assert.strictEqual(api.stepFocus(-1), null);
  assert.strictEqual(sandbox._focusTaskId, null);
  assert.strictEqual(cards.length, 0);
}

// 轮询重建卡片后重贴焦点类名，scroll=false 不把页面拉走
{
  const { api, cards } = makeSandbox([], ["t1", "t2"]);
  api.setFocusTask("t1");
  assert.ok(cards[0]._cls.has("focused"));
  assert.ok(!cards[1]._cls.has("focused"));
  assert.strictEqual(cards[0].scrolled, 1);       // 手动切焦点要滚到可见
  api.setFocusTask("t1", false);                // 轮询重贴
  assert.ok(cards[0]._cls.has("focused"));
  assert.strictEqual(cards[0].scrolled, 1);       // 不重复滚动
  api.setFocusTask(null, false);                 // 焦点卡片被筛掉
  assert.ok(!cards[0]._cls.has("focused"));
}

// runShortcut：↑↓ 走 stepFocus，Enter 打开选中任务的文件
{
  const { api, opened, cards, sandbox } = makeSandbox([], ["t1", "t2"]);
  api.runShortcut(ev("ArrowDown"));
  assert.ok(cards[0]._cls.has("focused"));
  assert.strictEqual(sandbox._focusTaskId, "t1");
  assert.strictEqual(cards[0].scrolled, 1);
  api.runShortcut(ev("Enter"));
  assert.deepStrictEqual(opened, ["t1"]);
  // 焦点在输入类控件上时让位（与桌面端 _keyboard_nav_allowed 一致）
  for (const tag of ["INPUT", "Select", "textarea"]) {
    api.runShortcut(Object.assign(ev("ArrowDown"), { target: { tagName: tag } }));
    assert.strictEqual(sandbox._focusTaskId, "t1");
  }
  api.runShortcut(Object.assign(ev("Enter"), { target: { tagName: "input" } }));
  api.runShortcut(Object.assign(ev("Enter"),
                                { target: { tagName: "div", isContentEditable: true } }));
  assert.deepStrictEqual(opened, ["t1"]);       // 没有第二次打开
}

