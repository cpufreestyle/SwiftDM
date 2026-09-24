// SwiftDM 扩展弹窗逻辑
let enabled = true;
let activeTabId = null;

document.addEventListener('DOMContentLoaded', () => {
  loadStatus();
  document.getElementById('toggleBtn').addEventListener('click', () => {
    chrome.runtime.sendMessage({ action: 'toggleEnabled' }, (response) => {
      if (response) { enabled = response.enabled; updateUI(); }
    });
  });
  document.getElementById('resetBtn').addEventListener('click', () => {
    chrome.runtime.sendMessage({ action: 'resetCount' }, () => {
      document.getElementById('sentCount').textContent = '0';
    });
  });
  document.getElementById('tabCapture').addEventListener('click', () => showPanel('capture'));
  document.getElementById('tabMedia').addEventListener('click', () => showPanel('media'));
  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    const tab = tabs && tabs[0];
    if (!tab) return;
    activeTabId = tab.id;
    document.getElementById('mediaPage').textContent = tab.title || tab.url || '当前标签页';
    loadMedia();
  });
});

function showPanel(which) {
  document.getElementById('tabCapture').className = 'tab' + (which === 'capture' ? ' active' : '');
  document.getElementById('tabMedia').className = 'tab' + (which === 'media' ? ' active' : '');
  document.getElementById('panelCapture').className = which === 'capture' ? '' : 'hidden';
  document.getElementById('panelMedia').className = which === 'media' ? '' : 'hidden';
  if (which === 'media') loadMedia();
}

function loadStatus() {
  chrome.runtime.sendMessage({ action: 'getStatus' }, (response) => {
    if (!response) return;
    enabled = response.enabled;
    document.getElementById('sentCount').textContent = response.sentCount || 0;
    updateUI();
  });
}

function updateUI() {
  const dot = document.getElementById('statusDot');
  const text = document.getElementById('statusText');
  const btn = document.getElementById('toggleBtn');
  if (enabled) {
    dot.className = 'status-dot active';
    text.textContent = '已启用';
    btn.textContent = '暂停';
  } else {
    dot.className = 'status-dot inactive';
    text.textContent = '已暂停';
    btn.textContent = '启用';
  }
}

function formatSize(bytes) {
  if (!bytes || bytes <= 0) return '';
  const units = ['B', 'KB', 'MB', 'GB'];
  let i = 0, v = bytes;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return v.toFixed(i === 0 ? 0 : 1) + ' ' + units[i];
}

function loadMedia() {
  if (activeTabId === null) return;
  chrome.runtime.sendMessage({ action: 'getMedia', tabId: activeTabId }, (res) => {
    const box = document.getElementById('mediaList');
    if (!res || !res.ok) {
      box.innerHTML = '<div class="media-empty">连不上 SwiftDM，确认桌面端已启动</div>';
      document.getElementById('mediaCount').textContent = '0';
      return;
    }
    const items = (res.items || []).slice().sort((a, b) => (b.bytes || 0) - (a.bytes || 0));
    document.getElementById('mediaCount').textContent = String(items.length);
    if (!items.length) {
      box.innerHTML = '<div class="media-empty">未嗅探到媒体资源</div>';
      return;
    }
    box.innerHTML = '';
    items.forEach((item, idx) => box.appendChild(renderItem(item, idx, res.page_url)));
  });
}

function renderItem(item, idx, pageUrl) {
  const row = document.createElement('div');
  row.className = 'media-item';
  const kind = item.is_mse ? 'mse' : (item.kind || 'hls');
  const sub = [item.quality_hint, formatSize(item.bytes)].filter(Boolean).join(' · ') || '流媒体';
  const label = document.createElement('div');
  label.className = 'media-name';
  label.innerHTML = '<div>' + escapeHtml(nameOf(item)) + '</div>' +
                    '<div class="media-sub">' + escapeHtml(sub) + '</div>';
  const chip = document.createElement('span');
  chip.className = 'media-kind ' + kind;
  chip.textContent = kind;
  row.appendChild(chip);
  row.appendChild(label);
  if (item.is_mse || item.kind === 'hls_segments') {
    // blob: 地址离开这个页面就没用了；只有分片流量时手上也没有主清单。
    // 两种情况能做的都只有把页面地址交给解析引擎。
    row.appendChild(pageBtn(pageUrl));
  } else {
    row.appendChild(downloadBtn(item, idx, pageUrl));
  }
  return row;
}

function downloadBtn(item, idx, pageUrl) {
  const btn = document.createElement('button');
  btn.className = 'btn-dl';
  btn.textContent = '下载';
  btn.addEventListener('click', () => {
    btn.disabled = true;
    btn.textContent = '添加中';
    chrome.runtime.sendMessage({ action: 'downloadMedia', item: item, tabId: activeTabId,
      pageUrl: pageUrl || '' }, (res) => {
      if (res && res.success) { btn.textContent = '已添加'; }
      else {
        btn.disabled = false;
        btn.textContent = '重试';
        btn.title = (res && res.error) || '添加失败';
      }
    });
  });
  return btn;
}

function pageBtn(pageUrl) {
  const btn = document.createElement('button');
  btn.className = 'btn-dl secondary';
  btn.textContent = '解析本页';
  btn.title = '把当前页面地址交给网页视频解析（yt-dlp）';
  btn.disabled = !pageUrl;
  btn.addEventListener('click', () => {
    btn.disabled = true;
    btn.textContent = '添加中';
    chrome.runtime.sendMessage({ action: 'downloadMedia', tabId: activeTabId, pageUrl: pageUrl,
      item: { url: pageUrl, kind: 'video_page' } }, (res) => {
      if (res && res.success) { btn.textContent = '已添加'; }
      else {
        btn.disabled = false;
        btn.textContent = '重试';
        btn.title = (res && res.error) || '添加失败';
      }
    });
  });
  return btn;
}

function nameOf(item) {
  try {
    const u = new URL(item.url);
    const last = decodeURIComponent(u.pathname.split('/').filter(Boolean).pop() || '');
    if (last && last.indexOf('.') > 0) return last;
    return u.hostname;
  } catch (e) {
    return item.url.slice(0, 28);
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
