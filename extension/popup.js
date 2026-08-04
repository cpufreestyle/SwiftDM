// SwiftDM 扩展弹窗逻辑
let enabled = true;

document.addEventListener('DOMContentLoaded', () => {
  loadStatus();

  document.getElementById('toggleBtn').addEventListener('click', () => {
    chrome.runtime.sendMessage({ action: 'toggleEnabled' }, (response) => {
      if (response) {
        enabled = response.enabled;
        updateUI();
      }
    });
  });

  document.getElementById('resetBtn').addEventListener('click', () => {
    chrome.runtime.sendMessage({ action: 'resetCount' }, (response) => {
      document.getElementById('sentCount').textContent = '0';
    });
  });
});

function loadStatus() {
  chrome.runtime.sendMessage({ action: 'getStatus' }, (response) => {
    if (response) {
      enabled = response.enabled;
      document.getElementById('sentCount').textContent = response.sentCount || 0;
      updateUI();
    }
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
