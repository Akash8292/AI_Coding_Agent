/**
 * ActivityLog Component
 * Shows the agent's real steps (emitted by the backend only after each
 * operation actually happened), with status and measured duration.
 */

const ActivityLog = (() => {
  const esc = (s) => String(s ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

  const ICON = { done: '✓', running: '⟳', error: '✕', skipped: '–', stopped: '■' };

  function fmtMs(ms) {
    if (ms === undefined || ms === null) return '';
    return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;
  }

  function init() {
    window.Store.subscribe((state, changedKeys) => {
      if (changedKeys.includes('activityLog')) renderPanel(state.activityLog);
    });
  }

  function itemHtml(item, compact) {
    const status = item.status || 'done';
    return `
      <div class="act-item act-${status}">
        <span class="act-icon">${ICON[status] || '•'}</span>
        <div class="act-body">
          <div class="act-text">${esc(item.text)}</div>
          ${item.detail && !compact ? `<div class="act-detail">${esc(item.detail)}</div>` : ''}
          ${item.detail && compact ? `<div class="act-detail" title="${esc(item.detail)}">${esc(item.detail.length > 140 ? item.detail.slice(0, 140) + '…' : item.detail)}</div>` : ''}
        </div>
        <span class="act-ms">${status === 'running' ? '' : fmtMs(item.ms)}</span>
      </div>`;
  }

  function renderPanel(items = []) {
    const container = document.getElementById('activity-log');
    if (!container) return;
    container.innerHTML = items && items.length
      ? items.map(i => itemHtml(i, false)).join('')
      : '<div class="activity-empty">No activity yet</div>';
  }

  /** Collapsible timeline shown inside an assistant message. */
  function renderInline(items = [], { open = false, stopped = false } = {}) {
    if (!items || !items.length) return '';
    const list = items.map(i => (stopped && i.status === 'running') ? { ...i, status: 'stopped', text: i.text + ' — stopped' } : i);
    const total = list.reduce((a, i) => a + (i.ms || 0), 0);
    const running = list.find(i => i.status === 'running');
    const failed = list.some(i => i.status === 'error');
    const wasStopped = list.some(i => i.status === 'stopped');
    const summary = running ? esc(running.text)
      : `${list.length} step${list.length === 1 ? '' : 's'} · ${fmtMs(total)}${wasStopped ? ' · stopped' : failed ? ' · with errors' : ''}`;
    return `
      <details class="activity-box" ${open || running ? 'open' : ''}>
        <summary>${running ? '<span class="spinner"></span>' : '<span class="act-summary-icon">◷</span>'}<span>${summary}</span></summary>
        <div class="activity-list">${list.map(i => itemHtml(i, true)).join('')}</div>
      </details>`;
  }

  return { init, renderPanel, renderInline, fmtMs };
})();

window.ActivityLog = ActivityLog;
