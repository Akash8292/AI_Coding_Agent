/**
 * DiffViewer Component
 * Renders unified diffs (computed by the server from the real file contents)
 * with old/new line numbers — inline in review cards and full-screen.
 */

const DiffViewer = (() => {
  const esc = (s) => String(s ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

  function parseUnifiedDiff(diffText) {
    if (!diffText) return [];
    const parsed = [];
    let oldN = 0;
    let newN = 0;
    for (const line of diffText.split('\n')) {
      if (line.startsWith('--- ') || line.startsWith('+++ ')) {
        continue; // file headers: the card already shows the path
      } else if (line.startsWith('@@')) {
        const m = line.match(/@@\s+-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@(.*)/);
        if (m) { oldN = parseInt(m[1], 10); newN = parseInt(m[2], 10); }
        parsed.push({ type: 'hunk', content: line, oldNum: '', newNum: '' });
      } else if (line.startsWith('\\')) {
        parsed.push({ type: 'meta', content: line.slice(2), oldNum: '', newNum: '' });
      } else if (line.startsWith('+')) {
        parsed.push({ type: 'add', content: line.slice(1), oldNum: '', newNum: newN++ });
      } else if (line.startsWith('-')) {
        parsed.push({ type: 'remove', content: line.slice(1), oldNum: oldN++, newNum: '' });
      } else if (line.startsWith(' ')) {
        parsed.push({ type: 'context', content: line.slice(1), oldNum: oldN++, newNum: newN++ });
      }
    }
    return parsed;
  }

  function toHtml(diffText) {
    const rows = parseUnifiedDiff(diffText);
    if (!rows.length) return '<div class="diff-empty">No line changes</div>';
    const sign = { add: '+', remove: '−', context: ' ', hunk: '', meta: '' };
    return `<table class="diff-table"><tbody>${rows.map(r => `
      <tr class="diff-row diff-${r.type}">
        <td class="diff-num">${r.oldNum}</td><td class="diff-num">${r.newNum}</td>
        <td class="diff-sign">${sign[r.type]}</td>
        <td class="diff-code">${esc(r.content) || '&nbsp;'}</td>
      </tr>`).join('')}</tbody></table>`;
  }

  function renderDiff(container, diffText) {
    if (typeof container === 'string') container = document.getElementById(container);
    if (container) container.innerHTML = toHtml(diffText);
  }

  function show(diffData) {
    const modal = document.getElementById('diff-modal');
    if (!modal) return;
    document.getElementById('diff-file-path').textContent = diffData.path || 'Diff';
    document.getElementById('diff-explanation').textContent = diffData.explanation || '';
    renderDiff('diff-viewer', diffData.diff || '');
    modal.classList.remove('hidden');
  }

  function close() {
    const modal = document.getElementById('diff-modal');
    if (modal) modal.classList.add('hidden');
  }

  return { parseUnifiedDiff, toHtml, renderDiff, show, close };
})();

window.DiffViewer = DiffViewer;
window.closeDiffModal = DiffViewer.close;
