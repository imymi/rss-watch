document.addEventListener('submit', event => {
  const prompt = event.target.dataset.confirm;
  if (prompt && !window.confirm(prompt)) event.preventDefault();
});
document.querySelectorAll('[data-back]').forEach(button => {
  button.addEventListener('click', () => window.history.back());
});
if (document.getElementById('worker-state')) {
  async function refreshStatus() {
    try {
      const response = await fetch('/api/status', {credentials: 'same-origin'});
      if (!response.ok) return;
      const status = await response.json();
      document.getElementById('worker-state').textContent = status.maintenance ? '维护中' : status['后台运行'] ? '在线' : '离线';
      const heartbeat = status['心跳时间'];
      document.getElementById('worker-heartbeat').textContent = '最近心跳 ' + (heartbeat ? new Date(heartbeat * 1000).toLocaleString('zh-CN', {timeZone: status.timezone}) : '—');
      for (const element of document.querySelectorAll('[data-state]')) {
        element.textContent = status['队列'].find(row => row.state === element.dataset.state)?.count ?? 0;
      }
    } catch (_) { /* Next poll will retry; the last displayed state remains available. */ }
  }
  setInterval(refreshStatus, 5000);
}
