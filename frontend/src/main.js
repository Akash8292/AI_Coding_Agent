/**
 * CodeSage Main Application Coordinator
 *
 * Chat lifecycle (agent state machine):
 *   IDLE ──send──▶ RUNNING ──done/error/cancelled──▶ IDLE
 *                     │
 *                   Stop ──▶ STOPPING (server cancel + fetch abort) ──▶ IDLE
 * After every run the conversation is re-fetched from the server, so what is
 * rendered (messages, proposals, statuses) is exactly what was persisted.
 */

(function () {
  // ── Rendering helpers ───────────────────────────────────────────────────
  if (window.marked) window.marked.use({ async: false, breaks: true, gfm: true });

  function escapeHtml(str) {
    return String(str ?? '')
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#039;');
  }
  window.escapeHtml = escapeHtml;

  /** Markdown → sanitized HTML. LLM output is untrusted: never inject it raw. */
  function renderMarkdown(text) {
    if (!text) return '';
    let html = null;
    try {
      if (window.marked) {
        const out = window.marked.parse(text);
        if (typeof out === 'string') html = out;
      }
    } catch (e) { /* fall through */ }
    if (html === null || !window.DOMPurify) {
      return `<pre class="plain-text">${escapeHtml(text)}</pre>`;
    }
    return window.DOMPurify.sanitize(html, { USE_PROFILES: { html: true } });
  }

  /** Syntax-highlight code blocks and add copy buttons inside `root`. */
  function enhanceCode(root) {
    if (!root) return;
    root.querySelectorAll('pre > code').forEach((code) => {
      if (code.dataset.enhanced) return;
      code.dataset.enhanced = '1';
      try { if (window.hljs) window.hljs.highlightElement(code); } catch (e) { /* ignore */ }
      const btn = document.createElement('button');
      btn.className = 'copy-btn';
      btn.textContent = 'Copy';
      btn.onclick = () => {
        navigator.clipboard.writeText(code.innerText).then(() => {
          btn.textContent = 'Copied';
          setTimeout(() => { btn.textContent = 'Copy'; }, 1200);
        });
      };
      code.parentElement.classList.add('has-copy');
      code.parentElement.appendChild(btn);
    });
  }

  // ── Toasts ──────────────────────────────────────────────────────────────
  window.showToast = function (message, type = 'info') {
    let container = document.getElementById('toast-container');
    if (!container) {
      container = document.createElement('div');
      container.id = 'toast-container';
      container.className = 'toast-container';
      document.body.appendChild(container);
    }
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(() => {
      toast.style.opacity = '0';
      toast.style.transition = 'opacity 0.2s ease';
      setTimeout(() => toast.remove(), 200);
    }, type === 'error' ? 6000 : 3500);
  };

  // ── Auth ────────────────────────────────────────────────────────────────
  window.switchTab = function (tab) {
    const isReg = tab === 'register';
    document.getElementById('auth-error').classList.add('hidden');
    document.getElementById('login-tab').classList.toggle('active', !isReg);
    document.getElementById('register-tab').classList.toggle('active', isReg);
    document.getElementById('register-fields').classList.toggle('hidden', !isReg);
    document.getElementById('auth-submit').textContent = isReg ? 'Create Account' : 'Sign In';
  };

  window.handleAuth = async function (e) {
    e.preventDefault();
    const isRegister = document.getElementById('register-tab').classList.contains('active');
    const email = document.getElementById('auth-email').value.trim();
    const password = document.getElementById('auth-password').value;
    const name = (document.getElementById('auth-name') || {}).value || '';
    const errorEl = document.getElementById('auth-error');
    const submitBtn = document.getElementById('auth-submit');
    errorEl.classList.add('hidden');
    submitBtn.disabled = true;
    try {
      if (isRegister) await window.API.register(email, password, name.trim());
      else await window.API.login(email, password);
      await bootstrapApp();
    } catch (err) {
      errorEl.textContent = err.message || 'Authentication failed';
      errorEl.classList.remove('hidden');
    } finally {
      submitBtn.disabled = false;
    }
  };

  window.onAuthUnauthorized = function () { showAuthView(); };

  function showAuthView() {
    document.getElementById('auth-page').classList.remove('hidden');
    document.getElementById('app').classList.add('hidden');
  }

  function showAppView() {
    document.getElementById('auth-page').classList.add('hidden');
    document.getElementById('app').classList.remove('hidden');
  }

  // ── Bootstrap ───────────────────────────────────────────────────────────
  let bootstrapped = false;
  async function bootstrapApp() {
    if (!window.API.getToken()) { showAuthView(); return; }
    try {
      const meRes = await window.API.getMe();
      window.Store.setUser(meRes.user);
      renderUserProfile(meRes.user);
      showAppView();
      if (!bootstrapped) {
        window.ModelSelector.init();
        window.ActivityLog.init();
        window.CommandPalette.init();
        setupGlobalKeybindings();
        bootstrapped = true;
      }
      await Promise.allSettled([loadModels(), loadWorkspaces(), loadConversations()]);
      setMode(window.Store.getState().mode, true);
      updateRepoStatusIndicator();
      setAgentState('IDLE');
    } catch (err) {
      console.error('Bootstrap error:', err);
      showAuthView();
    }
  }

  function renderUserProfile(user) {
    if (!user) return;
    document.getElementById('user-name').textContent = user.name || user.email.split('@')[0];
    document.getElementById('user-email').textContent = user.email;
    document.getElementById('user-avatar').textContent = (user.name || user.email || 'U')[0].toUpperCase();
  }

  async function loadModels() {
    try {
      window.Store.setModels(await window.API.getModels());
      window.ModelSelector.render();
    } catch (err) {
      console.warn('Could not load models:', err);
    }
  }
  window.reloadModels = loadModels;

  // ── Workspaces ──────────────────────────────────────────────────────────
  let workspacePolicy = { allow_any_path: true, allow_git_clone: true };

  async function loadWorkspaces() {
    try {
      const res = await window.API.listWorkspaces();
      workspacePolicy = res.policy || workspacePolicy;
      window.Store.setWorkspaces(res.workspaces || []);
      renderWorkspacesList(res.workspaces || []);
      if (res.workspaces && res.workspaces.length && !window.Store.getState().activeWorkspace) {
        selectWorkspace(res.workspaces[0].id);
      }
    } catch (err) {
      console.warn('Could not load workspaces:', err);
    }
  }

  function renderWorkspacesList(workspaces) {
    const container = document.getElementById('workspace-selector');
    if (!container) return;
    if (!workspaces || !workspaces.length) {
      container.innerHTML = '<div class="workspace-empty" onclick="showWorkspaceModal()">+ Add your first workspace</div>';
      return;
    }
    const active = window.Store.getState().activeWorkspace;
    container.innerHTML = `
      <select class="workspace-dropdown-select" onchange="selectWorkspace(this.value)" aria-label="Workspace">
        ${workspaces.map(ws => `<option value="${ws.id}" ${active && active.id === ws.id ? 'selected' : ''}>
          ${escapeHtml(ws.name)}${ws.branch ? ` (${escapeHtml(ws.branch)})` : ''}</option>`).join('')}
      </select>
      ${active ? `<div class="workspace-path" title="${escapeHtml(active.repo_path)}">${escapeHtml(active.repo_path)}</div>` : ''}`;
  }

  window.selectWorkspace = function (wsId) {
    wsId = parseInt(wsId, 10);
    const state = window.Store.getState();
    const ws = (state.workspaces || []).find(w => w.id === wsId);
    if (!ws) return;
    window.Store.setActiveWorkspace(ws);
    renderWorkspacesList(state.workspaces);
    updateRepoStatusIndicator();
    refreshWorkspaceSideData(ws.id);
    if (ws.index_status === 'indexing') pollIndexStatus(ws.id);
  };

  function refreshWorkspaceSideData(wsId) {
    loadWorkspaceFiles(wsId);
    loadGitStatus(wsId);
  }
  window.refreshWorkspaceSideData = refreshWorkspaceSideData;

  async function loadWorkspaceFiles(wsId) {
    try {
      const res = await window.API.listFiles(wsId);
      window.Store.setFiles(res.files || []);
      renderFileTree(res.files || []);
    } catch (err) {
      console.warn('Error loading workspace files:', err);
    }
  }

  function renderFileTree(files) {
    const container = document.getElementById('file-tree');
    if (!container) return;
    if (!files || !files.length) {
      container.innerHTML = '<div class="files-empty">No files found.<br/><button class="btn btn-secondary btn-sm" style="margin-top:8px" onclick="indexCurrentWorkspace()">Re-index</button></div>';
      return;
    }
    const shown = files.slice(0, 300);
    container.innerHTML = shown.map(f => `
      <div class="file-tree-item" data-path="${escapeHtml(f.path)}" title="${escapeHtml(f.path)}">
        <span class="file-icon">📄</span><span class="file-tree-path">${escapeHtml(f.path)}</span>
      </div>`).join('') +
      (files.length > shown.length ? `<div class="files-more">+ ${files.length - shown.length} more — use the filter</div>` : '');
    container.querySelectorAll('.file-tree-item').forEach(el => {
      el.onclick = () => window.openFileInChat(el.dataset.path);
    });
  }

  window.filterFiles = function (query) {
    const q = (query || '').toLowerCase().trim();
    const all = window.Store.getState().files || [];
    renderFileTree(q ? all.filter(f => f.path.toLowerCase().includes(q)) : all);
  };

  window.openFileInChat = function (filePath) {
    const input = document.getElementById('chat-input');
    if (!input) return;
    const insert = `\`${filePath}\` `;
    input.value = input.value ? `${input.value.trimEnd()} ${insert}` : `Explain ${insert}`;
    input.focus();
    autoResizeTextarea(input);
  };

  // ── Git panel ───────────────────────────────────────────────────────────
  async function loadGitStatus(wsId) {
    const panel = document.getElementById('git-panel');
    if (!panel) return;
    const [status, gitLog, cps] = await Promise.allSettled([
      window.API.getGitStatus(wsId), window.API.getGitLog(wsId), window.API.listGitCheckpoints(wsId),
    ]);
    const st = status.status === 'fulfilled' ? status.value : null;
    if (!st || !st.is_repo) {
      panel.innerHTML = '<div class="git-empty">Not a Git repository. CodeSage still keeps the original of every file it changes, so applied changes can be reverted.</div>';
      return;
    }
    const commits = gitLog.status === 'fulfilled' ? (gitLog.value.commits || []) : [];
    const checkpoints = cps.status === 'fulfilled' ? (cps.value.checkpoints || []) : [];
    const changes = st.changes || [];
    panel.innerHTML = `
      <div class="git-section">
        <div class="git-section-title">BRANCH</div>
        <div class="git-branch">⎇ ${escapeHtml(st.branch || '?')} <span class="git-head">${escapeHtml(st.head || '')}</span>
          ${st.ahead ? `<span class="git-ab">↑${st.ahead}</span>` : ''}${st.behind ? `<span class="git-ab">↓${st.behind}</span>` : ''}</div>
      </div>
      <div class="git-section">
        <div class="git-section-title">WORKING TREE</div>
        ${st.clean ? '<div class="git-clean">✓ Clean</div>' : changes.slice(0, 30).map(c =>
          `<div class="git-status-line"><span class="git-code git-${escapeHtml(c.label)}">${escapeHtml(c.code.trim() || '?')}</span> ${escapeHtml(c.path)}</div>`).join('')}
        ${changes.length > 30 ? `<div class="files-more">+ ${changes.length - 30} more</div>` : ''}
      </div>
      <div class="git-section">
        <div class="git-section-title">CHECKPOINTS <span class="git-hint" title="Snapshots are saved with git stash create/store; your working tree is never modified by saving one.">?</span></div>
        <div class="git-actions"><button class="git-btn" onclick="createCheckpoint()">Save checkpoint</button></div>
        ${checkpoints.slice(0, 6).map(c => `
          <div class="checkpoint-item">
            <div><div class="checkpoint-msg">${escapeHtml(c.message.replace(/^.*codesage:\s*/, ''))}</div>
            <div class="checkpoint-date">${escapeHtml(c.sha.slice(0, 8))} · ${escapeHtml(c.date)}</div></div>
            <button class="git-btn" onclick="restoreCheckpoint('${escapeHtml(c.sha)}')">Restore</button>
          </div>`).join('') || '<div class="git-hint-text">No checkpoints yet. One is saved automatically before each applied change.</div>'}
      </div>
      ${commits.length ? `<div class="git-section"><div class="git-section-title">RECENT COMMITS</div>
        ${commits.slice(0, 6).map(cm => `<div class="commit-item"><div class="commit-subject">${escapeHtml(cm.subject)}</div>
          <div class="checkpoint-date">${escapeHtml(cm.hash)} · ${escapeHtml(cm.author)} · ${escapeHtml(cm.date)}</div></div>`).join('')}</div>` : ''}`;
  }

  window.createCheckpoint = async function () {
    const ws = window.Store.getState().activeWorkspace;
    if (!ws) return window.showToast('Select a workspace first', 'error');
    try {
      const res = await window.API.createGitCheckpoint(ws.id, 'manual checkpoint');
      window.showToast(res.message || 'Checkpoint saved', 'success');
      loadGitStatus(ws.id);
    } catch (err) {
      window.showToast(`Checkpoint error: ${err.message}`, 'error');
    }
  };

  window.restoreCheckpoint = async function (sha) {
    const ws = window.Store.getState().activeWorkspace;
    if (!ws) return;
    if (!confirm('Restore tracked files to this checkpoint?\n\nYour current state is saved as a new checkpoint first, so this can be undone. Untracked files are not touched.')) return;
    try {
      const res = await window.API.restoreGitCheckpoint(ws.id, sha);
      window.showToast(res.message || 'Checkpoint restored', 'success');
      refreshWorkspaceSideData(ws.id);
    } catch (err) {
      window.showToast(`Restore error: ${err.message}`, 'error');
    }
  };

  // ── Indexing ────────────────────────────────────────────────────────────
  window.indexCurrentWorkspace = async function (force = false) {
    const ws = window.Store.getState().activeWorkspace;
    if (!ws) return window.showToast('Select a workspace to index', 'error');
    try {
      await window.API.indexWorkspace(ws.id, force);
      window.showToast(force ? 'Rebuilding index…' : 'Refreshing index…', 'info');
      pollIndexStatus(ws.id);
    } catch (err) {
      window.showToast(`Indexing failed: ${err.message}`, 'error');
    }
  };

  let indexTimer = null;
  function pollIndexStatus(wsId) {
    clearInterval(indexTimer);
    const status = document.getElementById('repo-context-status');
    indexTimer = setInterval(async () => {
      try {
        const st = await window.API.getIndexStatus(wsId);
        if (st.status === 'indexing' && status) {
          status.textContent = `Indexing… ${st.indexed_files || 0}/${st.total_files || '?'} files`;
        } else if (st.status === 'done') {
          clearInterval(indexTimer);
          updateRepoStatusIndicator();
          loadWorkspaceFiles(wsId);
        } else if (st.status === 'error') {
          clearInterval(indexTimer);
          window.showToast(`Indexing failed: ${st.error || 'unknown error'}`, 'error');
          updateRepoStatusIndicator();
        }
      } catch (e) {
        clearInterval(indexTimer);
      }
    }, 1000);
  }

  // ── Workspace modal ─────────────────────────────────────────────────────
  window.showWorkspaceModal = function () {
    document.getElementById('workspace-modal').classList.remove('hidden');
    document.getElementById('ws-error').classList.add('hidden');
    document.getElementById('ws-path').value = '';
    document.getElementById('ws-name').value = '';
    document.getElementById('ws-git').value = '';
    document.getElementById('ws-git-group').classList.toggle('hidden', !workspacePolicy.allow_git_clone);
    document.getElementById('ws-path-hint').textContent = workspacePolicy.allow_any_path
      ? 'Absolute path to a directory on the machine running CodeSage'
      : 'Path inside your workspace folder on the server (or clone a repository above)';
    setTimeout(() => document.getElementById('ws-path').focus(), 30);
  };

  window.createWorkspace = async function () {
    const name = document.getElementById('ws-name').value.trim();
    const repoPath = document.getElementById('ws-path').value.trim();
    const gitUrl = document.getElementById('ws-git').value.trim();
    const errorEl = document.getElementById('ws-error');
    const btn = document.getElementById('ws-submit-btn');
    if (!repoPath && !gitUrl) {
      errorEl.textContent = 'Enter a repository path or a git URL';
      errorEl.classList.remove('hidden');
      return;
    }
    btn.disabled = true;
    btn.textContent = gitUrl ? 'Cloning…' : 'Adding…';
    errorEl.classList.add('hidden');
    try {
      const ws = await window.API.createWorkspace(name, repoPath || undefined, gitUrl || undefined);
      closeModal('workspace-modal');
      window.showToast(`Workspace "${ws.name}" added — indexing in the background`, 'success');
      await loadWorkspaces();
      selectWorkspace(ws.id);
      pollIndexStatus(ws.id);
    } catch (err) {
      errorEl.textContent = err.message || 'Failed to add workspace';
      errorEl.classList.remove('hidden');
    } finally {
      btn.disabled = false;
      btn.textContent = 'Add Workspace';
    }
  };

  window.closeModal = function (modalId) {
    const el = document.getElementById(modalId);
    if (el) el.classList.add('hidden');
  };

  // ── Conversations ───────────────────────────────────────────────────────
  async function loadConversations(search = '') {
    const listEl = document.getElementById('conversations-list');
    try {
      const res = await window.API.listConversations(search);
      window.Store.setConversations(res.conversations || []);
      renderConversationsList(res.conversations || []);
    } catch (err) {
      if (listEl) listEl.innerHTML = '<div class="sidebar-loading">Could not load chats</div>';
    }
  }

  function renderConversationsList(conversations) {
    const listEl = document.getElementById('conversations-list');
    if (!listEl) return;
    if (!conversations || !conversations.length) {
      listEl.innerHTML = '<div class="sidebar-loading">No chats yet</div>';
      return;
    }
    const activeId = window.Store.getState().activeConversationId;
    listEl.innerHTML = conversations.map(c => `
      <div class="conv-item ${activeId === c.id ? 'active' : ''}" onclick="selectConversation(${c.id})" title="${escapeHtml(c.title)}">
        <span class="conv-title">${escapeHtml(c.title || 'Untitled Chat')}</span>
        <div class="conv-actions" onclick="event.stopPropagation()">
          <button class="conv-action-btn" title="Delete chat" onclick="deleteConversation(${c.id})">✕</button>
        </div>
      </div>`).join('');
  }

  let searchTimer = null;
  window.filterChats = function (q) {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => loadConversations(q), 200);
  };

  window.newChat = function () {
    if (agentState !== 'IDLE') return window.showToast('Stop the current request first (Esc)', 'info');
    window.Store.setActiveConversation(null, []);
    document.getElementById('chat-title').textContent = 'New Chat';
    renderMessages([]);
    renderConversationsList(window.Store.getState().conversations);
    window.refreshChangesPanel();
    window.ActivityLog.renderPanel([]);
    const input = document.getElementById('chat-input');
    input.value = '';
    input.focus();
  };

  window.selectConversation = async function (convId) {
    if (agentState !== 'IDLE') return window.showToast('Stop the current request first (Esc)', 'info');
    await openConversation(convId);
  };

  async function openConversation(convId, { scroll = true } = {}) {
    try {
      const conv = await window.API.getConversation(convId);
      window.Store.setActiveConversation(conv.id, conv.messages || []);
      document.getElementById('chat-title').textContent = conv.title || 'Chat';
      renderMessages(conv.messages || [], scroll);
      const state = window.Store.getState();
      renderConversationsList(state.conversations);
      const lastAssistant = [...(conv.messages || [])].reverse().find(m => m.role === 'assistant');
      window.ActivityLog.renderPanel(lastAssistant ? lastAssistant.activity_log || [] : []);
      window.refreshChangesPanel();
      if (conv.workspace_id && (!state.activeWorkspace || state.activeWorkspace.id !== conv.workspace_id)) {
        selectWorkspace(conv.workspace_id);
      }
      return conv;
    } catch (err) {
      window.showToast('Could not open chat: ' + err.message, 'error');
      return null;
    }
  }

  window.deleteConversation = async function (convId) {
    if (!confirm('Delete this conversation? Files are not affected.')) return;
    try {
      await window.API.deleteConversation(convId);
      if (window.Store.getState().activeConversationId === convId) window.newChat();
      loadConversations();
    } catch (err) {
      window.showToast('Could not delete: ' + err.message, 'error');
    }
  };

  // ── Changes panel (right sidebar) ───────────────────────────────────────
  window.refreshChangesPanel = function () {
    const panel = document.getElementById('changes-panel');
    if (!panel) return;
    const changes = window.Store.getState().messages.filter(m => m.change).map(m => m.change);
    if (!changes.length) {
      panel.innerHTML = '<div class="changes-empty">No proposed changes in this chat</div>';
      return;
    }
    panel.innerHTML = changes.slice().reverse().map(c => `
      <div class="change-item" onclick="document.getElementById('change-${escapeHtml(c.id)}')?.scrollIntoView({behavior:'smooth', block:'center'})">
        <span class="change-status ${escapeHtml(c.status)}"></span>
        <div class="change-item-body">
          <div class="change-path">${c.files.map(f => escapeHtml(f.path)).join(', ')}</div>
          <div class="change-item-meta">${escapeHtml(c.status)} · <span class="stat-add">+${c.additions}</span> <span class="stat-remove">−${c.deletions}</span></div>
        </div>
      </div>`).join('');
  };

  // ── Message rendering ───────────────────────────────────────────────────
  const EMPTY_STATE = `
    <div class="empty-state" id="empty-state">
      <div class="empty-icon">⚡</div>
      <h2>What should we work on?</h2>
      <p>Ask about your codebase, or ask for a change — CodeSage shows a diff and writes nothing until you accept it.</p>
      <div class="suggestions-grid">
        <button class="suggestion-btn" onclick="sendSuggestion('Explain this project')">Explain this project</button>
        <button class="suggestion-btn" onclick="sendSuggestion('How does authentication work in this codebase?')">How does auth work?</button>
        <button class="suggestion-btn" onclick="sendSuggestion('Find potential bugs in the error handling')">Find potential bugs</button>
        <button class="suggestion-btn" onclick="sendSuggestion('Add a docstring to the main module explaining its purpose')">Add a docstring</button>
      </div>
    </div>`;

  function renderMessages(messages, scroll = true) {
    const container = document.getElementById('messages-container');
    if (!container) return;
    if (!messages || !messages.length) {
      container.innerHTML = EMPTY_STATE;
      return;
    }
    const lastUserIdx = messages.map(m => m.role).lastIndexOf('user');
    container.innerHTML = messages.map((m, i) => messageHtml(m, { isLastTurn: i >= lastUserIdx })).join('');
    enhanceCode(container);
    if (scroll) container.scrollTop = container.scrollHeight;
  }

  function metricsHtml(msg) {
    const m = (msg.meta || {}).metrics;
    if (!m) return '';
    const parts = [];
    if (m.model) parts.push(escapeHtml(m.model));
    if (m.total_ms) parts.push(`${(m.total_ms / 1000).toFixed(1)}s`);
    if (m.ttft_ms) parts.push(`first token ${(m.ttft_ms / 1000).toFixed(1)}s`);
    if (m.input_tokens || m.output_tokens) {
      parts.push(`${(m.input_tokens || 0).toLocaleString()} in / ${(m.output_tokens || 0).toLocaleString()} out${m.tokens_estimated ? ' (est.)' : ''}`);
    }
    if (m.cost_usd) parts.push(`~$${m.cost_usd.toFixed(4)}`);
    return `<div class="message-metrics" title="Stage timings (ms): ${escapeHtml(JSON.stringify(m.timings || {}))}">${parts.join(' · ')}</div>`;
  }

  function messageHtml(msg, { isLastTurn = false } = {}) {
    const isAssistant = msg.role === 'assistant';
    const status = msg.status || 'complete';
    const meta = msg.meta || {};
    let body = '';
    if (isAssistant && msg.activity_log && msg.activity_log.length) {
      body += window.ActivityLog.renderInline(msg.activity_log, { stopped: status === 'cancelled' });
    }
    if (isAssistant && status === 'error') {
      const err = meta.error || {};
      body += `<div class="message-error"><strong>Request failed.</strong> ${escapeHtml(err.message || msg.content.replace(/^\*\*Request failed:\*\*\s*/, ''))}</div>`;
    } else {
      body += `<div class="markdown">${isAssistant ? renderMarkdown(msg.content) : `<p class="user-text">${escapeHtml(msg.content)}</p>`}</div>`;
    }
    if (msg.change) body += window.Proposals.renderChange(msg.change, { hideSummary: !!msg.change.summary && (msg.content || '').startsWith(msg.change.summary) });
    if (msg.command) body += window.Proposals.renderCommand(msg.command);

    let footer = '';
    if (isAssistant) {
      const retry = (status === 'error' && (meta.error || {}).retryable !== false) || status === 'cancelled';
      footer = `<div class="message-footer">${metricsHtml(msg)}
        ${retry && isLastTurn ? `<button class="btn btn-secondary btn-sm" onclick="retryLast()">↻ Retry</button>` : ''}
        ${status === 'cancelled' ? '<span class="status-badge rejected">Stopped</span>' : ''}</div>`;
    }
    const sender = isAssistant ? 'CodeSage' : 'You';
    return `
      <div class="message message-${msg.role} ${status !== 'complete' ? 'message-' + status : ''}" data-msg-id="${msg.id || ''}">
        <div class="message-avatar">${isAssistant ? '⚡' : '👤'}</div>
        <div class="message-main">
          <div class="message-sender">${sender}${isAssistant && meta.mode ? ` <span class="mode-chip">${escapeHtml(meta.mode)} · ${escapeHtml((meta.scope || '').replace('_', ' '))}</span>` : ''}</div>
          <div class="message-body">${body}</div>
          ${footer}
        </div>
      </div>`;
  }

  window.sendSuggestion = function (text) {
    const input = document.getElementById('chat-input');
    input.value = text;
    sendMessage();
  };

  window.handleInputKeydown = function (e) {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      sendMessage();
    }
  };

  window.autoResizeTextarea = function (el) {
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 200) + 'px';
  };

  // ── Agent state machine ────────────────────────────────────────────────
  let agentState = 'IDLE';          // IDLE | RUNNING | STOPPING
  let currentRun = null;            // {requestId, controller, stopTimer}

  function setAgentState(next) {
    agentState = next;
    const input = document.getElementById('chat-input');
    const btn = document.getElementById('send-btn');
    const statusEl = document.getElementById('input-status');
    window.Store.setStreaming(next !== 'IDLE');
    if (next === 'IDLE') {
      input.disabled = false;
      input.placeholder = 'Ask about your code or request a change… (Enter to send, Shift+Enter for newline)';
      btn.className = 'send-btn';
      btn.title = 'Send (Enter)';
      btn.innerHTML = '<span class="send-icon">↑</span>';
      btn.onclick = () => sendMessage();
      btn.disabled = false;
      statusEl.textContent = '';
    } else {
      input.disabled = true;
      input.placeholder = next === 'STOPPING' ? 'Stopping…' : 'CodeSage is working… (Esc to stop)';
      btn.className = 'send-btn stop';
      btn.title = 'Stop (Esc)';
      btn.innerHTML = '<span class="stop-icon"></span>';
      btn.onclick = () => stopCurrentRequest();
      btn.disabled = next === 'STOPPING';
      statusEl.textContent = next === 'STOPPING' ? 'Stopping…' : '';
    }
  }

  window.stopCurrentRequest = async function () {
    if (agentState !== 'RUNNING' || !currentRun) return;
    const run = currentRun;
    setAgentState('STOPPING');
    try {
      await window.API.cancelChat(run.requestId);   // stops the server run + provider request
    } catch (e) { /* already finished — fine */ }
    // The server answers with a 'cancelled' event; abort the stream if it doesn't arrive promptly.
    run.stopTimer = setTimeout(() => run.controller.abort(), 2500);
  };

  window.retryLast = function () {
    const msgs = window.Store.getState().messages;
    const lastUser = [...msgs].reverse().find(m => m.role === 'user');
    if (lastUser) sendMessage(lastUser.content, { retry: true });
  };

  async function sendMessage(overrideText, { retry = false } = {}) {
    const input = document.getElementById('chat-input');
    const text = (overrideText !== undefined ? overrideText : input.value).trim();
    if (!text || agentState !== 'IDLE') return;
    const state = window.Store.getState();
    if (!state.selectedProvider || !state.selectedModel) {
      window.showToast('Choose a model first (top right)', 'error');
      return;
    }
    if (overrideText === undefined) {
      input.value = '';
      input.style.height = 'auto';
    }

    const requestId = 'req_' + Math.random().toString(36).slice(2, 12);
    const controller = new AbortController();
    currentRun = { requestId, controller, stopTimer: null };
    setAgentState('RUNNING');

    // Optimistic UI: user turn + live assistant turn
    let msgs = window.Store.getState().messages.slice();
    if (retry) {
      while (msgs.length && msgs[msgs.length - 1].role === 'assistant' && msgs[msgs.length - 1].status !== 'complete') msgs.pop();
    } else {
      msgs.push({ role: 'user', content: text, status: 'complete' });
    }
    window.Store.setState({ messages: msgs });
    renderMessages(msgs);
    const container = document.getElementById('messages-container');
    const live = document.createElement('div');
    live.className = 'message message-assistant message-live';
    live.innerHTML = `<div class="message-avatar">⚡</div>
      <div class="message-main"><div class="message-sender">CodeSage <span class="mode-chip" id="live-mode"></span></div>
      <div class="message-body"><div id="live-activity"></div><div class="markdown" id="live-text"></div>
      <div id="live-progress" class="live-progress"></div></div></div>`;
    container.appendChild(live);
    container.scrollTop = container.scrollHeight;

    let streamed = '';
    let activity = [];
    let convId = state.activeConversationId;
    let renderPending = false;
    const nearBottom = () => container.scrollHeight - container.scrollTop - container.clientHeight < 120;

    function paint() {
      renderPending = false;
      const stick = nearBottom();
      document.getElementById('live-activity').innerHTML = window.ActivityLog.renderInline(activity, { open: true });
      const t = document.getElementById('live-text');
      t.innerHTML = renderMarkdown(streamed) + (streamed ? '<span class="stream-cursor"></span>' : '');
      if (stick) container.scrollTop = container.scrollHeight;
    }
    const schedulePaint = () => { if (!renderPending) { renderPending = true; requestAnimationFrame(paint); } };

    async function finish(kind, payload) {
      if (currentRun && currentRun.stopTimer) clearTimeout(currentRun.stopTimer);
      currentRun = null;
      setAgentState('IDLE');
      if (convId) {
        const conv = await openConversation(convId, { scroll: true });
        if (conv && kind === 'done') {
          const last = conv.messages[conv.messages.length - 1];
          if (last && last.command) window.Proposals.maybeAutoApprove(last.command);
          const card = last && (last.change || last.command)
            && document.querySelector(`[data-msg-id="${last.id}"] .change-card`);
          if (card) card.scrollIntoView({ block: 'start', behavior: 'smooth' });
        }
      } else {
        // request never reached the server: show the error inline
        live.querySelector('.message-body').innerHTML =
          `<div class="message-error"><strong>Request failed.</strong> ${escapeHtml(payload && payload.message)}</div>`;
      }
      loadConversations();
      if (kind === 'error') window.showToast('Request failed — details are shown in the chat', 'error');
      if (kind === 'cancelled') window.showToast('Stopped', 'info');
      document.getElementById('chat-input').focus();
    }

    await window.API.streamChat({
      conversation_id: convId,
      message: text,
      provider: state.selectedProvider,
      model: state.selectedModel,
      use_repo: state.useRepo,
      workspace_id: state.activeWorkspace ? state.activeWorkspace.id : null,
      mode: state.mode,
      request_id: requestId,
      retry,
    }, {
      onStart: (ev) => {
        if (!convId) {
          convId = ev.conversation_id;
          window.Store.setState({ activeConversationId: convId });
          document.getElementById('chat-title').textContent = text.slice(0, 80);
        }
      },
      onPlan: (ev) => {
        const chip = document.getElementById('live-mode');
        if (chip) chip.textContent = `${ev.mode} · ${ev.scope.replace('_', ' ')}`;
      },
      onActivity: (items) => {
        activity = items;
        window.Store.setActivityLog(items);
        schedulePaint();
      },
      onChunk: (chunk) => { streamed += chunk; schedulePaint(); },
      onProgress: (ev) => {
        const el = document.getElementById('live-progress');
        if (el) el.textContent = `Receiving edit plan… ${ev.chars.toLocaleString()} characters`;
      },
      onDone: () => finish('done'),
      onCancelled: () => finish('cancelled'),
      onError: (err) => finish('error', err),
    }, controller.signal);
  }
  window.sendMessage = sendMessage;

  // ── Header controls ─────────────────────────────────────────────────────
  window.toggleSidebar = function () { document.getElementById('sidebar').classList.toggle('open'); };
  window.toggleContextPanel = function () { document.getElementById('context-panel').classList.toggle('collapsed'); };

  window.switchContextTab = function (tabName) {
    document.querySelectorAll('.context-tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tabName));
    document.querySelectorAll('.context-tab-content').forEach(c => c.classList.toggle('active', c.id === `tab-${tabName}`));
  };

  function setMode(mode, silent = false) {
    window.Store.setMode(mode);
    document.getElementById('mode-quick').classList.toggle('active', mode === 'quick');
    document.getElementById('mode-deep').classList.toggle('active', mode === 'deep');
    if (!silent) window.showToast(mode === 'quick' ? 'Quick: focused context' : 'Deep: wider repository context', 'info');
  }
  window.setMode = (m) => setMode(m);

  window.toggleRepoContext = function () {
    window.Store.setUseRepo(!window.Store.getState().useRepo);
    updateRepoStatusIndicator();
  };

  function updateRepoStatusIndicator() {
    const state = window.Store.getState();
    const btn = document.getElementById('repo-context-btn');
    const status = document.getElementById('repo-context-status');
    btn.classList.toggle('active', state.useRepo);
    if (state.useRepo && state.activeWorkspace) {
      status.textContent = `Workspace: ${state.activeWorkspace.name}`;
      status.classList.add('active');
    } else if (state.useRepo) {
      status.textContent = 'No workspace selected — add one to ask about code or make changes';
      status.classList.remove('active');
    } else {
      status.textContent = 'Workspace context off — general questions only';
      status.classList.remove('active');
    }
  }

  // ── Settings ────────────────────────────────────────────────────────────
  window.showSettings = function () {
    document.getElementById('settings-modal').classList.remove('hidden');
    switchSettingsSection('account');
  };

  window.switchSettingsSection = async function (section) {
    document.querySelectorAll('.settings-nav-btn').forEach(b => b.classList.toggle('active', b.dataset.section === section));
    const content = document.getElementById('settings-content');
    const user = window.Store.getState().user || {};
    const models = window.Store.getState().models || {};

    if (section === 'account') {
      content.innerHTML = `
        <div class="settings-section">
          <h4>Account</h4>
          <div class="form-group"><label>Email</label><input type="text" value="${escapeHtml(user.email || '')}" disabled /></div>
          <div class="form-group"><label>Display name</label><input type="text" id="settings-name" value="${escapeHtml(user.name || '')}" /></div>
          <button class="btn btn-primary" onclick="saveAccountSettings()">Save</button>
          <div class="settings-divider"></div>
          <button class="btn btn-danger" onclick="logout()">Sign out</button>
        </div>`;
    } else if (section === 'ai') {
      content.innerHTML = `
        <div class="settings-section">
          <h4>LLM providers</h4>
          <p class="settings-hint">API keys are configured on the server (environment variables) and never sent to the browser. Ollama is optional.</p>
          ${Object.entries(models).map(([k, p]) => `
            <div class="provider-row">
              <div><div class="provider-row-name">${escapeHtml(p.label || k)}</div>
                <div class="provider-row-sub">${p.error ? escapeHtml(p.error) : `${(p.models || []).length} models · default ${escapeHtml(p.default_model || '')}`}</div></div>
              <span class="provider-status ${p.status === 'ready' ? 'available' : p.status === 'error' ? 'warning' : 'unavailable'}">${escapeHtml(p.status)}</span>
            </div>`).join('')}
          <button class="btn btn-secondary" onclick="reloadModels().then(() => switchSettingsSection('ai'))">Refresh</button>
        </div>`;
    } else if (section === 'usage') {
      content.innerHTML = '<div class="sidebar-loading">Loading usage…</div>';
      try {
        const [today, all] = await Promise.all([window.API.getUsage('today'), window.API.getUsage('all')]);
        const tile = (label, value, cls = '') => `<div class="usage-stat"><div class="usage-stat-label">${label}</div><div class="usage-stat-value ${cls}">${value}</div></div>`;
        content.innerHTML = `
          <div class="settings-section">
            <h4>Usage</h4>
            <div class="usage-grid">
              ${tile('REQUESTS TODAY', today.requests || 0)}
              ${tile('TOKENS TODAY', (today.total_tokens || 0).toLocaleString())}
              ${tile('ALL-TIME TOKENS', (all.total_tokens || 0).toLocaleString())}
              ${tile('EST. COST (ALL)', '$' + (all.cost_usd || 0).toFixed(4))}
            </div>
            <table class="usage-table"><thead><tr><th>Provider</th><th>Requests</th><th>Tokens</th><th>Est. cost</th></tr></thead><tbody>
            ${Object.entries(all.by_provider || {}).map(([p, v]) => `<tr><td>${escapeHtml(p)}</td><td>${v.requests}</td>
              <td>${(v.input_tokens + v.output_tokens).toLocaleString()}</td><td>$${v.cost_usd.toFixed(4)}</td></tr>`).join('') || '<tr><td colspan="4">No usage yet</td></tr>'}
            </tbody></table>
            <p class="settings-hint">Costs are estimates from a static price table.</p>
          </div>`;
      } catch (e) {
        content.innerHTML = '<div class="change-error">Could not load usage data</div>';
      }
    } else if (section === 'permissions') {
      const allowed = Array.from(window.Store.getState().sessionPermissions || []);
      content.innerHTML = `
        <div class="settings-section">
          <h4>Command permissions</h4>
          <p class="settings-hint">Commands always need approval. "Allow for session" remembers an exact command until you reload the page. Destructive commands are blocked by the server regardless.</p>
          ${allowed.length ? allowed.map(cmd => `<div class="perm-item"><code>${escapeHtml(cmd)}</code></div>`).join('') : '<div class="settings-hint">No commands allowed for this session.</div>'}
        </div>`;
    } else if (section === 'appearance') {
      content.innerHTML = '<div class="settings-section"><h4>Appearance</h4><p class="settings-hint">CodeSage uses a dark developer theme.</p></div>';
    }
  };

  window.saveAccountSettings = async function () {
    try {
      const res = await window.API.updateMe({ name: document.getElementById('settings-name').value.trim() });
      window.Store.setUser(res.user);
      renderUserProfile(res.user);
      window.showToast('Profile updated', 'success');
    } catch (err) {
      window.showToast(`Error: ${err.message}`, 'error');
    }
  };

  window.logout = function () {
    window.API.clearToken();
    window.location.reload();
  };

  // ── Keyboard shortcuts ──────────────────────────────────────────────────
  function setupGlobalKeybindings() {
    document.addEventListener('keydown', (e) => {
      const mod = e.ctrlKey || e.metaKey;
      if (e.key === 'Escape' && agentState === 'RUNNING') {
        e.preventDefault();
        stopCurrentRequest();
      } else if (e.key === 'Escape') {
        ['diff-modal', 'workspace-modal', 'settings-modal'].forEach(closeModal);
      } else if (mod && e.key.toLowerCase() === 'n') {
        e.preventDefault();
        window.newChat();
      } else if (mod && e.key.toLowerCase() === 'b') {
        e.preventDefault();
        window.toggleSidebar();
      } else if (mod && e.key === ',') {
        e.preventDefault();
        window.showSettings();
      } else if (e.key === '/' && document.activeElement.tagName !== 'TEXTAREA' && document.activeElement.tagName !== 'INPUT') {
        e.preventDefault();
        document.getElementById('chat-input').focus();
      }
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bootstrapApp);
  else bootstrapApp();
})();
