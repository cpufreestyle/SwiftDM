// Web 端任务详情测试：从 templates/index.html 抽出详情逻辑段（含分段进度），
// 在 vm 里配最小桩执行，覆盖：行数据与桌面端同源、纯文本导出、HTML 转义。
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const HTML = fs.readFileSync(
  path.join(__dirname, "..", "templates", "index.html"), "utf8");

// 详情段紧跟分段进度段（后者被详情复用），一起抽出执行
const START = HTML.indexOf("// ===== 分段进度 =====");
const END = HTML.indexOf("function createTaskCard");
assert.ok(START > 0 && END > START, "任务详情代码段必须存在");
assert.ok(HTML.indexOf("// ===== 任务详情 =====") > START,
          "详情段必须位于分段进度段之后");
const CODE = HTML.slice(START, END) +
  "\n;({ detailRows, detailText, detailStatusText, detailSizeText, segmentStripHtml," +
  " detailStateActionsHtml, renderDetail });";

// renderDetail 往这两个节点塞 HTML，用可记录的桩接住
const _rendered = { title: null, body: null, actions: null };
function _stub(id) {
  return {
    set textContent(v) { if (id === "detailTitle") _rendered.title = v; },
    get textContent() { return ""; },
    set innerHTML(v) {
      if (id === "detailBody") _rendered.body = v;
      if (id === "detailActions") _rendered.actions = v;
    },
    get innerHTML() { return ""; },
  };
}

const sandbox = {
  console,
  formatSize: (v) => `${v}B`,
  formatSpeed: (v) => `${v}B/s`,
  REASON_HINTS: { needs_ffmpeg: "缺少 ffmpeg" },
  escapeHtml: (s) => String(s).replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#39;"),
  _tasksById: {},
  document: { getElementById: (id) => _stub(id) },
};
const api = vm.runInNewContext(CODE, sandbox, { filename: "detail-section.js" });
const plain = (v) => JSON.parse(JSON.stringify(v));

// 直链任务：共有字段各一行，数值格式与桌面端一致
{
  const rows = plain(api.detailRows({
    task_id: "t1", filename: "movie.bin", url: "https://s/m.bin",
    save_dir: "C:/dl", status: "downloading", progress: 42.55,
    total_size: 10485760, downloaded: 4294967, speed: 1048576, eta: "6s",
    kind: "http",
  }));
  assert.deepStrictEqual(rows.find(r => r[0] === "文件名"), ["文件名", "movie.bin"]);
  assert.deepStrictEqual(rows.find(r => r[0] === "任务 ID"), ["任务 ID", "t1"]);
  assert.deepStrictEqual(rows.find(r => r[0] === "进度"), ["进度", "42.5%"]);
  assert.deepStrictEqual(rows.find(r => r[0] === "已下载 / 总大小"),
                         ["已下载 / 总大小", "4294967B / 10485760B"]);
  assert.deepStrictEqual(rows.find(r => r[0] === "下载速度"),
                         ["下载速度", "1048576B/s"]);
  assert.deepStrictEqual(rows.find(r => r[0] === "保存目录"), ["保存目录", "C:/dl"]);
  assert.ok(!rows.some(r => r[0] === "传输协议"));
}

// 磁力任务：协议与种子/同伴
{
  const rows = plain(api.detailRows({
    task_id: "t2", filename: "linux.iso", url: "magnet:?xt=urn:btih:0",
    status: "downloading", kind: "torrent", protocol: "BitTorrent",
    seeds: 3, peers: 7, total_size: 1000, downloaded: 50,
  }));
  assert.deepStrictEqual(rows.find(r => r[0] === "传输协议"),
                         ["传输协议", "BitTorrent"]);
  assert.deepStrictEqual(rows.find(r => r[0] === "种子 / 同伴"),
                         ["种子 / 同伴", "3 / 7"]);
}

// 失败任务：失败原因 + 处理建议；无链接不占行
{
  const rows = plain(api.detailRows({
    task_id: "t3", filename: "v.mp4", status: "failed",
    error: "boom", error_reason: "needs_ffmpeg",
  }));
  assert.deepStrictEqual(rows.find(r => r[0] === "失败原因"), ["失败原因", "boom"]);
  assert.deepStrictEqual(rows.find(r => r[0] === "处理建议"),
                         ["处理建议", "缺少 ffmpeg"]);
  assert.ok(!rows.some(r => r[0] === "下载链接"));
}

// 大小未知与空值兜底
assert.strictEqual(api.detailSizeText({ total_size: 0, downloaded: 2048 }),
                   "2048B（总大小未知）");
assert.strictEqual(api.detailSizeText({ total_size: 0, downloaded: 0 }), "-");
assert.deepStrictEqual(plain(api.detailRows({})).find(r => r[0] === "文件名"),
                       ["文件名", "-"]);

// 定时等待与自动重试倒计时接在状态后面
assert.strictEqual(api.detailStatusText(
  { status: "pending", scheduled_at: 1 }), "⏰ 定时等待");
assert.ok(/↻ \d+s 后自动重试/.test(api.detailStatusText(
  { status: "failed", auto_retry_at: Date.now() / 1000 + 30 })));

// 纯文本导出：一行一条，便于反馈问题时粘贴
{
  const text = api.detailText({ task_id: "t1", filename: "a.bin",
                                status: "completed", progress: 100,
                                total_size: 10, downloaded: 10 });
  assert.ok(text.startsWith("文件名: a.bin\n任务 ID: t1\n"), text);
  assert.ok(text.includes("状态: ✓ 完成"), text);
}

// 详情里也带分段进度（多段才渲染）
{
  const html = api.segmentStripHtml({
    status: "downloading", segments_offsets: [[0, 9], [10, 19]],
    segments_progress: [5, 5],
  });
  assert.ok(html.includes("seg-strip"), html);
  assert.strictEqual(api.segmentStripHtml(
    { status: "completed", segments_offsets: [[0, 9], [10, 19]],
      segments_progress: [10, 10] }), "");
}

// 详情面板的状态动作：与卡片行同一套动作、同一套状态取舍
{
  const dl = plain(api.detailStateActionsHtml({ task_id: "t1", status: "downloading" }));
  assert.ok(dl.indexOf("pauseTask('t1')") >= 0, "下载中要能暂停");
  assert.ok(dl.indexOf("cancelTask('t1')") >= 0, "下载中要能取消");
  assert.ok(dl.indexOf("retryTask") < 0, "下载中不该出现重试");

  const pz = plain(api.detailStateActionsHtml({ task_id: "t1", status: "paused" }));
  assert.ok(pz.indexOf("resumeTask('t1')") >= 0, "已暂停要能继续");
  assert.ok(pz.indexOf("cancelTask('t1')") >= 0, "已暂停要能取消");

  const fl = plain(api.detailStateActionsHtml({ task_id: "t1", status: "failed" }));
  assert.ok(fl.indexOf("retryTask('t1')") >= 0, "失败要能重试");
  assert.ok(fl.indexOf("removeTask") < 0, "详情面板不该提供删除（避免误触，删除留在卡片行）");

  const cn = plain(api.detailStateActionsHtml({ task_id: "t1", status: "cancelled" }));
  assert.ok(cn.indexOf("retryTask('t1')") >= 0, "已取消要能重试");

  const cp = plain(api.detailStateActionsHtml({ task_id: "t1", status: "completed" }));
  assert.ok(cp.indexOf("openFile('t1')") >= 0, "已完成要能打开文件");
  assert.ok(cp.indexOf("openFolder('t1')") < 0, "打开文件夹由静态按钮承担，别重复");

  const pd = plain(api.detailStateActionsHtml({ task_id: "t1", status: "pending" }));
  assert.strictEqual(pd, "", "等待中的定时任务没有可用的状态动作");
}

// renderDetail 要把状态动作拼进 detailActions，静态按钮一个都不能丢
{
  api.renderDetail({ task_id: "t9", filename: "a.bin", status: "failed",
                     url: "https://s/a.bin", error: "boom" });
  assert.ok(_rendered.actions.indexOf("retryTask('t9')") >= 0,
            "失败任务打开详情要看到重试按钮");
  for (const needle of ["openFolder('t9')", "copyDetail('t9')",
                        "copyLink('t9')", "closeDetail()"]) {
    assert.ok(_rendered.actions.indexOf(needle) >= 0,
              "详情面板原有的 " + needle + " 不能被挤掉");
  }
  // 成功任务：打开文件，且不该残留重试
  api.renderDetail({ task_id: "t8", filename: "b.bin", status: "completed" });
  assert.ok(_rendered.actions.indexOf("openFile('t8')") >= 0, "已完成要能打开");
  assert.ok(_rendered.actions.indexOf("retryTask") < 0, "已完成不该出现重试");
}

console.log("test_web_detail: all ok");
