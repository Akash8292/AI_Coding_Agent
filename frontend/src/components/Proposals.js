/**
 * Proposals — review cards for agent-proposed code changes and commands.
 *
 * Both cards render server records (ProposedChange / PendingApproval) and every
 * action goes through the API, so what the user sees is always what the server
 * will apply. A pending change has NOT touched the workspace.
 */

const Proposals = (() => {
  const esc = (s) => String(s ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#039;');

  const CHANGE_STATUS = {
    pending:  { label: 'Awaiting review', cls: 'pending',  note: 'Nothing has been written to your files yet.' },
    applied:  { label: 'Applied',         cls: 'applied',  note: 'Written to your workspace.' },
    rejected: { label: 'Rejected',        cls: 'rejected', note: 'Discarded — your files were not modified.' },
    reverted: { label: 'Reverted',        cls: 'rejected', note: 'Undone — files restored to their previous content.' },
    stale:    { label: 'Not applied',     cls: 'error',    note: 'The files changed after this diff was created, so nothing was written. Ask again for a fresh diff.' },
    failed:   { label: 'Failed',          cls: 'error',    note: 'Writing failed and was rolled back.' },
  };

  const ACTION_ICON = { modify: '✎', create: '+', delete: '−' };

  // ── Change card ──────────────────────────────────────────────────────────
  function renderChange(change, { hideSummary = false } = {}) {
    if (!change) return '';
    const st = CHANGE_STATUS[change.status] || { label: change.status, cls: 'pending', note: '' };
    const files = change.files || [];
    const fileHtml = files.map((f, i) => `
      <details class="change-file" ${files.length <= 3 || i === 0 ? 'open' : ''}>
        <summary>
          <span class="change-file-action action-${esc(f.action)}">${ACTION_ICON[f.action] || '✎'}</span>
          <span class="change-file-path">${esc(f.path)}</span>
          <span class="change-file-stats"><span class="stat-add">+${f.additions || 0}</span> <span class="stat-remove">−${f.deletions || 0}</span></span>
          <button class="link-btn" title="Open full-screen diff"
            onclick="event.preventDefault(); Proposals.openFullDiff('${esc(change.id)}', ${i})">⤢</button>
        </summary>
        ${(f.warnings || []).map(w => `<div class="change-warning">⚠ ${esc(w)}</div>`).join('')}
        <div class="diff-block">${window.DiffViewer.toHtml(f.diff)}</div>
      </details>`).join('');

    const plan = (change.plan || []).length > 1
      ? `<ol class="change-plan">${change.plan.map(s => `<li>${esc(s)}</li>`).join('')}</ol>` : '';

    let actions = '';
    if (change.status === 'pending') {
      actions = `
        <button class="btn btn-primary btn-sm" onclick="Proposals.applyChange('${esc(change.id)}', this)">✓ Accept${files.length > 1 ? ' all' : ''}</button>
        <button class="btn btn-secondary btn-sm" onclick="Proposals.rejectChange('${esc(change.id)}', this)">✕ Reject</button>`;
    } else if (change.status === 'applied') {
      actions = `<button class="btn btn-secondary btn-sm" onclick="Proposals.revertChange('${esc(change.id)}', this)">↶ Revert</button>`;
    }

    const verification = (change.verification || []).length ? `
      <div class="change-verify">
        ${change.verification.map(v => `<div class="verify-item ${v.ok ? 'ok' : 'bad'}">${v.ok ? '✓' : '✕'} <code>${esc(v.path)}</code> — ${esc(v.message)}</div>`).join('')}
      </div>` : '';

    const error = change.error && ['stale', 'failed'].includes(change.status)
      ? `<div class="change-error">${esc(change.error)}</div>` : '';

    return `
      <div class="change-card status-${st.cls}" id="change-${esc(change.id)}" data-change-id="${esc(change.id)}">
        <div class="change-card-header">
          <div class="change-card-title">
            <span class="change-title-text">Proposed change</span>
            <span class="change-card-count">${files.length} file${files.length === 1 ? '' : 's'} ·
              <span class="stat-add">+${change.additions || 0}</span> <span class="stat-remove">−${change.deletions || 0}</span></span>
          </div>
          <span class="status-badge ${st.cls}">${st.label}</span>
        </div>
        ${change.summary && !hideSummary ? `<div class="change-summary">${esc(change.summary)}</div>` : ''}
        ${plan}
        <div class="change-files">${fileHtml}</div>
        ${verification}
        ${error}
        <div class="change-card-footer">
          <span class="change-note">${st.note}</span>
          <div class="change-actions">${actions}</div>
        </div>
      </div>`;
  }

  function replaceChange(change) {
    const el = document.getElementById(`change-${change.id}`);
    if (el) el.outerHTML = renderChange(change);
    window.Store.updateAttached('change', change);
    if (typeof window.refreshChangesPanel === 'function') window.refreshChangesPanel();
  }

  async function act(id, btn, fn, successMsg) {
    const card = document.getElementById(`change-${id}`);
    card && card.querySelectorAll('button').forEach(b => { b.disabled = true; });
    if (btn) btn.textContent = '…';
    try {
      const res = await fn(id);
      replaceChange(res.change);
      window.showToast(successMsg(res.change), 'success');
      const ws = window.Store.getState().activeWorkspace;
      if (ws && typeof window.refreshWorkspaceSideData === 'function') window.refreshWorkspaceSideData(ws.id);
    } catch (err) {
      if (err.data && err.data.change) replaceChange(err.data.change);
      else card && card.querySelectorAll('button').forEach(b => { b.disabled = false; });
      window.showToast(err.message, 'error');
    }
  }

  const applyChange = (id, btn) => act(id, btn, window.API.applyChange,
    c => `Applied ${c.files.length} file(s)` + ((c.verification || []).every(v => v.ok) ? ' — verified' : ' — check verification'));
  const rejectChange = (id, btn) => act(id, btn, window.API.rejectChange, () => 'Change rejected — files untouched');
  const revertChange = (id, btn) => act(id, btn, window.API.revertChange, () => 'Change reverted');

  async function openFullDiff(id, index) {
    try {
      const change = await window.API.getChange(id);
      const f = change.files[index];
      window.DiffViewer.show({ path: f.path, explanation: change.summary, diff: f.diff, readOnly: true });
    } catch (err) {
      window.showToast(err.message, 'error');
    }
  }

  // ── Command card ─────────────────────────────────────────────────────────
  function renderCommand(cmd) {
    if (!cmd) return '';
    const command = cmd.command || (cmd.tool_args || {}).command || '';
    const r = cmd.result || {};
    const statusLabel = {
      pending: 'Needs your approval', running: 'Running…', completed: 'Completed', failed: 'Failed',
      timeout: 'Timed out', rejected: 'Rejected', blocked: 'Blocked',
    }[cmd.status] || cmd.status;
    const cls = { completed: 'applied', rejected: 'rejected', pending: 'pending', running: 'pending' }[cmd.status] || 'error';
    const allowedForSession = window.Store.hasSessionPermission(command);

    let body = '';
    if (cmd.status === 'pending') {
      body = `
        <div class="change-card-footer">
          <span class="change-note">Nothing runs without your approval.${cmd.danger_level === 'dangerous' ? ' <strong>This command changes state on your machine.</strong>' : ''}</span>
          <div class="change-actions">
            <button class="btn btn-primary btn-sm" onclick="Proposals.approveCommand(${cmd.id}, false, this)">Allow once</button>
            <button class="btn btn-secondary btn-sm" onclick="Proposals.approveCommand(${cmd.id}, true, this)">Allow for session</button>
            <button class="btn btn-secondary btn-sm" onclick="Proposals.rejectCommand(${cmd.id}, this)">Reject</button>
          </div>
        </div>`;
    } else if (r && (r.output !== undefined || r.error)) {
      body = `
        <div class="command-result">
          <div class="command-meta">exit ${r.exit_code ?? '—'} · ${((r.duration_ms || 0) / 1000).toFixed(1)}s${r.truncated ? ' · output truncated' : ''}</div>
          ${r.error ? `<div class="change-error">${esc(r.error)}</div>` : ''}
          ${r.output ? `<pre class="command-output">${esc(r.output)}</pre>` : ''}
        </div>`;
    }
    return `
      <div class="change-card command-card status-${cls}" id="command-${cmd.id}">
        <div class="change-card-header">
          <div class="change-card-title"><span class="change-title-text">Run command</span>
            <span class="risk-badge risk-${esc(cmd.danger_level)}">${esc(cmd.danger_level)} risk</span></div>
          <span class="status-badge ${cls}">${statusLabel}</span>
        </div>
        <pre class="command-line">$ ${esc(command)}</pre>
        ${cmd.reason ? `<div class="change-summary">${esc(cmd.reason)}</div>` : ''}
        ${allowedForSession && cmd.status === 'pending' ? '<div class="change-note">This exact command is allowed for this session.</div>' : ''}
        ${body}
      </div>`;
  }

  function replaceCommand(cmd) {
    const el = document.getElementById(`command-${cmd.id}`);
    if (el) el.outerHTML = renderCommand(cmd);
    window.Store.updateAttached('command', cmd);
  }

  async function approveCommand(id, forSession, btn) {
    const card = document.getElementById(`command-${id}`);
    const cmdText = card ? card.querySelector('.command-line').textContent.replace(/^\$ /, '') : '';
    if (forSession && cmdText) window.Store.allowSessionPermission(cmdText);
    card && card.querySelectorAll('button').forEach(b => { b.disabled = true; });
    if (btn) btn.textContent = 'Running…';
    try {
      const res = await window.API.approveCommand(id);
      replaceCommand(res.command);
      window.showToast(res.ok ? 'Command finished' : 'Command failed — see output', res.ok ? 'success' : 'error');
    } catch (err) {
      if (err.data && err.data.command) replaceCommand(err.data.command);
      window.showToast(err.message, 'error');
    }
  }

  async function rejectCommand(id) {
    try {
      const res = await window.API.rejectCommand(id);
      replaceCommand(res.command);
    } catch (err) {
      window.showToast(err.message, 'error');
    }
  }

  // Auto-approve a command the user already allowed for this session.
  function maybeAutoApprove(cmd) {
    if (cmd && cmd.status === 'pending' && cmd.danger_level !== 'blocked'
        && window.Store.hasSessionPermission(cmd.command)) {
      approveCommand(cmd.id, false, null);
      return true;
    }
    return false;
  }

  return {
    renderChange, renderCommand, applyChange, rejectChange, revertChange, openFullDiff,
    approveCommand, rejectCommand, maybeAutoApprove, replaceChange, replaceCommand,
  };
})();

window.Proposals = Proposals;
