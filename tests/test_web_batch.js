// Web 端多选批量操作测试：从 templates/index.html 抽出批量逻辑段，
// 在 vm 里配最小 DOM/网络桩执行，覆盖：按状态过滤、选择显隐、删除确认、
// 逐个调用对应端点、完成后清空选择并主动拉取一次任务。
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const HTML = fs.readFileSync(
  path.join(__dirname, "..", "templates", "index.html"), "utf8");

const START = HTML.indexOf("// ===== 多选批量操作");
const END = HTML.indexOf("async function copyAllLinks");
assert.ok(START > 0 && END > START, "批量操作代码段必须存在");
const CODE = HTML.slice(START, END) +
  "\n;({ BATCH_STATUS, batchTargets, togglePick, clearSelection, " +
  "pruneSelection, updateSelectBar, batchAction });";

function makeEl() {
  return {
    disabled: false,
    hidden: false,
    _text: "",
    checked: false,
    dataset: {},
    set textContent(v) { this._text = String(v); },
    get textContent() { return this._text; },
  };
}

function makeSandbox(tasks, opts = {}) {
  const els = {};
  const calls = [];
  const sandbox = {
    console,
    _selected: new Set(opts.selected || []),
    _tasksById: {},
    _lastTasks: tasks,
    _confirm: opts.confirm !== false,
    document: {
      getElementById: (id) => els[id] || (els[id] = makeEl()),
      querySelectorAll: () => [],
    },
    confirm: () => sandbox._confirm,
    api: async (p, o) => {
      calls.push({ path: p, method: (o || {}).method });
      if (p === "/api/tasks") return { success: true, tasks: tasks };
      return { success: true };
    },
    showToast: (m) => calls.push({ toast: m }),
    renderTasks: (list) => calls.push({ render: list.length }),
  };
  sandbox._lastTasks.forEach(t => { sandbox._tasksById[t.task_id] = t; });
  const api_ = vm.runInNewContext(CODE, sandbox, { filename: "batch-section.js" });
  return { sandbox, api: api_, els, calls };
}

const TASKS = [
  { task_id: "d1", status: "downloading", filename: "run.iso" },
  { task_id: "p1", status: "paused", filename: "stop.iso" },
  { task_id: "f1", status: "failed", filename: "bad.iso" },
  { task_id: "c1", status: "cancelled", filename: "gone.iso" },
  { task_id: "k1", status: "completed", filename: "done.iso" },
];

// ---- 按状态过滤 ----
{
  const { api } = makeSandbox(TASKS, { selected: ["d1", "p1", "f1", "c1", "k1"] });
  assert.deepStrictEqual(Array.from(api.batchTargets("pause")).sort(), ["d1"]);
  assert.deepStrictEqual(Array.from(api.batchTargets("resume")).sort(), ["p1"]);
  assert.deepStrictEqual(Array.from(api.batchTargets("retry")).sort(), ["c1", "f1"]);
  assert.deepStrictEqual(Array.from(api.batchTargets("remove")).sort(),
                         ["c1", "d1", "f1", "k1", "p1"]);
  // 未知任务 id 不会炸，只是被过滤掉
  const { api: a2 } = makeSandbox(TASKS, { selected: ["ghost", "d1"] });
  assert.deepStrictEqual(Array.from(a2.batchTargets("pause")), ["d1"]);
  assert.deepStrictEqual(Array.from(a2.batchTargets("remove")).sort(), ["d1", "ghost"]);
}

// ---- 选择显隐 / 按钮可用性 ----
{
  const { api, sandbox, els, calls } = makeSandbox(TASKS, { selected: ["d1"] });
  api.updateSelectBar();
  assert.strictEqual(els.selectBar.hidden, false);
  assert.strictEqual(els.selectCount.textContent, "已选 1 项");
  const byId = (id) => sandbox.document.getElementById(id);
  assert.strictEqual(byId("selPause").disabled, false);   // d1 下载中 → 可暂停
  assert.strictEqual(byId("selResume").disabled, true);  // 没有 paused
  assert.strictEqual(byId("selRetry").disabled, true);   // 没有失败/取消
  assert.strictEqual(byId("selRemove").disabled, false);  // 删除不限状态

  api.updateSelectBar.call({
    _selected: new Set(),
    _tasksById: {},
  });
  // 直接调上面的沙箱副本也行，但更直观：清空后应隐藏
  api.clearSelection();
  assert.strictEqual(els.selectBar.hidden, true);
  assert.strictEqual(els.selectCount.textContent, "已选 0 项");
  assert.strictEqual(calls.length, 0);
}

// ---- togglePick / pruneSelection ----
{
  const { sandbox, api, els } = makeSandbox(TASKS);
  api.togglePick({ checked: true, dataset: { id: "f1" } });
  assert.ok(sandbox._selected.has("f1"));
  assert.strictEqual(els.selectBar.hidden, false);
  api.togglePick({ checked: false, dataset: { id: "f1" } });
  assert.ok(!sandbox._selected.has("f1"));
  assert.strictEqual(els.selectBar.hidden, true);

  // 渲染后任务消失（被删除/清除）：选择集要跟着剪枝
  sandbox._selected = new Set(["f1", "ghost"]);
  sandbox._lastTasks = TASKS.filter(t => t.task_id !== "f1");
  api.pruneSelection();
  assert.deepStrictEqual([...sandbox._selected], []);
}

// ---- batchAction: pause 只打下载中的任务 ----
(async () => {
  {
    const { api, calls, sandbox } = makeSandbox(TASKS, { selected: ["d1", "f1", "k1"] });
    await api.batchAction("pause");
    assert.deepStrictEqual(calls, [
      { path: "/api/pause/d1", method: "POST" },
      { toast: "已暂停 1 个任务" },
      { path: "/api/tasks", method: undefined },
      { render: 5 },
    ]);
    assert.strictEqual(sandbox._selected.size, 0);   // 完成后清空选择
  }
  // remove 要先确认；确认后逐个 DELETE
  {
    const { api, calls } = makeSandbox(TASKS, { selected: ["f1", "k1"] });
    await api.batchAction("remove");
    assert.deepStrictEqual(calls, [
      { path: "/api/remove/f1", method: "DELETE" },
      { path: "/api/remove/k1", method: "DELETE" },
      { toast: "已删除 2 个任务" },
      { path: "/api/tasks", method: undefined },
      { render: 5 },
    ]);
  }
  // 用户取消确认：一个请求都不能发
  {
    const { api, calls } = makeSandbox(TASKS, { selected: ["f1"], confirm: false });
    await api.batchAction("remove");
    assert.deepStrictEqual(calls, []);
  }
  // retry 只打失败/取消
  {
    const { api, calls } = makeSandbox(TASKS, { selected: ["d1", "p1", "f1", "c1"] });
    await api.batchAction("retry");
    assert.deepStrictEqual(calls.filter(c => c.path).map(c => c.path).sort(),
                           ["/api/retry/c1", "/api/retry/f1", "/api/tasks"]);
  }
  // 空选择：直接返回
  {
    const { api, calls } = makeSandbox(TASKS, { selected: [] });
    await api.batchAction("pause");
    assert.deepStrictEqual(calls, []);
  }
  // resume 只打暂停中的
  {
    const { api, calls } = makeSandbox(TASKS, { selected: ["p1", "d1"] });
    await api.batchAction("resume");
    assert.deepStrictEqual(calls.filter(c => c.path).map(c => c.path),
                           ["/api/resume/p1", "/api/tasks"]);
  }
  console.log("web batch selection OK");
})().catch(e => { console.error(e); process.exit(1); });
