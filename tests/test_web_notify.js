// 完成提示音（浏览器侧）：从 templates/index.html 抽出音序部分，用伪
// AudioContext 驱动，覆盖：none 不出声、beep 是 880/660/880
// 三声、system 是一高一低两声、锁定逻辑与失败静默。
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const HTML = fs.readFileSync(
  path.join(__dirname, "..", "templates", "index.html"), "utf8");

// 从 let notifySoundCache 开始剪：这样 setSound 才能改到同一个词法作用域里的值
const START = HTML.indexOf('let notifySoundCache = "none";');
const END = HTML.indexOf("function notifyNewCompletions");
assert.ok(START > 0 && END > START, "提示音代码段必须存在");
const CODE = HTML.slice(START, END) + "\n" +
  // 把私有缓存暴露成一个 setter，方便测试切换声音选项
  "function __setSound(v) { notifySoundCache = v; }\n" +
  ";({ playCompletionSound, unlockCompletionAudio, completionSoundKind," +
  " setSound: __setSound });";

function fakeAudioContext(tones, opts) {
  opts = opts || {};
  return function AudioContext() {
    if (opts.boom) throw new Error("no audio device");
    const ctx = {
      state: opts.suspended ? "suspended" : "running",
      currentTime: 0,
      resume() { ctx.state = "running"; return Promise.resolve(); },
      createOscillator() {
        const osc = {
          type: "",
          frequency: { value: 0 },
          connect(n) { return n; },
          start(t) {
            if (opts.boomOnStart) throw new Error("broken audio");
            tones.push({ freq: osc.frequency.value, at: t });
          },
          stop() {},
        };
        return osc;
      },
      createGain() {
        const gain = { gain: { setValueAtTime() {}, exponentialRampToValueAtTime() {} } };
        gain.connect = (n) => n;
        return gain;
      },
    };
    return ctx;
  };
}

function setup(sound, opts) {
  const tones = [];
  const listeners = {};
  const sandbox = {
    console,
    window: { AudioContext: fakeAudioContext(tones, opts) },
    document: { addEventListener: (ev, fn) => { listeners[ev] = fn; } },
  };
  const api = vm.runInNewContext(CODE, sandbox, { filename: "notify-sound.js" });
  api.setSound(sound);
  return { api, tones, listeners };
}

// 默认关闭：setSound 之前不应该有声音
{
  const { api, tones, listeners } = setup("none");
  assert.strictEqual(api.completionSoundKind(), "none");
  api.unlockCompletionAudio();
  api.playCompletionSound();
  assert.deepStrictEqual(tones, []);
  // 锁定侦听要注册，否则第一次任务完成永远没声音
  assert.ok(listeners.pointerdown && listeners.keydown, "缺少解锁侦听");
  listeners.pointerdown();
  assert.strictEqual(api.completionSoundKind(), "none");
}

// beep：880/660/880 三声短音，与桌面端 winsound 一致
{
  const { api, tones } = setup("beep");
  assert.strictEqual(api.completionSoundKind(), "beep");
  api.unlockCompletionAudio();
  api.playCompletionSound();
  assert.deepStrictEqual(tones.map(t => t.freq), [880, 660, 880]);
  const ats = tones.map(t => t.at);
  assert.deepStrictEqual(ats.slice().sort((a, b) => a - b), ats, "必须按顺序排");
  assert.ok(ats[1] > ats[0] && ats[2] > ats[1], "三声之间有间隔");
}

// system：一高一低两声，与 beep 可区分
{
  const { api, tones } = setup("system");
  api.unlockCompletionAudio();
  api.playCompletionSound();
  assert.deepStrictEqual(tones.map(t => t.freq), [587.33, 440]);
}

// 暂停的上下文要 resume；重复解锁不应该再建一个
{
  const tones = [];
  let built = 0;
  class Ctx {
    constructor() { built++; this.state = "suspended"; this.currentTime = 0;
      this.resume = () => { this.state = "running"; return Promise.resolve(); }; }
    createOscillator() { return { frequency: { value: 0 }, connect(n) { return n; },
      start(t) { tones.push(t); }, stop() {} }; }
    createGain() { const g = { gain: { setValueAtTime() {},
      exponentialRampToValueAtTime() {} } }; g.connect = (n) => n; return g; }
  }
  const sandbox = { console, window: { AudioContext: Ctx }, document: { addEventListener() {} } };
  const api = vm.runInNewContext(CODE, sandbox, { filename: "notify-sound.js" });
  api.unlockCompletionAudio();
  api.unlockCompletionAudio();
  assert.strictEqual(built, 1, "重复解锁不应该重建上下文");
  assert.strictEqual(sandbox.window.AudioContext ? "x" : "x", "x");
}

// 音频异常必须静默；不能带崩下载提示
{
  const { api } = setup("beep", { boomOnStart: true });
  api.unlockCompletionAudio();
  assert.doesNotThrow(() => api.playCompletionSound());
}
{
  const { api } = setup("beep", { boom: true });
  assert.doesNotThrow(() => api.unlockCompletionAudio());
  assert.doesNotThrow(() => api.playCompletionSound());
}

// 任务真的完成时才播放；首帧只记录不响
{
  const src = HTML.slice(HTML.indexOf("function notifyNewCompletions"));
  const body = src.slice(0, src.indexOf("\n}"));
  assert.ok(/if \(fresh\.length\) playCompletionSound\(\);/.test(body),
    "完成提示音必须挂在 notifyNewCompletions 里");
  assert.ok(body.indexOf("_knownDoneIds === null") >= 0 &&
            body.indexOf("return;") >= 0, "首帧仍然只记录不提示");
}

console.log("test_web_notify: all ok");
