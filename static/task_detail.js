(() => {
  const logBox = document.getElementById('log-box');
  if (!logBox) {
    return;
  }

  const taskId = logBox.dataset.taskId;
  let lastId = Number(logBox.dataset.lastLogId || 0);

  const renderLog = (item) => {
    const row = document.createElement('div');
    row.className = `log-row log-${item.level.toLowerCase()}`;
    row.textContent = `[${item.created_at}] [${item.level}] [${item.stage}] ${item.message}`;
    logBox.appendChild(row);
    logBox.scrollTop = logBox.scrollHeight;
  };

  const poll = async () => {
    try {
      const response = await fetch(`/api/tasks/${taskId}/logs?after_id=${lastId}`);
      if (!response.ok) {
        return;
      }
      const payload = await response.json();
      for (const item of payload.items) {
        lastId = item.id;
        renderLog(item);
      }
    } catch (error) {
      console.warn('log polling failed', error);
    }
  };

  setInterval(poll, 2000);
})();
