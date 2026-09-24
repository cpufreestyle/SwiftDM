// DOM 层嗅探：抓 <video>/<source> 的地址（清单常在网络层已经见过，这里补漏 + 标出 MSE）。
(function () {
  if (window !== window.top) return;          // 只在顶层框架跑，避免同一页面 N 份上报
  const S = window.SwiftDMSniff;
  if (!S) return;

  let timer = null;
  const sent = new Set();

  function collect() {
    const items = [];
    const nodes = document.querySelectorAll('video, audio, source');
    nodes.forEach((el) => {
      const url = el.currentSrc || el.src || '';
      if (!url) return;
      if (S.isMseUrl(url)) {
        items.push({ url: url, kind: 'mse', quality_hint: '', bytes: 0, is_mse: true });
        return;
      }
      if (!/^https?:/i.test(url)) return;
      const guess = S.classifyResource({ url: url });
      items.push(guess || {
        url: url,
        kind: /\.(m3u8)(\?|$)/i.test(url) ? 'hls' : (/\.mpd(\?|$)/i.test(url) ? 'dash' : 'video'),
        quality_hint: S.qualityFromUrl(url),
        bytes: 0,
        is_mse: false,
      });
    });
    return items.filter((it) => {
      const key = it.url;
      if (sent.has(key)) return false;
      sent.add(key);
      return true;
    });
  }

  function flush() {
    const items = collect();
    if (!items.length) return;
    try { chrome.runtime.sendMessage({ type: 'swiftdm-dom-media', items: items }); } catch (e) {}
  }

  function schedule() {
    if (timer) return;
    timer = setTimeout(() => { timer = null; flush(); }, 1500);
  }

  window.addEventListener('load', schedule, true);
  document.addEventListener('play', schedule, true);
  document.addEventListener('loadedmetadata', schedule, true);
  const observer = new MutationObserver(schedule);
  observer.observe(document.documentElement, { childList: true, subtree: true, attributes: true,
                                               attributeFilter: ['src'] });
  setTimeout(() => observer.disconnect(), 60000);   // 一分钟后再不扫 DOM，省资源
  schedule();
})();
