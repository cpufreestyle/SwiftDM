// Web 端定时任务可管理性测试：抽出 pendingActionHtml / renderScheduled /
// cancelScheduled 在 vm 里跑，覆盖「定时等待中的卡片有取消入口」「设置面板的
// 定时列表显示文件名并提供取消」「取消走 /api/cancel 后列表刷新」。
// 桌面端对应行为见 tests/test_desktop_ui.py 的 pending 卡片用例。
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const HTML = fs.readFileSync(
  path.join(__dirname, "..", "templates", "index.html"), "utf8");

const PENDING_START = HTML.indexOf("function pendingActionHtml");
const PENDING_END = HTML.indexOf("function createTaskCard");
assert.ok(PENDING_START > 0 && PENDING_END > PENDING_START, "pendingActionHtml 必须存在");
const PENDING_CODE = HTML.slice(PENDING_START, PENDING_END) + "\n;({ pendingActionHtml });";

const SCHED_START = HTML.indexOf("function renderScheduled");
const SCHED_END = HTML.indexOf("// Web-only 没有系统托盘");
assert.ok(SCHED_START > 0 && SCHED_END > SCHED_START, "定时列表代码段必须存在");
const SCHED_CODE = HTML.slice(SCHED_START, SCHED_END) +
  "\n;({ renderScheduled, cancelScheduled });";

// pending 卡片动作：有定时信息叫「取消定时」，否则退化「取消」
{
  const sandbox = { console };
  const api = vm.runInNewContext(PENDING_CODE, sandbox, { filename: "pending.js" });
  const withSchedule = api.pendingActionHtml({ task_id: "t1", scheduled_at: 1770000000 });
  assert.ok(withSchedule.includes("取消定时"), withSchedule);
  assert.ok(withSchedule.includes("cancelTask('t1')"), withSchedule);
  assert.ok(withSchedule.includes("btn-danger"), withSchedule);
  const plain = api.pendingActionHtml({ task_id: "t1" });
  assert.ok(plain.includes("取消</button>"), plain);
  assert.ok(!plain.includes("取消定时"), plain);
}

// renderScheduled：文件名优先，task_id 只进 title；每行带取消按钮
{
  const els = {};
  const calls = [];
  const sandbox = {
    console,
    _tasksById: { t1: { filename: "ubuntu.iso" } },
    escapeHtml: (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;"),
    api: async (p, o) => {
      calls.push({ path: p, method: (o || {}).method });
      if (p === "/api/settings") return { scheduled: [] };
      return { success: true };
    },
    showToast: (m) => calls.push({ toast: m }),
    document: {
      getElementById: (id) => els[id] || (els[id] = { _html: "", innerHTML: "", textContent: "" }),
    },
  };
  const api = vm.runInNewContext(SCHED_CODE, sandbox, { filename: "sched.js" });

  // 空列表：明说「无」，不留空砞
  api.renderScheduled([]);
  assert.strictEqual(els.scheduledList.textContent, "无");
  assert.strictEqual(els.scheduledList.innerHTML, "");

  // 有任务：文件名 + task_id 进 title + 取消按钮
  api.renderScheduled([{ task_id: "t1", start_at: 1770000000 },
                       { task_id: "t2", start_at: 1770000600 }]);
  const html = els.scheduledList.innerHTML;
  assert.ok(html.includes("ubuntu.iso"), html);
  assert.ok(html.includes('title="t1"'), "task_id 收进 title，不占正文");
  assert.ok(html.includes("ubuntu.iso") && html.includes("t2"), "未知任务退回显示 id");
  assert.ok((html.match(/cancelScheduled\(/g) || []).length === 2, html);

  // cancelScheduled：打 /api/cancel，成功后再拉一次设置刷新列表
  (async () => {
    await api.cancelScheduled("t1");
    assert.deepStrictEqual(calls, [
      { path: "/api/cancel/t1", method: "POST" },
      { toast: "已取消该定时任务" },
      { path: "/api/settings", method: undefined },
    ]);
  })().catch((err) => {
    console.error(err);
    process.exit(1);
  });
}
