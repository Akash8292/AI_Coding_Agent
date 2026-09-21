/**
 * ActivityLog Component
 * Visualizes agent reasoning steps, tool usage, file searches, and checkpoints.
 */

const ActivityLog = (() => {
  function init() {
    window.Store.subscribe((state, changedKeys) => {
      if (changedKeys.includes('activityLog')) {
        renderPanel(state.activityLog);
      }
    });
  }

  function renderPanel(items = []) {
    const container = document.getElementById('activity-log');
    if (!container) return;

    if (!items || items.length === 0) {
      container.innerHTML = '<div class="activity-empty">No activity yet</div>';
      return;
    }

    container.innerHTML = '';
    const fragment = document.createDocumentFragment();

    for (const item of items) {
      const el = document.createElement('div');
      const status = item.status || 'done';
      el.className = `activity-log-item ${status}`;

      let icon = '✓';
      if (status === 'loading') icon = '⟳';
      else if (status === 'error') icon = '✕';
      else if (item.type === 'search') icon = '🔍';
      else if (item.type === 'file') icon = '📄';
      else if (item.type === 'edit') icon = '✎';
      else if (item.type === 'command') icon = '⚡';

      el.innerHTML = `
        <span class="al-icon">${icon}</span>
        <div style="flex: 1; word-break: break-word;">
          <div>${item.text || item.message || ''}</div>
          ${item.detail ? `<div style="font-size: 10px; color: var(--text-2); font-family: var(--font-mono); margin-top: 2px;">${item.detail}</div>` : ''}
        </div>
      `;

      fragment.appendChild(el);
    }

    container.appendChild(fragment);
  }

  function renderInline(items = []) {
    if (!items || items.length === 0) return '';
    let html = '<div class="message-activity-box" style="margin-bottom: 12px; padding: 8px 12px; background: var(--bg-2); border-radius: var(--radius-sm); border: 1px solid var(--border);">';
    html += '<div style="font-size: 11px; font-weight: 600; color: var(--text-2); margin-bottom: 6px; text-transform: uppercase; letter-spacing: 0.05em;">Inspection Steps</div>';
    html += '<div style="display: flex; flex-direction: column; gap: 4px;">';
    for (const item of items) {
      let icon = '✓';
      if (item.status === 'loading') icon = '⟳';
      else if (item.status === 'error') icon = '✕';
      html += `
        <div style="display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--text-1);">
          <span style="color: var(--accent); font-size: 11px;">${icon}</span>
          <span>${item.text || ''}</span>
        </div>
      `;
    }
    html += '</div></div>';
    return html;
  }

  return {
    init,
    renderPanel,
    renderInline,
  };
})();

window.ActivityLog = ActivityLog;
