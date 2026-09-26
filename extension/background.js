// SwiftDM 浏览器监控 - 后台 Service Worker
importScripts('sniff.js');

// Flask 后端端口：5000 被占用时主程序会顺延到 5002-5005；5001 是浏览器监控端口
// （只收文件捕获），因此媒体/任务类接口只按 bases 依次尝试。
const SWIFTDM_BASES = [
  'http://127.0.0.1:5000',
  'http://127.0.0.1:5002',
  'http://127.0.0.1:5003',
  'http://127.0.0.1:5004',
  'http://127.0.0.1:5005'
];
const MONITOR_CAPTURE_URL = 'http://127.0.0.1:5001/capture';
let lastGoodBase = '';

async function postJson(path, data) {
  const bases = lastGoodBase ? [lastGoodBase].concat(SWIFTDM_BASES.filter(b => b !== lastGoodBase))
                             : SWIFTDM_BASES.slice();
  for (const base of bases) {
    try {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 3000);
      const response = await fetch(base + path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
        signal: controller.signal
      });
      clearTimeout(timer);
      let result = {};
      try { result = await response.json(); } catch (e) { result = {}; }
      result.__status = response.status;
      lastGoodBase = base;
      chrome.storage.local.set({ swiftBase: base });
      return result;
    } catch (e) {
      console.debug(`[SwiftDM] ${base}${path} 连接失败:`, e.message);
    }
  }
  return null;
}

async function getJson(path) {
  const bases = lastGoodBase ? [lastGoodBase].concat(SWIFTDM_BASES.filter(b => b !== lastGoodBase))
                             : SWIFTDM_BASES.slice();
  for (const base of bases) {
    try {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 3000);
      const response = await fetch(base + path, { signal: controller.signal });
      clearTimeout(timer);
      lastGoodBase = base;
      try { return await response.json(); } catch (e) { return {}; }
    } catch (e) {
      console.debug(`[SwiftDM] ${base}${path} 连接失败:`, e.message);
    }
  }
  return null;
}

// 文件捕获端点列表（5001 只收文件捕获，放最后兜底）
const CAPTURE_URLS = SWIFTDM_BASES.map(b => b + '/api/browser-capture')
                                  .concat([MONITOR_CAPTURE_URL]);

// 存储下载历史，避免重复发送
let sentDownloads = new Set();
let enabled = true;

// 从存储中加载设置
chrome.storage.local.get(['enabled', 'sentCount'], (result) => {
  enabled = result.enabled !== false; // 默认启用
});

// 监听下载事件
chrome.downloads.onCreated.addListener(async (downloadItem) => {
  if (!enabled) return;
  if (sentDownloads.has(downloadItem.id)) return;

  const url = downloadItem.url;
  if (!url || !url.startsWith('http')) return;

  sentDownloads.add(downloadItem.id);

  // 发送到 SwiftDM（先确认它被接受，再决定是否接管，避免误删下载）
  const ok = await sendToSwiftDM({
    url: url,
    filename: downloadItem.filename || extractFilename(url),
    fileSize: downloadItem.fileSize || 0,
    mime: downloadItem.mime || '',
    referrer: downloadItem.referrer || ''
  });

  // 接管：捕获成功后取消浏览器原生下载，改由 SwiftDM 多线程加速（避免重复下载）
  if (ok) {
    try {
      chrome.downloads.cancel(downloadItem.id);
      chrome.downloads.erase({ id: downloadItem.id }); // 清掉“已中断”残留记录
    } catch (e) {
      console.debug('[SwiftDM] 取消/清除浏览器下载失败:', e);
    }
    showTakeoverNotification(downloadItem.filename || extractFilename(url));
  }
});

// 监听 webRequest —— 捕获尚未添加到下载列表的请求
const DOWNLOAD_EXTENSIONS = [
  '.zip', '.rar', '.7z', '.tar', '.gz', '.bz2', '.xz', '.iso',
  '.exe', '.msi', '.dmg', '.pkg', '.deb', '.rpm', '.apk',
  '.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm',
  '.mp3', '.aac', '.flac', '.wav', '.ogg', '.m4a',
  '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.svg',
  '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
  '.torrent', '.crx', '.xpi', '.jar'
];

const DOWNLOAD_CONTENT_TYPES = [
  'application/zip', 'application/x-rar-compressed', 'application/x-7z-compressed',
  'application/x-tar', 'application/gzip', 'application/x-bzip2',
  'application/x-msdownload', 'application/octet-stream',
  'application/x-apple-diskimage', 'application/x-iso9660-image',
  'application/x-shockwave-flash'
];

// 记录检测到的下载 URL（防止频繁重复）
let detectedUrls = new Map(); // url -> timestamp

chrome.webRequest.onHeadersReceived.addListener(
  (details) => {
    if (!enabled) return;
    if (details.type !== 'main_frame' && details.type !== 'sub_frame') return;

    const url = details.url;
    if (detectedUrls.has(url) && (Date.now() - detectedUrls.get(url)) < 30000) {
      return;
    }

    // 检查 Content-Type
    const contentType = (details.responseHeaders || []).find(
      h => h.name.toLowerCase() === 'content-type'
    );

    let isDownload = false;

    // 检查 URL 扩展名
    try {
      const urlObj = new URL(url);
      const pathname = urlObj.pathname.toLowerCase();
      if (DOWNLOAD_EXTENSIONS.some(ext => pathname.endsWith(ext))) {
        isDownload = true;
      }
    } catch(e) {}

    // 检查 Content-Type
    if (!isDownload && contentType) {
      const ct = contentType.value.toLowerCase();
      if (DOWNLOAD_CONTENT_TYPES.some(t => ct.startsWith(t))) {
        isDownload = true;
      }
      if (ct.startsWith('video/') || ct.startsWith('audio/')) {
        isDownload = true;
      }
    }

    // 检查 Content-Disposition
    if (!isDownload) {
      const disposition = (details.responseHeaders || []).find(
        h => h.name.toLowerCase() === 'content-disposition'
      );
      if (disposition && disposition.value.includes('attachment')) {
        isDownload = true;
      }
    }

    if (isDownload) {
      detectedUrls.set(url, Date.now());
      sendToSwiftDM({
        url: url,
        filename: extractFilename(url),
        fileSize: 0,
        mime: contentType ? contentType.value : '',
        referrer: details.initiator || ''
      });
    }
  },
  { urls: ['<all_urls>'] },
  ['responseHeaders']
);

// ==================== 流媒体嗅探 ====================
// 与上面的「文件下载」检测互不影响：这里看的是清单/媒体响应本身。
const sniffedSeen = new Map(); // url -> timestamp，60s 内同一 URL 不重复上报

chrome.webRequest.onHeadersReceived.addListener(
  (details) => {
    if (!enabled) return;
    if (typeof details.tabId !== 'number' || details.tabId < 0) return;
    const Sniff = self.SwiftDMSniff;
    if (!Sniff) return;

    const headers = details.responseHeaders || [];
    const header = (name) => {
      const hit = headers.find(h => h.name && h.name.toLowerCase() === name);
      return hit ? hit.value : '';
    };
    const item = Sniff.classifyResource({
      url: details.url,
      contentType: header('content-type'),
      contentDisposition: header('content-disposition'),
      status: details.status,
      contentLength: header('content-length'),
    });
    if (item) {
      const last = sniffedSeen.get(details.url) || 0;
      if (Date.now() - last < 60000) return;
      sniffedSeen.set(details.url, Date.now());
      reportMedia(details.tabId, [item]);
      return;
    }
    // HLS 常见形态：没有清单可见性（DRM 播放器/加密清单），只能靠分片流量推断
    const frag = Sniff.noteFragment(details.tabId, details.url);
    if (frag) reportMedia(details.tabId, [frag]);
  },
  { urls: ['<all_urls>'], types: ['xmlhttprequest', 'media', 'sub_frame', 'object', 'other'] },
  ['responseHeaders']
);

function reportMedia(tabId, items) {
  chrome.tabs.get(tabId, (tab) => {
    const payload = {
      tab_id: String(tabId),
      page_url: (tab && tab.url) || '',
      title: (tab && tab.title) || '',
      items: items
    };
    if (!payload.page_url) return;
    postJson('/api/media/discover', payload).then((res) => {
      if (!res) return;
      if (res.ok) console.log('[SwiftDM] 嗅到 ' + items.length + ' 个媒体资源 (tab ' + tabId + ')');
      else if (res.reason === 'disabled') console.debug('[SwiftDM] 浏览器监控已关闭，忽略嗅探');
    });
  });
}

chrome.tabs.onRemoved.addListener((tabId) => {
  self.SwiftDMSniff && self.SwiftDMSniff.resetTab(tabId);
});

// 发送到 SwiftDM（尝试多个端点）
async function sendToSwiftDM(data) {
  for (const url of CAPTURE_URLS) {
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 3000);
      
      const response = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
        signal: controller.signal
      });
      
      clearTimeout(timeout);
      
      let result = {};
      try { result = await response.json(); } catch (e) { result = {}; }
      console.log('[SwiftDM]', result.message || result);
      
      // 端点响应了但明确拒绝（如无效 URL）：不再尝试其它端点，且不接管
      if (!result.success) return false;
      
      // 更新计数
      chrome.storage.local.get(['sentCount'], (res) => {
        const count = (res.sentCount || 0) + 1;
        chrome.storage.local.set({ sentCount: count });
      });
      
      return true; // 成功捕获
    } catch (e) {
      console.debug(`[SwiftDM] ${url} 连接失败:`, e.message);
    }
  }
  // 所有端点都失败（SwiftDM 未运行）
  console.debug('[SwiftDM] SwiftDM 未运行，所有端点连接失败');
  return false;
}

// 提取文件名
function extractFilename(url) {
  try {
    const urlObj = new URL(url);
    const pathname = urlObj.pathname;
    const segments = pathname.split('/');
    const last = segments[segments.length - 1];
    if (last && last.includes('.')) {
      return decodeURIComponent(last);
    }
    // 从查询参数中找
    const params = new URLSearchParams(urlObj.search);
    for (const [key, value] of params) {
      if (value && value.includes('.')) {
        const filenameMatch = value.match(/([^/]+\.[a-zA-Z0-9]+)$/);
        if (filenameMatch) return filenameMatch[1];
      }
    }
  } catch(e) {}
  return 'download_' + Date.now();
}

// 接管成功提示
function showTakeoverNotification(filename) {
  try {
    chrome.notifications.create('swiftdm-takeover-' + Date.now(), {
      type: 'basic',
      iconUrl: 'icons/icon48.png',
      title: 'SwiftDM 已接管下载',
      message: '已取消浏览器原生下载，改由 SwiftDM 加速：\n' + filename,
      priority: 1
    });
  } catch (e) {
    console.debug('[SwiftDM] 通知失败:', e);
  }
}

// 定期清理检测缓存
setInterval(() => {
  const now = Date.now();
  for (const [url, ts] of detectedUrls) {
    if (now - ts > 60000) detectedUrls.delete(url);
  }
  // 清理已发送的下载 ID（最多保留 200 个）
  if (sentDownloads.size > 200) {
    sentDownloads = new Set([...sentDownloads].slice(-100));
  }
}, 30000);

// 监听来自 popup 的消息
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.action === 'getStatus') {
    // 把真正连通过的 base 一起给弹窗；主程序端口会从 5000 顺延到 5002-5005
    chrome.storage.local.get(['sentCount', 'swiftBase'], (result) => {
      sendResponse({
        enabled: enabled,
        sentCount: result.sentCount || 0,
        base: result.swiftBase || ''
      });
    });
    return true; // 异步响应
  }
  if (message.action === 'toggleEnabled') {
    enabled = !enabled;
    chrome.storage.local.set({ enabled: enabled });
    sendResponse({ enabled: enabled });
    return true;
  }
  if (message.action === 'resetCount') {
    chrome.storage.local.set({ sentCount: 0 });
    sendResponse({ sentCount: 0 });
    return true;
  }
  if (message.action === 'getMedia') {
    const tabId = String(message.tabId || '');
    getJson('/api/media/list?tabId=' + encodeURIComponent(tabId)).then((res) => {
      sendResponse(res || { ok: false, error: 'SwiftDM 未运行' });
    });
    return true;
  }
  if (message.action === 'getTasks') {
    getJson('/api/tasks').then((res) => {
      const payload = res || { tasks: [], stats: {}, ok: false, error: 'SwiftDM 未运行' };
      // 本次真连上才报 base，否则弹窗会显示上一次的地址，说“连着”却无数据
      sendResponse(Object.assign({}, payload, { __base: res ? (lastGoodBase || '') : '' }));
    });
    return true;
  }
  if (message.action === 'retryTask') {
    const taskId = String(message.taskId || '');
    if (!taskId) { sendResponse({ success: false, error: '缺少任务 ID' }); return true; }
    postJson('/api/retry/' + encodeURIComponent(taskId), {}).then((res) => {
      sendResponse(res || { success: false, error: 'SwiftDM 未运行' });
    });
    return true;
  }
  if (message.action === 'retryAllTasks') {
    postJson('/api/retry_all', {}).then((res) => {
      sendResponse(res || { success: false, error: 'SwiftDM 未运行' });
    });
    return true;
  }
  if (message.action === 'downloadMedia') {
    addMediaTask(message).then((res) => sendResponse(res));
    return true;
  }
  if (message.type === 'swiftdm-dom-media') {
    const tabId = sender.tab && sender.tab.id;
    if (tabId !== undefined && Array.isArray(message.items) && message.items.length) {
      reportMedia(tabId, message.items);
    }
    sendResponse({ ok: true });
    return true;
  }
});

// popup 点「下载」：把浏览器 Cookie 交给后端（仅写临时文件，绝不记录内容）
function getCookiesNetscape(pageUrl) {
  return new Promise((resolve) => {
    try {
      const host = new URL(pageUrl).hostname;
      chrome.cookies.getAll({}, (cookies) => {
        const mine = (cookies || []).filter(c => c && c.domain &&
          (host === c.domain.replace(/^\./, '') || host.endsWith('.' + c.domain.replace(/^\./, ''))));
        resolve(self.SwiftDMSniff.cookiesToNetscape(mine, pageUrl));
      });
    } catch (e) {
      resolve('');
    }
  });
}

async function addMediaTask(message) {
  const item = message.item || {};
  if (!item.url) return { success: false, error: '缺少媒体地址' };
  // 分片摘要与 MSE 源只是「嗅到了播放器」，拿单个 .ts / blob: 去下载只会得到一个坏文件
  if (item.kind === 'hls_segments') {
    return { success: false, reason: 'segments_only', error: '只嗅到了 HLS 分片流量，未拿到主清单，无法下载' };
  }
  if (item.is_mse || item.url.indexOf('blob:') === 0) {
    return { success: false, reason: 'mse_source',
             error: 'MSE 播放器接管的视频取不到直连地址，请用「解析本页」按网页解析下载' };
  }
  const kind = item.kind === 'video' ? 'auto' : (item.kind || 'auto');
  const cookies = await getCookiesNetscape(message.pageUrl || item.page_url || '');
  const payload = {
    url: item.url,
    kind: kind,
    filename: item.filename || '',
    referer: message.pageUrl || item.page_url || '',
    cookies_netscape: cookies
    // 不写 segments：由后端按设置面板的默认线程数决定，
    // 否则这里写死 8 会让那个设置对扩展推送的任务永远失效
  };
  const res = await postJson('/api/add', payload);
  if (!res) return { success: false, error: 'SwiftDM 未运行' };
  if (!res.success) return { success: false, error: res.error || '添加失败', reason: res.reason || '' };
  showTakeoverNotification(item.filename || (res.task && res.task.filename) || '流媒体');
  chrome.storage.local.get(['sentCount'], (r) => {
    chrome.storage.local.set({ sentCount: (r.sentCount || 0) + 1 });
  });
  return { success: true, task: res.task };
}
