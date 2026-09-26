// DOM id 交叉校验：JS 里 getElementById 引用的节点必须真实存在于 HTML，
// 反之 HTML 里的 id 也不该是死节点（改名字改漏一边时表现为「点了没反应」）。
// 动态拼接的 id 单独列出来核对，漏一个就是一个静默失效的功能。
const assert = require("assert");
const fs = require("fs");
const path = require("path");

function idsOf(html) {
  return new Set([...html.matchAll(/id="([^"]+)"/g)].map((m) => m[1]));
}
function staticRefs(js) {
  const refs = new Set();
  for (const m of js.matchAll(/getElementById\((['"])([^'"]+)\1\)/g)) refs.add(m[2]);
  return refs;
}

function check(name, html, js, dynamicIds) {
  const ids = idsOf(html);
  const missing = [...staticRefs(js)].filter((r) => !ids.has(r));
  assert.deepStrictEqual(missing, [], `${name}: JS 引用了不存在的 id`);
  const unused = [...ids].filter((i) => !staticRefs(js).has(i));
  // 死节点只提示不失败：模板里可能预留/由 CSS 使用
  dynamicIds.forEach((id) => assert.ok(ids.has(id), `${name}: 动态 id ${id} 必须存在`));
  console.log(`${name}: ${ids.size} ids ok` + (unused.length ? `（未静态引用: ${unused.join(", ")}）` : ""));
  return unused;
}

const popupJs = fs.readFileSync(path.join(__dirname, "..", "extension", "popup.js"), "utf8");
const popupHtml = fs.readFileSync(path.join(__dirname, "..", "extension", "popup.html"), "utf8");
check("popup", popupHtml, popupJs,
      ["tabCapture", "tabMedia", "tabTasks",
       "panelCapture", "panelMedia", "panelTasks"]);

const web = fs.readFileSync(path.join(__dirname, "..", "templates", "index.html"), "utf8");
const webJs = [...web.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map((m) => m[1]).join("\n");
// 批量操作按钮 sel+Action、过滤计数 fc-+key 都是运行期拼出来的
check("web", web, webJs,
      ["selPause", "selResume", "selRetry", "selRemove",
       "fc-all", "fc-active", "fc-completed", "fc-failed"]);

// popup 的页签/面板必须成套：少一个就整页点不开（曾经踩过）
for (const p of ["capture", "media", "tasks"]) {
  const cap = p[0].toUpperCase() + p.slice(1);
  for (const prefix of ["tab", "panel"]) {
    assert.ok(popupJs.includes(`'${prefix}${cap}'`) || popupHtml.includes(`id="${prefix}${cap}"`),
      `popup 缺少 ${prefix}${cap}`);
  }
}

console.log("dom ids: all ok");
