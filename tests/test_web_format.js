// Web ??????????? main_window.format_size/format_speed??? popup.js
// ???????? formatSpeed ???? formatSize????????????
// "??/s"?formatSize(0) ????????????????????
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const HTML = fs.readFileSync(
  path.join(__dirname, "..", "templates", "index.html"), "utf8");

const START = HTML.indexOf("function formatSize");
const END = HTML.indexOf("function showToast");
assert.ok(START > 0 && END > START, "formatSize/formatSpeed must exist");
const CODE = HTML.slice(START, END) +
  "\n;({ formatSize, formatSpeed });";

const sandbox = { console };
const api = vm.runInNewContext(CODE, sandbox, { filename: "format.js" });

assert.strictEqual(api.formatSize(0), "未知", "大小为 0 代表未知");
assert.strictEqual(api.formatSize(1048576), "1.0 MB");
assert.strictEqual(api.formatSize(512), "512 B");
assert.strictEqual(api.formatSpeed(0), "0 B/s", "空闲速度应为 0");
assert.strictEqual(api.formatSpeed(1536), "1.5 KB/s");
assert.strictEqual(api.formatSpeed(1048576), "1.0 MB/s");

// ?? popup ? formatSize ????? Web ???TB ???? Web ??
const POPUP = fs.readFileSync(
  path.join(__dirname, "..", "extension", "popup.js"), "utf8");
const units = [...POPUP.matchAll(/const units = \[([^\]]+)\]/g)].map(m => m[1]);
assert.ok(units.length > 0, "popup.js must declare a units table");
for (const table of units) {
  assert.ok(table.includes("'TB'"), "popup units missing TB: " + table);
}

console.log("web format helpers: ok");
