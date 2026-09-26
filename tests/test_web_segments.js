// Web 端分段进度测试：从 templates/index.html 抽出分段逻辑段，在 vm 里配最小
// 桩执行，覆盖：每段字节区间还原、段满标记、单段/脏数据/完成态不展示。
// 注意：vm 内对象与当前 realm 原型不同，比较前用 JSON 往返归一化。
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const HTML = fs.readFileSync(
  path.join(__dirname, "..", "templates", "index.html"), "utf8");

const START = HTML.indexOf("// ===== 分段进度 =====");
const END = HTML.indexOf("function createTaskCard");
assert.ok(START > 0 && END > START, "分段进度代码段必须存在");
const CODE = HTML.slice(START, END) +
  "\n;({ segmentProgress, segmentStripHtml });";

const sandbox = { console, formatSize: (v) => String(v) };
const api = vm.runInNewContext(CODE, sandbox, { filename: "segment-section.js" });

const plain = (v) => JSON.parse(JSON.stringify(v));

// 每段实际字节区间还原（最后一段吃掉余数），进度按段内占比四舍五入向下取整
{
  const task = {
    status: "downloading",
    segments_offsets: [[0, 9], [10, 24]],
    segments_progress: [4, 10],
  };
  assert.deepStrictEqual(plain(api.segmentProgress(task)), [
    { index: 0, done: 4, total: 10, pct: 40 },
    { index: 1, done: 10, total: 15, pct: 66 },
  ]);
  const html = api.segmentStripHtml(task);
  assert.ok(html.includes("0/2 段"), html);
  assert.ok(html.includes("seg-cell"), html);
  assert.ok(html.includes("width:40%"), html);
  assert.ok(!html.includes("seg-fill done"), html);
}

// 暂停态同样展示；段满的格子标 done 并计入完成数
{
  const html = api.segmentStripHtml({
    status: "paused",
    segments_offsets: [[0, 9], [10, 19]],
    segments_progress: [10, 5],
  });
  assert.ok(html.includes("1/2 段"), html);
  assert.ok(html.includes('class="seg-fill done"'), html);
  assert.ok(html.includes("width:50%"), html);
}

// 单段 / 缺字段 / 结构异常：不展示（整体进度条已等价）
assert.deepStrictEqual(plain(api.segmentProgress(
  { segments_offsets: [[0, 9]], segments_progress: [3] })), []);
assert.deepStrictEqual(plain(api.segmentProgress(
  { segments_progress: [1, 2] })), []);
assert.deepStrictEqual(plain(api.segmentProgress(
  { segments_offsets: "oops", segments_progress: [] })), []);
assert.deepStrictEqual(plain(api.segmentProgress(null)), []);
assert.strictEqual(api.segmentStripHtml(
  { status: "completed", segments_offsets: [[0, 9], [10, 19]],
    segments_progress: [10, 10] }), "");
assert.strictEqual(api.segmentStripHtml(
  { status: "failed", segments_offsets: [[0, 9], [10, 19]] }), "");
assert.strictEqual(api.segmentStripHtml(null), "");

// 进度缺项/脏数据按 0 处理；done 超过该段总量时夹住，进度条不溢出
assert.deepStrictEqual(plain(api.segmentProgress({
  segments_offsets: [[0, 9], [10, 19]],
  segments_progress: [null, "x"],
})), [
  { index: 0, done: 0, total: 10, pct: 0 },
  { index: 1, done: 0, total: 10, pct: 0 },
]);
assert.deepStrictEqual(plain(api.segmentProgress({
  segments_offsets: [[0, 9], [10, 19]],
  segments_progress: [99, 5],
})), [
  { index: 0, done: 10, total: 10, pct: 100 },
  { index: 1, done: 5, total: 10, pct: 50 },
]);

console.log("test_web_segments: all ok");
