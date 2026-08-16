// SwiftDM 浏览器监控 - 后台 Service Worker
// 依次尝试多个端点：Flask API（5000，被占用时主程序自动回退到 5002-5005），
// 以及浏览器监控服务器（5001 固定端口）
const SWIFTDM_URLS = [
  'http://127.0.0.1:5000/api/browser-capture',
  'http://127.0.0.1:5001/capture',
  'http://127.0.0.1:5002/api/browser-capture',
  'http://127.0.0.1:5003/api/browser-capture',
  'http://127.0.0.1:5004/api/browser-capture',
  'http://127.0.0.1:5005/api/browser-capture'
];

// 存储下载历史，避免重复发送
let sentDownloads = new Set();
let enabled = true;

// 从存储中加载设置
chrome.storage.local.get(['enabled', 'sentCount'], (result) => {
  enabled = result.enabled !== false; // 默认启用
  document.getElementById?.('status')?.textContent = enabled ? '已启用' : '已暂停';
});

// 监听下载事件
chrome.downloads.onCreated.addListener((downloadItem) => {
  if (!enabled) return;
  if (sentDownloads.has(downloadItem.id)) return;

  const url = downloadItem.url;
  if (!url || !url.startsWith('http')) return;

  sentDownloads.add(downloadItem.id);

  // 发送到 SwiftDM
  sendToSwiftDM({
    url: url,
    filename: downloadItem.filename || extractFilename(url),
    fileSize: downloadItem.fileSize || 0,
    mime: downloadItem.mime || '',
    referrer: downloadItem.referrer || ''
  });

  // 取消浏览器原生下载（可选：让 SwiftDM 接管）
  // chrome.downloads.cancel(downloadItem.id);
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

// 发送到 SwiftDM（尝试多个端点）
async function sendToSwiftDM(data) {
  for (const url of SWIFTDM_URLS) {
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
      
      const result = await response.json();
      console.log('[SwiftDM]', result.message || result);
      
      // 更新计数
      chrome.storage.local.get(['sentCount'], (res) => {
        const count = (res.sentCount || 0) + 1;
        chrome.storage.local.set({ sentCount: count });
      });
      
      return; // 成功则直接返回
    } catch (e) {
      console.debug(`[SwiftDM] ${url} 连接失败:`, e.message);
    }
  }
  // 所有端点都失败
  console.debug('[SwiftDM] SwiftDM 未运行，所有端点连接失败');
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
    chrome.storage.local.get(['sentCount'], (result) => {
      sendResponse({
        enabled: enabled,
        sentCount: result.sentCount || 0
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
});
