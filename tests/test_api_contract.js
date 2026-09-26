const assert = require("assert");
const fs = require("fs");
const path = require("path");

const web = fs.readFileSync(path.join(__dirname, "..", "templates", "index.html"), "utf8");
const ext = ["background.js", "popup.js"].map(f =>
  fs.readFileSync(path.join(__dirname, "..", "extension", f), "utf8")).join("\n");

const routes = [...fs.readFileSync(path.join(__dirname, "..", "app.py"), "utf8")
  .matchAll(/@app\.route\(\s*"([^"]+)"/g)].map(m => m[1]);
// 路由参数两种写法归一：Flask 的 /api/pause/<task_id> 与前端的 /api/pause/${id}
const norm = (p) => p.replace(/\/<[^>]+>/g, "/<id>").replace(/\$\{[^}]+\}/g, "<id>");

function collect(src) {
  const set = new Set();
  // '/api/retry/' + id 这类拼接：引号后面紧跟 + 说明参数是拼上去的，
  // 归一成 <id>，否则会被误判成「调用了不存在的路由」
  const push = (m) => {
    // 只紧跟其后的 + 才算拼接，晚了就是别的代码里的加号；
    // 有的模式带上了收尾引号、有的没有，先剥掉再判断
    let after = src.slice(m.index + m[0].length, m.index + m[0].length + 8);
    after = after.replace(/^['"`]/, "");
    set.add(/^\s*\+/.test(after) ? m[1] + "<id>" : m[1]);
  };
  for (const m of src.matchAll(/(?:api|fetch)\(\s*[`"']([^`"']+)[`"']/g)) push(m);
  for (const m of src.matchAll(/getJson\(['"`]([^'"`]+)/g)) push(m);
  for (const m of src.matchAll(/postJson\(['"`]([^'"`]+)/g)) push(m);
  return set;
}

for (const [name, src] of [["web", web], ["extension", ext]]) {
  const missing = [];
  for (const call of collect(src)) {
    const path_ = call.split("?")[0];
    if (!routes.some((r) => norm(r) === norm(path_))) missing.push(call);
  }
  assert.deepStrictEqual(missing, [], `${name}: 调用了不存在的后端路由`);
  console.log(`${name}: all API calls resolve to real routes`);
}

// 失败原因提示两端必须同源（media._MEDIA_HINTS 是唯一事实来源，
// 桌面端 _reason_hint 直接读它）。后端会下发 download_failed，两边都必须有键。
const py = fs.readFileSync(path.join(__dirname, "..", "media.py"), "utf8");
const pyStart = py.indexOf("_MEDIA_HINTS = {");
const pyBlock = py.slice(pyStart, py.indexOf("}", pyStart));
const pyKeys = [...pyBlock.matchAll(/^\s{4}"?([a-z_]+)"?\s*:/gm)].map(m => m[1]);
const jsStart = web.indexOf("const REASON_HINTS = {");
const jsBlock = web.slice(jsStart, web.indexOf("};", jsStart));
const jsKeys = [...jsBlock.matchAll(/^\s*([a-z_]+):/gm)].map(m => m[1]);
assert.ok(pyKeys.length > 0 && jsKeys.length > 0, "两边都要有 REASON_HINTS");
assert.deepStrictEqual(jsKeys.slice().sort(), pyKeys.slice().sort(),
  "Web 端 REASON_HINTS 与 media._MEDIA_HINTS 键不一致");
console.log("REASON_HINTS in sync:", jsKeys.join(", "));

console.log("api + hints: all ok");
