/**
 * DiffViewer Component
 * Renders unified diffs with line numbers, syntax highlighting, and accept/reject actions.
 */

const DiffViewer = (() => {
  let currentDiffData = null;

  function parseUnifiedDiff(diffText) {
    if (!diffText) return [];
    const lines = diffText.split('\n');
    let oldLineNum = 0;
    let newLineNum = 0;
    const parsed = [];

    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      if (line.startsWith('---') || line.startsWith('+++')) {
        parsed.push({ type: 'header', content: line, oldNum: '', newNum: '' });
      } else if (line.startsWith('@@')) {
        // Parse hunk header: @@ -oldStart,oldCount +newStart,newCount @@
        const match = line.match(/@@\s+-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@/);
        if (match) {
          oldLineNum = parseInt(match[1], 10);
          newLineNum = parseInt(match[2], 10);
        }
        parsed.push({ type: 'hunk', content: line, oldNum: '...', newNum: '...' });
      } else if (line.startsWith('+')) {
        parsed.push({
          type: 'add',
          content: line.substring(1),
          oldNum: '',
          newNum: newLineNum++,
        });
      } else if (line.startsWith('-')) {
        parsed.push({
          type: 'remove',
          content: line.substring(1),
          oldNum: oldLineNum++,
          newNum: '',
        });
      } else {
        // Context line
        const content = line.startsWith(' ') ? line.substring(1) : line;
        parsed.push({
          type: 'context',
          content,
          oldNum: oldLineNum++,
          newNum: newLineNum++,
        });
      }
    }
    return parsed;
  }

  function renderDiff(container, diffText) {
    if (typeof container === 'string') {
      container = document.getElementById(container);
    }
    if (!container) return;

    container.innerHTML = '';
    const lines = parseUnifiedDiff(diffText);

    if (lines.length === 0) {
      container.innerHTML = '<div style="padding: 16px; color: var(--text-2); text-align: center;">No changes detected</div>';
      return;
    }

    const fragment = document.createDocumentFragment();

    for (const item of lines) {
      const row = document.createElement('div');
      row.className = `diff-line ${item.type}`;

      const numCol = document.createElement('div');
      numCol.className = 'diff-line-num';
      if (item.type === 'add') {
        numCol.textContent = `+${item.newNum}`;
      } else if (item.type === 'remove') {
        numCol.textContent = `-${item.oldNum}`;
      } else if (item.type === 'hunk') {
        numCol.textContent = '@@';
      } else if (item.newNum) {
        numCol.textContent = `${item.newNum}`;
      } else {
        numCol.textContent = '';
      }

      const contentCol = document.createElement('div');
      contentCol.className = 'diff-line-content';
      contentCol.textContent = item.content;

      row.appendChild(numCol);
      row.appendChild(contentCol);
      fragment.appendChild(row);
    }

    container.appendChild(fragment);
  }

  function show(diffData) {
    currentDiffData = diffData;
    const modal = document.getElementById('diff-modal');
    const pathEl = document.getElementById('diff-file-path');
    const expEl = document.getElementById('diff-explanation');
    const viewerEl = document.getElementById('diff-viewer');

    if (!modal) return;

    if (pathEl) pathEl.textContent = diffData.path || 'Code Diff';
    if (expEl) expEl.textContent = diffData.explanation || '';
    if (viewerEl) renderDiff(viewerEl, diffData.diff || '');

    modal.classList.remove('hidden');
  }

  function close() {
    const modal = document.getElementById('diff-modal');
    if (modal) modal.classList.add('hidden');
    currentDiffData = null;
  }

  async function accept() {
    if (!currentDiffData) return;
    const data = currentDiffData;
    close();

    if (typeof data.onAccept === 'function') {
      data.onAccept(data);
    } else if (data.workspace_id && data.path && data.proposed_content) {
      try {
        await window.API.applyEdit(data.workspace_id, data.path, data.proposed_content);
        if (window.showToast) window.showToast(`Applied changes to ${data.path}`, 'success');
      } catch (err) {
        if (window.showToast) window.showToast(`Failed to apply: ${err.message}`, 'error');
      }
    }
  }

  function reject() {
    if (currentDiffData && typeof currentDiffData.onReject === 'function') {
      currentDiffData.onReject(currentDiffData);
    }
    close();
    if (window.showToast) window.showToast('Changes discarded', 'info');
  }

  return {
    renderDiff,
    show,
    close,
    accept,
    reject,
    getCurrent: () => currentDiffData,
  };
})();

window.DiffViewer = DiffViewer;
window.closeDiffModal = DiffViewer.close;
window.acceptDiff = DiffViewer.accept;
window.rejectDiff = DiffViewer.reject;
