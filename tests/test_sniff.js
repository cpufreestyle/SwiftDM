const assert = require("assert");
const S = require("../extension/sniff.js");

// --- classifyResource ---
assert.strictEqual(S.classifyResource({ url: "https://c/a/index.m3u8" }).kind, "hls");
assert.strictEqual(S.classifyResource({ url: "https://c/a/index.m3u8" }).is_mse, false);
assert.strictEqual(S.classifyResource({ url: "https://c/a/manifest.mpd" }).kind, "dash");
assert.strictEqual(
  S.classifyResource({ url: "https://c/a/x", contentType: "application/vnd.apple.mpegurl" }).kind,
  "hls");
assert.strictEqual(
  S.classifyResource({ url: "https://c/a/x", contentType: "application/dash+xml" }).kind, "dash");
assert.strictEqual(
  S.classifyResource({ url: "https://c/a/x", contentType: "video/mp4" }).kind, "video");
assert.strictEqual(
  S.classifyResource({ url: "https://c/a/seg-1.ts", contentType: "video/mp2t" }), null,
  "单个 ts 分片不该当成资源");
assert.strictEqual(S.classifyResource({ url: "https://c/a/style.css", contentType: "text/css" }), null);
assert.strictEqual(S.classifyResource({ url: "https://c/a/x.mp4", status: 404 }), null);
assert.strictEqual(
  S.classifyResource({ url: "https://c/a/x.mp4", contentDisposition: "attachment; filename=\"a.mp4\"" }),
  null, "attachment 属于文件下载，交给 capture 通道");

// --- quality / size hints ---
assert.strictEqual(S.qualityFromUrl("https://c/1080p/prog_index.m3u8"), "1080P");
assert.strictEqual(S.qualityFromUrl("https://c/n_720p/master.mpd"), "720P");
assert.strictEqual(S.qualityFromUrl("https://c/master.m3u8"), "");
assert.strictEqual(
  S.classifyResource({
    url: "https://c/av/index.m3u8?x-collection=2160", contentType: "application/vnd.apple.mpegurl",
  }).quality_hint, "");

// --- MSE ---
assert.strictEqual(S.isMseUrl("blob:https://site/9c0b"), true);
assert.strictEqual(S.isMseUrl("mediastream:abc"), true);
assert.strictEqual(S.isMseUrl("https://c/a.m3u8"), false);

// --- fragment counting（只用于展示，清单未知所以不可下载） ---
const tab = "42";
for (let i = 0; i < 4; i++) {
  assert.strictEqual(S.noteFragment(tab, `https://c/seg/${i}.ts`), null, "未达阈值");
}
const hit = S.noteFragment(tab, "https://c/seg/4.ts");
assert.ok(hit, "第 5 个分片应给出摘要条目");
assert.strictEqual(hit.kind, "hls_segments");
assert.strictEqual(hit.url, "https://c/seg/4.ts");
assert.strictEqual(hit.quality_hint, "5 个分片");
assert.strictEqual(S.noteFragment(tab, "https://c/seg/4.ts"), null, "重复片段不再触发");
assert.strictEqual(S.noteFragment(tab, "https://c/seg/3.ts"), null, "已计过的片段不再触发");
const grown = S.noteFragment(tab, "https://c/seg/5.ts");
assert.strictEqual(grown.quality_hint, "6 个分片", "新增分片应刷新计数");
S.resetTab(tab);
assert.strictEqual(S.noteFragment(tab, "https://c/seg/0.ts"), null, "reset 后重新计数");

// --- cookies → Netscape ---
const cookies = [
  { name: "sid", value: "abc", domain: ".example.com", path: "/", secure: false, httpOnly: false, expirationDate: 0 },
  { name: "token", value: "xyz", domain: "api.example.com", path: "/v1", secure: true, httpOnly: true, expirationDate: 1893456000 },
  { name: "other", value: "no", domain: ".other.com", path: "/", secure: false, httpOnly: false, expirationDate: 0 },
];
const text = S.cookiesToNetscape(cookies, "https://api.example.com/watch?v=1");
const lines = text.split("\n").filter((l) => l && !l.startsWith("# Netscape") && !l.startsWith("# Generated"));
assert.strictEqual(lines.length, 2, "只保留作用域匹配的 Cookie");
assert.strictEqual(lines[0], ".example.com\tTRUE\t/\tFALSE\t0\tsid\tabc");
assert.strictEqual(lines[1], "#HttpOnly_api.example.com\tFALSE\t/v1\tTRUE\t1893456000\ttoken\txyz");
assert.ok(text.startsWith("# Netscape HTTP Cookie File"));
assert.strictEqual(S.cookiesToNetscape([], "not a url"), "");

console.log("sniff.js OK");
