// Web 端实时速度曲线：从 templates/index.html 抽出绘制逻辑，在 vm 沙箱里做行为级验证。
// 源码级守卫（画布存在、喂样、环形缓冲、令牌取色、重画时机）见 tests/test_web_ui.py。
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const HTML = fs.readFileSync(
  path.join(__dirname, "..", "templates", "index.html"), "utf8");

const START = HTML.indexOf("// ===== 实时速度曲线（与桌面端 SpeedGraph 同一套取舍）");
const END = HTML.indexOf("// ===== SSE 实时更新");
assert.ok(START > 0 && END > START, "speed graph block must sit between renderStats and the SSE wiring");
// 状态声明已经前置到脚本开头（否则 applyTheme 会撞上 TDZ），
// 抽取时把它们拼回来＋方便模拟真实顺序。
const STATE_START = HTML.indexOf("// ===== 实时速度曲线状态");
const STATE_END = HTML.indexOf("// ===== 工具函数");
assert.ok(STATE_START > 0 && STATE_END > STATE_START,
           "speed graph state must be declared before the rest of the script");
const CODE = HTML.slice(STATE_START, STATE_END) + HTML.slice(START, END);

function makeCtx(log) {
  const rec = (name) => (...args) => { log.push([name, ...args]); };
  const ctx = {
    clearRect: rec("clearRect"),
    setTransform: rec("setTransform"),
    beginPath: rec("beginPath"),
    moveTo: rec("moveTo"),
    lineTo: rec("lineTo"),
    closePath: rec("closePath"),
    fill: rec("fill"),
    stroke: rec("stroke"),
  };
  for (const p of ["fillStyle", "strokeStyle", "lineWidth", "globalAlpha", "lineJoin", "lineCap"]) {
    let v = null;
    Object.defineProperty(ctx, p, {
      get: () => v,
      set: (nv) => { v = nv; log.push(["set:" + p, nv]); },
    });
  }
  return ctx;
}

function makeSandbox() {
  const log = [];
  const el = {
    id: "speedGraph",
    clientWidth: 96,
    clientHeight: 24,
    width: 0,
    height: 0,
    title: "",
    _ctx: makeCtx(log),
    getContext() { return this._ctx; },
  };
  const sandbox = {
    console,
    document: { getElementById: (id) => (id === "speedGraph" ? el : null) },
    getComputedStyle: () => ({
      getPropertyValue: (k) => ({ "--accent2": "#abcdef", "--border": "#123456" })[k] || "",
    }),
    requestAnimationFrame: (fn) => { sandbox.__raf = fn; return 7; },
    formatSpeed: (v) => v + " B/s",
    formatSize: (v) => v + " B",
  };
  sandbox.window = { devicePixelRatio: 2 };
  sandbox.__log = log;
  sandbox.__el = el;
  return sandbox;
}

// ---- 1. 纯几何：采样值映射到画布坐标 ----
{
  const s = makeSandbox();
  const api = vm.runInNewContext(CODE + "\n;({ speedGraphPoints });", s, { filename: "g.js" });
  assert.deepStrictEqual(Array.from(api.speedGraphPoints([], 96, 24)), [],
                         "no samples -> no points");
  assert.deepStrictEqual(Array.from(api.speedGraphPoints([0], 0, 24)), [],
                         "zero-width canvas -> no points");
  // 单个点落在右边界（最新采样），值为 0 时贴底部
  assert.deepStrictEqual(Array.from(api.speedGraphPoints([0], 96, 24), (pt) => Array.from(pt)),
                         [[96, 22]], "single sample sits at the right edge, on the bottom row");
  // 以窗口内峰值为四幅上限：最大值到顶，0 到底，中间值线性分布
  const pts = Array.from(api.speedGraphPoints([0, 1, 2], 90, 20), (pt) => Array.from(pt));
  assert.strictEqual(pts.length, 3);
  assert.strictEqual(pts[2][0], 90);
  assert.strictEqual(pts[2][1], 1, "peak touches the top row");
  assert.strictEqual(pts[0][1], 18, "zero sits on the bottom row");
  assert.ok(pts[1][1] > pts[2][1] && pts[1][1] < pts[0][1], "middle sample is in between");
}

// ---- 2. 采样环是定长的：超出窗口直接丢最早的点 ----
{
  const s = makeSandbox();
  const api = vm.runInNewContext(
    CODE + "\n;({ pushSpeedSample, speedSamples, SPEED_SAMPLES_MAX });", s, { filename: "g.js" });
  assert.strictEqual(api.SPEED_SAMPLES_MAX, 90);
  for (let i = 0; i < 200; i++) api.pushSpeedSample(i);
  assert.strictEqual(api.speedSamples.length, 90, "window stays bounded");
  assert.strictEqual(api.speedSamples[0], 110, "oldest samples are dropped");
  assert.strictEqual(api.speedSamples[89], 199);
  api.pushSpeedSample(-5);
  assert.strictEqual(api.speedSamples[api.speedSamples.length - 1], 0, "negative clamps to 0");
}

// ---- 3. 绘制：充分利用 rAF 合并，一帧最多画一次 ----
{
  const s = makeSandbox();
  const api = vm.runInNewContext(
    CODE + "\n;({ pushSpeedSample, scheduleSpeedGraph, drawSpeedGraph });", s, { filename: "g.js" });
  api.pushSpeedSample(1000);
  api.pushSpeedSample(2000);
  assert.strictEqual(typeof s.__raf, "function", "one rAF scheduled");
  assert.strictEqual(s.__log.length, 0, "no drawing before the frame runs");
  s.__raf();
  const names = s.__log.map((c) => c[0]);
  for (const need of ["setTransform", "clearRect", "beginPath", "fill", "stroke"]) {
    assert.ok(names.includes(need), "canvas must call " + need + ": " + names.join(","));
  }
  assert.strictEqual(names[0], "setTransform", "DPR transform comes first");
  assert.ok(s.__log.some((c) => c[0] === "setTransform" && c[1] === 2 && c[4] === 2),
            "devicePixelRatio=2 must scale the context");
  assert.ok(s.__log.some((c) => c[0] === "set:fillStyle" && c[1] === "#abcdef"),
            "fill uses the accent2 token value");
  assert.ok(s.__log.some((c) => c[0] === "set:strokeStyle" && c[1] === "#abcdef"),
            "stroke uses the accent2 token value");
  // 回调复位了 speedGraphRaf，下一次 push 还能再预约一帧
  api.pushSpeedSample(3000);
  assert.strictEqual(typeof s.__raf, "function", "frame can be scheduled again");
  s.__raf();
  assert.strictEqual(s.__el.title.includes("当前 3000 B/s"), true, s.__el.title);
  assert.ok(s.__el.title.includes("峰值 3000 B/s"), s.__el.title);
  assert.ok(s.__el.title.includes("最近 2 秒"), s.__el.title);
}

// ---- 4. 空采样 / 零尺寸时不得引发异常 ----
{
  const s = makeSandbox();
  s.__el.clientWidth = 0;
  const api = vm.runInNewContext(
    CODE + "\n;({ pushSpeedSample, drawSpeedGraph });", s, { filename: "g.js" });
  api.pushSpeedSample(500);
  assert.strictEqual(s.__log.length, 0, "zero-size canvas draws nothing");
}

console.log("web speed graph: ok");
