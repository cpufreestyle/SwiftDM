// SwiftDM 扩展弹窗逻辑
let enabled = true;
let activeTabId = null;

// 主题跟随应用设置（与 Web 端 / 桌面端共用同一份 theme 设置）：
// 弹窗上手动切换 data-theme，css 里的 :root / :root[data-theme="light"] 两套调色板切换。
// 拿不到设置（桌面端没启动）时落到系统偏好，至少和浏览器自己的深色/浅色一致。
function systemPrefersLight() {
  return !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches);
}

function resolveTheme(pref) {
  if (pref === 'light' || pref === 'dark') return pref;
  return systemPrefersLight() ? 'light' : 'dark';
}

function applyTheme(pref) {
  document.documentElement.dataset.theme = resolveTheme(pref);
}

function loadTheme() {
  chrome.runtime.sendMessage({ action: 'getSettings' }, (res) => {
    applyTheme(res && res.theme);
  });
}

document.addEventListener('DOMContentLoaded', () => {
  loadTheme();
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
  document.getElementById('tabTasks').addEventListener('click', () => showPanel('tasks'));
  document.getElementById('retryAllBtn').addEventListener('click', retryAllTasks);
  loadLive();                       // 打开弹窗即刷新实时状态与失败角标
  setInterval(loadLive, LIVE_REFRESH_MS);  // popup 打开期间持续刷新
  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    const tab = tabs && tabs[0];
    if (!tab) return;
    activeTabId = tab.id;
    document.getElementById('mediaPage').textContent = tab.title || tab.url || '当前标签页';
    loadMedia();
  });
});

// 面板名 -> DOM id 的映射规则（tabCapture / panelCapture ...）。
// 集中在这里遍历，新增面板只改 PANELS，不会漏刷某个 tab 或某个面板——
// 早先手写四个赋值时就漏了 tabTasks / panelTasks，「任务」页整个点不开。
const PANELS = ['capture', 'media', 'tasks'];

function showPanel(which) {
  if (PANELS.indexOf(which) < 0) return;
  PANELS.forEach((name) => {
    const cap = name.charAt(0).toUpperCase() + name.slice(1);
    const on = name === which;
    document.getElementById('tab' + cap).className = 'tab' + (on ? ' active' : '');
    document.getElementById('panel' + cap).className = on ? '' : 'hidden';
  });
  if (which === 'media') loadMedia();
  if (which === 'tasks') loadTasks();
}

function loadStatus() {
  chrome.runtime.sendMessage({ action: 'getStatus' }, (response) => {
    if (!response) return;
    enabled = response.enabled;
    document.getElementById('sentCount').textContent = response.sentCount || 0;
    renderServerBase(response);
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
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
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

// 「任务」页：失败/已取消任务列表 + 一键重试（数据来自 /api/tasks，
// 后端按创建顺序返回，所以倒序取即可得到"最近的失败"）
const TASK_RETRYABLE = ['failed', 'cancelled'];
const MAX_TASK_ROWS = 8;
const LIVE_REFRESH_MS = 2000;

// popup 头部实时状态：优先用后端 stats，缺字段时按任务列表现算（老版本后端也兼容）
function liveStatsOf(payload) {
  const tasks = (payload && payload.tasks) || [];
  const stats = (payload && payload.stats) || {};
  const num = (v, fallback) => (typeof v === 'number' && isFinite(v) ? v : fallback);
  const retryable = (t) => TASK_RETRYABLE.indexOf(t.status) >= 0;
  return {
    active: num(stats.active, tasks.filter((t) => t.status === 'downloading').length),
    // 与「任务」页角标/列表同一口径：失败 + 已取消（两者都能一键重试）。
    // 后端 stats.failed 只数 failed，直接用会和角标对不上，所以这里按任务列表现算。
    failed: tasks.filter(retryable).length,
    speed: num(stats.total_speed, tasks.reduce(
      (sum, t) => sum + (t.status === 'downloading' ? (t.speed || 0) : 0), 0)),
  };
}

function formatSpeed(bps) {
  if (!bps || bps <= 0) return '0 B/s';
  return formatSize(bps) + '/s';
}

// 服务器地址：显示真正连通过的 base。
// 之前这里写死 127.0.0.1:5001，那是浏览器监控端口而不是 Web 端口，
// 而且主程序端口被占时会顺延，写死的值从来就是错的。
function serverBaseOf(res) {
  const base = res && (res.__base || res.base);
  if (!base) return '';
  return String(base).replace(/^https?:\/\//, '');
}

function renderServerBase(res) {
  const el = document.getElementById('serverAddr');
  if (!el) return;
  el.textContent = serverBaseOf(res) || '未连接';
}

function loadLive() {
  chrome.runtime.sendMessage({ action: 'getTasks' }, (res) => {
    if (!res) return;  // 掉线时保留上一次的数字，不清空
    const s = liveStatsOf(res);
    document.getElementById('liveActive').textContent = String(s.active);
    document.getElementById('liveSpeed').textContent = formatSpeed(s.speed);
    document.getElementById('liveFailed').textContent = String(s.failed);
    renderServerBase(res);
    renderTasks(res);  // 同一份响应顺手刷新失败角标/列表，省一次请求
  });
}

function failedTasksOf(payload) {
  const tasks = (payload && payload.tasks) || [];
  return tasks.filter((t) => TASK_RETRYABLE.indexOf(t.status) >= 0)
              .slice(-MAX_TASK_ROWS)
              .reverse();
}

function loadTasks() {
  chrome.runtime.sendMessage({ action: 'getTasks' }, (res) => renderTasks(res));
}

function renderTasks(res) {
  const box = document.getElementById('taskList');
  const badge = document.getElementById('taskFailCount');
  const retryAll = document.getElementById('retryAllBtn');
  const failed = failedTasksOf(res);
  badge.textContent = String(failed.length);
  badge.className = 'badge warn' + (failed.length ? '' : ' hidden');
  retryAll.style.visibility = failed.length > 1 ? 'visible' : 'hidden';
  if (!res) {
      box.innerHTML = '<div class="media-empty">连不上 SwiftDM，确认桌面端已启动</div>';
      return;
  }
  if (!failed.length) {
      box.innerHTML = '<div class="media-empty">没有失败的下载任务</div>';
      return;
  }
  box.innerHTML = '';
  failed.forEach((task) => box.appendChild(taskRow(task)));
}

function taskRow(task) {
  const row = document.createElement('div');
  row.className = 'media-item';
  const label = document.createElement('div');
  label.className = 'media-name';
  const sub = (task.error || '').trim() || (task.status === 'cancelled' ? '已取消' : '未知原因');
  label.innerHTML = '<div>' + escapeHtml(task.filename || '未命名任务') + '</div>' +
                    '<div class="media-sub">' + escapeHtml(sub) + '</div>';
  row.appendChild(label);
  row.appendChild(retryBtn(task));
  return row;
}

function retryBtn(task) {
  const btn = document.createElement('button');
  btn.className = 'btn-dl';
  btn.textContent = '重试';
  btn.title = '重试该任务（失败任务会从已有分片续传）';
  btn.addEventListener('click', () => {
    btn.disabled = true;
    btn.textContent = '重试中';
    chrome.runtime.sendMessage({ action: 'retryTask', taskId: task.task_id }, (res) => {
      if (res && res.success) {
        btn.textContent = '已重试';
        setTimeout(loadTasks, 1200);  // 重试后多半又变成下载中，稍后刷新列表
      } else {
        btn.disabled = false;
        btn.textContent = '重试';
        btn.title = (res && res.error) || '重试失败';
      }
    });
  });
  return btn;
}

function retryAllTasks() {
  const btn = document.getElementById('retryAllBtn');
  btn.disabled = true;
  btn.textContent = '重试中';
  chrome.runtime.sendMessage({ action: 'retryAllTasks' }, (res) => {
    if (res && res.success) {
      btn.textContent = '已重试 ' + (res.retried || 0);
      setTimeout(loadTasks, 1200);
    } else {
      btn.disabled = false;
      btn.textContent = '全部重试';
      btn.title = (res && res.error) || '重试失败';
    }
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
