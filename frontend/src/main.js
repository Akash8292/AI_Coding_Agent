/**
 * CodeSage Main Application Coordinator
 */

(function () {
  let pendingPermissionCallback = null;

  // Initialize markdown renderer if available
  if (window.marked) {
    window.marked.setOptions({
      highlight: function (code, lang) {
        if (window.hljs && lang && window.hljs.getLanguage(lang)) {
          try {
            return window.hljs.highlight(code, { language: lang }).value;
          } catch (e) {}
        }
        return window.hljs ? window.hljs.highlightAuto(code).value : code;
      },
      breaks: true,
      gfm: true,
    });
  }

  // Global Toast Helper
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
      toast.style.transform = 'translateX(20px)';
      toast.style.transition = 'all 0.2s ease';
      setTimeout(() => toast.remove(), 200);
    }, 3500);
  };

  // Auth Handling
  window.switchTab = function (tab) {
    const loginTab = document.getElementById('login-tab');
    const regTab = document.getElementById('register-tab');
    const regFields = document.getElementById('register-fields');
    const submitBtn = document.getElementById('auth-submit');
    const errorEl = document.getElementById('auth-error');

    if (errorEl) errorEl.classList.add('hidden');

    if (tab === 'register') {
      loginTab.classList.remove('active');
      regTab.classList.add('active');
      regFields.classList.remove('hidden');
      submitBtn.textContent = 'Create Account';
    } else {
      regTab.classList.remove('active');
      loginTab.classList.add('active');
      regFields.classList.add('hidden');
      submitBtn.textContent = 'Sign In';
    }
  };

  window.handleAuth = async function (e) {
    e.preventDefault();
    const isRegister = document.getElementById('register-tab').classList.contains('active');
    const email = document.getElementById('auth-email').value.trim();
    const password = document.getElementById('auth-password').value;
    const name = document.getElementById('auth-name') ? document.getElementById('auth-name').value.trim() : '';
    const errorEl = document.getElementById('auth-error');
    const submitBtn = document.getElementById('auth-submit');

    errorEl.classList.add('hidden');
    submitBtn.disabled = true;

    try {
      if (isRegister) {
        await window.API.register(email, password, name);
      } else {
        await window.API.login(email, password);
      }
      await bootstrapApp();
    } catch (err) {
      errorEl.textContent = err.message || 'Authentication failed';
      errorEl.classList.remove('hidden');
    } finally {
      submitBtn.disabled = false;
    }
  };

  window.onAuthUnauthorized = function () {
    showAuthView();
  };

  function showAuthView() {
    document.getElementById('auth-page').classList.remove('hidden');
    document.getElementById('app').classList.add('hidden');
  }

  function showAppView() {
    document.getElementById('auth-page').classList.add('hidden');
    document.getElementById('app').classList.remove('hidden');
  }

  // App Bootstrap
  async function bootstrapApp() {
    const token = window.API.getToken();
    if (!token) {
      showAuthView();
      return;
    }

    try {
      const meRes = await window.API.getMe();
      window.Store.setUser(meRes.user);
      renderUserProfile(meRes.user);
      showAppView();

      // Load initial state in parallel
      await Promise.allSettled([
        loadModels(),
        loadWorkspaces(),
        loadConversations(),
      ]);

      // Initialize UI components
      window.ModelSelector.init();
      window.ActivityLog.init();
      window.CommandPalette.init();
      setupGlobalKeybindings();

    } catch (err) {
      console.error('Bootstrap error:', err);
      showAuthView();
    }
  }

  function renderUserProfile(user) {
    if (!user) return;
    const nameEl = document.getElementById('user-name');
    const emailEl = document.getElementById('user-email');
    const avatarEl = document.getElementById('user-avatar');

    if (nameEl) nameEl.textContent = user.name || user.email.split('@')[0];
    if (emailEl) emailEl.textContent = user.email;
    if (avatarEl) {
      const initial = (user.name || user.email || 'U')[0].toUpperCase();
      avatarEl.textContent = initial;
    }
  }

  async function loadModels() {
    try {
      const models = await window.API.getModels();
      window.Store.setModels(models);
      window.ModelSelector.render();
    } catch (err) {
      console.warn('Could not load models:', err);
    }
  }

  async function loadWorkspaces() {
    try {
      const res = await window.API.listWorkspaces();
      window.Store.setWorkspaces(res.workspaces || []);
      renderWorkspacesList(res.workspaces || []);

      if (res.workspaces && res.workspaces.length > 0 && !window.Store.getState().activeWorkspace) {
        selectWorkspace(res.workspaces[0].id);
      }
    } catch (err) {
      console.warn('Could not load workspaces:', err);
    }
  }

  function renderWorkspacesList(workspaces) {
    const container = document.getElementById('workspace-selector');
    if (!container) return;

    if (!workspaces || workspaces.length === 0) {
      container.innerHTML = '<div class="workspace-empty" onclick="showWorkspaceModal()" style="cursor:pointer;">+ Add your first workspace</div>';
      return;
    }

    const activeWs = window.Store.getState().activeWorkspace;
    let html = '<select class="workspace-dropdown-select" onchange="selectWorkspace(this.value)" style="width:100%; padding:6px 8px; font-size:12px; background:var(--bg-2); border:1px solid var(--border); border-radius:var(--radius-sm); color:var(--text-0); outline:none;">';
    for (const ws of workspaces) {
      const isSelected = activeWs && activeWs.id === ws.id;
      html += `<option value="${ws.id}" ${isSelected ? 'selected' : ''}>📁 ${ws.name} (${ws.branch || 'main'})</option>`;
    }
    html += '</select>';
    container.innerHTML = html;
  }

  window.selectWorkspace = async function (wsId) {
    wsId = parseInt(wsId, 10);
    const state = window.Store.getState();
    const ws = (state.workspaces || []).find(w => w.id === wsId);
    if (!ws) return;

    window.Store.setActiveWorkspace(ws);
    renderWorkspacesList(state.workspaces);
    updateRepoStatusIndicator();

    // Fetch workspace files and git status
    loadWorkspaceFiles(ws.id);
    loadGitStatus(ws.id);
  };

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

    if (!files || files.length === 0) {
      container.innerHTML = '<div class="files-empty">No files indexed.<br/><button class="btn btn-secondary" style="margin-top:8px; font-size:11px; padding:4px 8px;" onclick="indexCurrentWorkspace()">Index Now</button></div>';
      return;
    }

    let html = '';
    for (const f of files.slice(0, 100)) {
      html += `
        <div class="file-tree-item" onclick="openFileInChat('${f.path}')" title="${f.path}">
          <span class="file-icon">📄</span>
          <span style="overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${f.path}</span>
        </div>
      `;
    }
    if (files.length > 100) {
      html += `<div style="font-size:11px; color:var(--text-3); padding:4px 6px;">+ ${files.length - 100} more files</div>`;
    }
    container.innerHTML = html;
  }

  window.filterFiles = function (query) {
    const q = (query || '').toLowerCase().trim();
    const all = window.Store.getState().files || [];
    if (!q) {
      renderFileTree(all);
      return;
    }
    const filtered = all.filter(f => f.path.toLowerCase().includes(q));
    renderFileTree(filtered);
  };

  window.openFileInChat = function (filePath) {
    const input = document.getElementById('chat-input');
    if (input) {
      input.value = `Inspect file: ${filePath}\n` + input.value;
      input.focus();
    }
  };

  async function loadGitStatus(wsId) {
    const panel = document.getElementById('git-panel');
    if (!panel) return;

    try {
      const [status, gitLog] = await Promise.allSettled([
        window.API.getGitStatus(wsId),
        window.API.getGitLog(wsId),
      ]);

      const st = status.status === 'fulfilled' ? status.value : null;
      const lg = gitLog.status === 'fulfilled' ? gitLog.value : null;

      if (!st || !st.is_repo) {
        panel.innerHTML = '<div class="git-empty">Directory is not a Git repository</div>';
        return;
      }

      let html = `
        <div class="git-section">
          <div class="git-section-title">CURRENT BRANCH</div>
          <div class="git-branch">⎇ ${st.branch || 'main'}</div>
        </div>
        <div class="git-section">
          <div class="git-section-title">WORKING TREE</div>
      `;

      if (st.clean) {
        html += '<div style="font-size:12px; color:var(--green); padding:4px 0;">✓ Working tree clean</div>';
      } else {
        const changes = st.changes || [];
        for (const c of changes.slice(0, 10)) {
          html += `<div class="git-status-line">${c}</div>`;
        }
      }

      html += `
        </div>
        <div class="git-actions">
          <button class="git-btn" onclick="createCheckpoint()">Save Checkpoint</button>
          <button class="git-btn" onclick="restoreCheckpoint()">Restore</button>
        </div>
      `;

      if (lg && lg.commits && lg.commits.length > 0) {
        html += '<div class="git-section" style="margin-top:14px;"><div class="git-section-title">RECENT COMMITS</div>';
        for (const cm of lg.commits.slice(0, 5)) {
          html += `
            <div style="font-size:11px; padding:4px 0; border-bottom:1px solid var(--border);">
              <div style="color:var(--text-0); font-weight:500;">${cm.subject || cm.hash}</div>
              <div style="color:var(--text-3); font-size:10px;">${cm.author || ''} • ${cm.date || ''}</div>
            </div>
          `;
        }
        html += '</div>';
      }

      panel.innerHTML = html;
    } catch (e) {
      panel.innerHTML = '<div class="git-empty">Could not load Git status</div>';
    }
  }

  window.createCheckpoint = async function () {
    const ws = window.Store.getState().activeWorkspace;
    if (!ws) {
      window.showToast('Please select a workspace first', 'error');
      return;
    }
    try {
      const res = await window.API.createGitCheckpoint(ws.id, 'User manual checkpoint');
      window.showToast(res.message || 'Checkpoint saved', 'success');
      loadGitStatus(ws.id);
    } catch (err) {
      window.showToast(`Checkpoint error: ${err.message}`, 'error');
    }
  };

  window.restoreCheckpoint = async function () {
    const ws = window.Store.getState().activeWorkspace;
    if (!ws) {
      window.showToast('Please select a workspace first', 'error');
      return;
    }
    if (!confirm('Restore most recent checkpoint? Uncommitted changes will be restored.')) return;
    try {
      const res = await window.API.restoreGitCheckpoint(ws.id);
      window.showToast(res.message || 'Checkpoint restored', 'success');
      loadGitStatus(ws.id);
      loadWorkspaceFiles(ws.id);
    } catch (err) {
      window.showToast(`Restore error: ${err.message}`, 'error');
    }
  };

  window.indexCurrentWorkspace = async function () {
    const ws = window.Store.getState().activeWorkspace;
    if (!ws) {
      window.showToast('Select a workspace to index', 'error');
      return;
    }
    window.showToast('Started indexing repository...', 'info');
    try {
      await window.API.indexWorkspace(ws.id);
      pollIndexStatus(ws.id);
    } catch (err) {
      window.showToast(`Indexing failed: ${err.message}`, 'error');
    }
  };

  function pollIndexStatus(wsId) {
    const timer = setInterval(async () => {
      try {
        const st = await window.API.getIndexStatus(wsId);
        if (st.status === 'done') {
          clearInterval(timer);
          window.showToast(`Indexing complete: ${st.total_files} files, ${st.total_chunks} code chunks`, 'success');
          loadWorkspaceFiles(wsId);
        } else if (st.status === 'error') {
          clearInterval(timer);
          window.showToast(`Indexing failed: ${st.error || 'Unknown error'}`, 'error');
        }
      } catch (e) {
        clearInterval(timer);
      }
    }, 1500);
  }

  // Workspace Modal
  window.showWorkspaceModal = function () {
    document.getElementById('workspace-modal').classList.remove('hidden');
    document.getElementById('ws-error').classList.add('hidden');
    document.getElementById('ws-path').value = '';
    document.getElementById('ws-name').value = '';
  };

  window.createWorkspace = async function () {
    const name = document.getElementById('ws-name').value.trim();
    const repoPath = document.getElementById('ws-path').value.trim();
    const errorEl = document.getElementById('ws-error');
    const btn = document.getElementById('ws-submit-btn');

    if (!repoPath) {
      errorEl.textContent = 'Repository path is required';
      errorEl.classList.remove('hidden');
      return;
    }

    btn.disabled = true;
    errorEl.classList.add('hidden');

    try {
      const ws = await window.API.createWorkspace(name, repoPath);
      closeModal('workspace-modal');
      window.showToast(`Workspace "${ws.name}" added`, 'success');
      await loadWorkspaces();
      selectWorkspace(ws.id);
      window.indexCurrentWorkspace();
    } catch (err) {
      errorEl.textContent = err.message || 'Failed to add workspace';
      errorEl.classList.remove('hidden');
    } finally {
      btn.disabled = false;
    }
  };

  window.closeModal = function (modalId) {
    const el = document.getElementById(modalId);
    if (el) el.classList.add('hidden');
  };

  // Conversations
  async function loadConversations(search = '') {
    const listEl = document.getElementById('conversations-list');
    if (!listEl) return;

    try {
      const res = await window.API.listConversations(search);
      window.Store.setConversations(res.conversations || []);
      renderConversationsList(res.conversations || []);
    } catch (err) {
      listEl.innerHTML = '<div class="sidebar-loading">Could not load chats</div>';
    }
  }

  function renderConversationsList(conversations) {
    const listEl = document.getElementById('conversations-list');
    if (!listEl) return;

    if (!conversations || conversations.length === 0) {
      listEl.innerHTML = '<div class="sidebar-loading">No chats yet</div>';
      return;
    }

    const activeId = window.Store.getState().activeConversationId;
    let html = '';
    for (const c of conversations) {
      const isActive = activeId === c.id;
      html += `
        <div class="conv-item ${isActive ? 'active' : ''}" onclick="selectConversation(${c.id})">
          <span class="conv-title">${escapeHtml(c.title || 'Untitled Chat')}</span>
          <div class="conv-actions" onclick="event.stopPropagation()">
            <button class="conv-action-btn" title="Delete chat" onclick="deleteConversation(${c.id})">✕</button>
          </div>
        </div>
      `;
    }
    listEl.innerHTML = html;
  }

  window.filterChats = function (q) {
    loadConversations(q);
  };

  window.newChat = function () {
    window.Store.setActiveConversation(null, []);
    const titleEl = document.getElementById('chat-title');
    if (titleEl) titleEl.textContent = 'New Chat';
    renderMessages([]);
    const state = window.Store.getState();
    renderConversationsList(state.conversations);
    const input = document.getElementById('chat-input');
    if (input) {
      input.value = '';
      input.focus();
    }
  };

  window.selectConversation = async function (convId) {
    try {
      const conv = await window.API.getConversation(convId);
      window.Store.setActiveConversation(conv.id, conv.messages || []);
      const titleEl = document.getElementById('chat-title');
      if (titleEl) titleEl.textContent = conv.title || 'Chat';
      renderMessages(conv.messages || []);
      const state = window.Store.getState();
      renderConversationsList(state.conversations);

      if (conv.workspace_id && (!state.activeWorkspace || state.activeWorkspace.id !== conv.workspace_id)) {
        selectWorkspace(conv.workspace_id);
      }
    } catch (err) {
      window.showToast('Could not open chat: ' + err.message, 'error');
    }
  };

  window.deleteConversation = async function (convId) {
    if (!confirm('Delete this conversation?')) return;
    try {
      await window.API.deleteConversation(convId);
      const state = window.Store.getState();
      if (state.activeConversationId === convId) {
        newChat();
      }
      loadConversations();
    } catch (err) {
      window.showToast('Could not delete: ' + err.message, 'error');
    }
  };

  // Chat Rendering & Interaction
  function renderMessages(messages) {
    const container = document.getElementById('messages-container');
    if (!container) return;

    if (!messages || messages.length === 0) {
      container.innerHTML = `
        <div class="empty-state" id="empty-state">
          <div class="empty-icon">⚡</div>
          <h2>Start building with AI</h2>
          <p>Ask anything about your codebase, debug issues, or request code changes.</p>
          <div class="suggestions-grid">
            <button class="suggestion-btn" onclick="sendSuggestion('Explain this project\\'s architecture')">Explain this project</button>
            <button class="suggestion-btn" onclick="sendSuggestion('Find potential security vulnerabilities')">Find security issues</button>
            <button class="suggestion-btn" onclick="sendSuggestion('How does authentication work in this codebase?')">Explain auth flow</button>
            <button class="suggestion-btn" onclick="sendSuggestion('Add unit tests for the main service')">Add unit tests</button>
          </div>
        </div>
      `;
      return;
    }

    container.innerHTML = '';
    const fragment = document.createDocumentFragment();

    for (const msg of messages) {
      const msgEl = createMessageElement(msg);
      fragment.appendChild(msgEl);
    }

    container.appendChild(fragment);
    container.scrollTop = container.scrollHeight;
  }

  function createMessageElement(msg) {
    const el = document.createElement('div');
    el.className = `message message-${msg.role}`;
    el.style.cssText = 'padding: 16px 24px; display: flex; gap: 14px; border-bottom: 1px solid var(--border);';

    const isAssistant = msg.role === 'assistant';
    const avatar = isAssistant ? '⚡' : '👤';
    const sender = isAssistant ? (msg.model ? `CodeSage (${msg.model})` : 'CodeSage') : 'You';

    let contentHtml = '';
    if (isAssistant && msg.activity_log && msg.activity_log.length > 0) {
      contentHtml += window.ActivityLog.renderInline(msg.activity_log);
    }

    if (window.marked && msg.content) {
      contentHtml += window.marked.parse(msg.content);
    } else {
      contentHtml += `<pre style="white-space:pre-wrap; font-family:inherit;">${escapeHtml(msg.content || '')}</pre>`;
    }

    el.innerHTML = `
      <div style="width:28px; height:28px; border-radius:50%; background:${isAssistant ? 'var(--surface-2)' : 'var(--accent-dim)'}; display:flex; align-items:center; justify-content:center; flex-shrink:0; font-size:14px; margin-top:2px;">
        ${avatar}
      </div>
      <div style="flex:1; min-width:0;">
        <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:6px;">
          <span style="font-size:12px; font-weight:600; color:${isAssistant ? 'var(--accent)' : 'var(--text-1)'};">${sender}</span>
          ${msg.input_tokens ? `<span style="font-size:10px; color:var(--text-3); font-family:var(--font-mono);">${msg.input_tokens + msg.output_tokens} tok</span>` : ''}
        </div>
        <div class="message-body" style="line-height:1.6; color:var(--text-0); font-size:13px;">
          ${contentHtml}
        </div>
      </div>
    `;

    return el;
  }

  window.sendSuggestion = function (text) {
    const input = document.getElementById('chat-input');
    if (input) {
      input.value = text;
      sendMessage();
    }
  };

  window.handleInputKeydown = function (e) {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      sendMessage();
    }
  };

  window.autoResizeTextarea = function (el) {
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 180) + 'px';
  };

  window.sendMessage = async function () {
    const input = document.getElementById('chat-input');
    const text = (input ? input.value : '').trim();
    if (!text) return;

    const state = window.Store.getState();
    if (state.isStreaming) return;

    input.value = '';
    input.style.height = 'auto';

    // Add user message to state & UI
    const userMsg = { role: 'user', content: text };
    window.Store.addMessage(userMsg);

    // Prepare assistant message
    const assistantMsg = { role: 'assistant', content: '', activity_log: [] };
    window.Store.addMessage(assistantMsg);
    window.Store.setStreaming(true, '');

    renderMessages(window.Store.getState().messages);

    const messagesContainer = document.getElementById('messages-container');
    const lastMsgBody = messagesContainer.lastElementChild ? messagesContainer.lastElementChild.querySelector('.message-body') : null;

    try {
      await window.API.streamChat(
        {
          conversation_id: state.activeConversationId,
          message: text,
          provider: state.selectedProvider,
          model: state.selectedModel,
          use_repo: state.useRepo,
          workspace_id: state.activeWorkspace ? state.activeWorkspace.id : null,
          mode: state.mode,
        },
        {
          onActivity: (items) => {
            window.Store.setActivityLog(items);
            if (lastMsgBody) {
              const currentContent = window.Store.getState().streamingContent;
              lastMsgBody.innerHTML = window.ActivityLog.renderInline(items) + (window.marked ? window.marked.parse(currentContent) : currentContent);
              messagesContainer.scrollTop = messagesContainer.scrollHeight;
            }
          },
          onChunk: (chunk) => {
            window.Store.appendStreamingChunk(chunk);
            if (lastMsgBody) {
              const fullText = window.Store.getState().streamingContent;
              const currentActivity = window.Store.getState().activityLog;
              lastMsgBody.innerHTML = (currentActivity && currentActivity.length ? window.ActivityLog.renderInline(currentActivity) : '') + (window.marked ? window.marked.parse(fullText) : fullText);
              messagesContainer.scrollTop = messagesContainer.scrollHeight;
            }
          },
          onDone: (event) => {
            window.Store.setStreaming(false);
            const fullText = window.Store.getState().streamingContent;
            const activity = window.Store.getState().activityLog;
            window.Store.updateLastMessage(fullText, activity);

            if (event.conversation_id && !state.activeConversationId) {
              window.Store.setState({ activeConversationId: event.conversation_id });
              loadConversations();
            }
          },
          onError: (err) => {
            window.Store.setStreaming(false);
            window.Store.updateLastMessage(`⚠️ Error: ${err.message}`);
            renderMessages(window.Store.getState().messages);
          },
        }
      );
    } catch (err) {
      window.Store.setStreaming(false);
      window.Store.updateLastMessage(`⚠️ Connection error: ${err.message}`);
      renderMessages(window.Store.getState().messages);
    }
  };

  // Header and UI Controls
  window.toggleSidebar = function () {
    const sidebar = document.getElementById('sidebar');
    if (sidebar) sidebar.classList.toggle('open');
  };

  window.toggleContextPanel = function () {
    const panel = document.getElementById('context-panel');
    if (panel) panel.classList.toggle('collapsed');
  };

  window.switchContextTab = function (tabName) {
    const tabs = document.querySelectorAll('.context-tab');
    const contents = document.querySelectorAll('.context-tab-content');

    tabs.forEach(t => t.classList.toggle('active', t.dataset.tab === tabName));
    contents.forEach(c => c.classList.toggle('active', c.id === `tab-${tabName}`));
  };

  window.setMode = function (mode) {
    window.Store.setMode(mode);
    const qBtn = document.getElementById('mode-quick');
    const dBtn = document.getElementById('mode-deep');
    if (qBtn && dBtn) {
      qBtn.classList.toggle('active', mode === 'quick');
      dBtn.classList.toggle('active', mode === 'deep');
    }
    window.showToast(`Switched to ${mode === 'quick' ? 'Quick Answer' : 'Deep Investigation'} mode`, 'info');
  };

  window.toggleRepoContext = function () {
    const current = window.Store.getState().useRepo;
    window.Store.setUseRepo(!current);
    updateRepoStatusIndicator();
  };

  function updateRepoStatusIndicator() {
    const state = window.Store.getState();
    const btn = document.getElementById('repo-context-btn');
    const status = document.getElementById('repo-context-status');

    if (btn) {
      btn.classList.toggle('active', state.useRepo);
      btn.style.color = state.useRepo ? 'var(--accent)' : 'var(--text-2)';
    }

    if (status) {
      if (state.useRepo && state.activeWorkspace) {
        status.textContent = `Using repository: ${state.activeWorkspace.name}`;
        status.classList.add('active');
      } else if (state.useRepo && !state.activeWorkspace) {
        status.textContent = 'Repository context enabled (no workspace selected)';
        status.classList.remove('active');
      } else {
        status.textContent = 'Repository context disabled';
        status.classList.remove('active');
      }
    }
  }

  // Permission Modal
  window.requestPermission = function ({ command, reason }) {
    return new Promise((resolve, reject) => {
      const modal = document.getElementById('permission-modal');
      const cmdEl = document.getElementById('permission-command');
      const rsnEl = document.getElementById('permission-reason');

      if (!modal) {
        resolve(true);
        return;
      }

      if (window.Store.hasSessionPermission(command)) {
        resolve(true);
        return;
      }

      cmdEl.textContent = command;
      rsnEl.textContent = reason || 'Execution of local system command required.';
      modal.classList.remove('hidden');

      pendingPermissionCallback = { resolve, reject, command };
    });
  };

  window.allowPermission = function () {
    closeModal('permission-modal');
    if (pendingPermissionCallback) {
      pendingPermissionCallback.resolve(true);
      pendingPermissionCallback = null;
    }
  };

  window.allowPermissionSession = function () {
    closeModal('permission-modal');
    if (pendingPermissionCallback) {
      window.Store.allowSessionPermission(pendingPermissionCallback.command);
      pendingPermissionCallback.resolve(true);
      pendingPermissionCallback = null;
    }
  };

  window.rejectPermission = function () {
    closeModal('permission-modal');
    if (pendingPermissionCallback) {
      pendingPermissionCallback.reject(new Error('Permission rejected by user'));
      pendingPermissionCallback = null;
    }
  };

  // Settings Modal
  window.showSettings = async function () {
    document.getElementById('settings-modal').classList.remove('hidden');
    switchSettingsSection('account');
  };

  window.switchSettingsSection = async function (section) {
    const navBtns = document.querySelectorAll('.settings-nav-btn');
    navBtns.forEach(b => b.classList.toggle('active', b.dataset.section === section));

    const content = document.getElementById('settings-content');
    if (!content) return;

    const user = window.Store.getState().user || {};
    const models = window.Store.getState().models || {};

    if (section === 'account') {
      content.innerHTML = `
        <div class="settings-section">
          <h4>Account Profile</h4>
          <div class="form-group" style="margin-bottom:12px;">
            <label style="display:block; font-size:12px; margin-bottom:4px;">Email</label>
            <input type="text" value="${escapeHtml(user.email || '')}" disabled style="width:100%; padding:8px; background:var(--bg-2); border:1px solid var(--border); border-radius:var(--radius-sm); color:var(--text-1);" />
          </div>
          <div class="form-group" style="margin-bottom:12px;">
            <label style="display:block; font-size:12px; margin-bottom:4px;">Display Name</label>
            <input type="text" id="settings-name" value="${escapeHtml(user.name || '')}" style="width:100%; padding:8px; background:var(--bg-2); border:1px solid var(--border); border-radius:var(--radius-sm); color:var(--text-0);" />
          </div>
          <button class="btn btn-primary" onclick="saveAccountSettings()">Save Profile</button>
          <div style="margin-top:24px; padding-top:16px; border-top:1px solid var(--border);">
            <button class="btn btn-danger" onclick="logout()">Sign Out</button>
          </div>
        </div>
      `;
    } else if (section === 'ai') {
      let providersHtml = '';
      for (const [pKey, pInfo] of Object.entries(models)) {
        const isReady = pInfo.available;
        providersHtml += `
          <div style="display:flex; align-items:center; justify-content:space-between; padding:10px; background:var(--bg-2); border-radius:var(--radius-sm); margin-bottom:8px; border:1px solid var(--border);">
            <div>
              <div style="font-weight:600; font-size:13px;">${pInfo.name || pKey}</div>
              <div style="font-size:11px; color:var(--text-2);">${(pInfo.models || []).join(', ')}</div>
            </div>
            <span class="provider-status ${isReady ? 'available' : 'unavailable'}">${isReady ? 'Ready' : 'Not Configured'}</span>
          </div>
        `;
      }
      content.innerHTML = `
        <div class="settings-section">
          <h4>LLM Providers & Models</h4>
          <p style="font-size:12px; color:var(--text-2); margin-bottom:12px;">Set your API keys in your environment variables (.env) or docker configuration.</p>
          ${providersHtml}
        </div>
      `;
    } else if (section === 'usage') {
      content.innerHTML = '<div class="sidebar-loading">Loading usage stats...</div>';
      try {
        const stats = await window.API.getUsage('all');
        content.innerHTML = `
          <div class="settings-section">
            <h4>API Usage & Tokens</h4>
            <div style="display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-bottom:16px;">
              <div class="usage-stat">
                <div class="usage-stat-label">TOTAL REQUESTS</div>
                <div class="usage-stat-value">${stats.requests || 0}</div>
              </div>
              <div class="usage-stat">
                <div class="usage-stat-label">TOTAL TOKENS</div>
                <div class="usage-stat-value">${(stats.total_tokens || 0).toLocaleString()}</div>
              </div>
            </div>
            <div class="usage-stat">
              <div class="usage-stat-label">ESTIMATED COST</div>
              <div class="usage-stat-value" style="color:var(--green);">$${(stats.cost_usd || 0).toFixed(4)}</div>
            </div>
          </div>
        `;
      } catch (e) {
        content.innerHTML = '<div style="color:var(--red);">Could not load usage data</div>';
      }
    } else if (section === 'permissions') {
      const allowed = Array.from(window.Store.getState().sessionPermissions || []);
      content.innerHTML = `
        <div class="settings-section">
          <h4>Execution Permissions</h4>
          <p style="font-size:12px; color:var(--text-2); margin-bottom:12px;">Commands approved for execution during this session:</p>
          ${allowed.length ? allowed.map(cmd => `<div style="font-family:var(--font-mono); font-size:12px; padding:6px 8px; background:var(--bg-2); border-radius:4px; margin-bottom:4px;">${escapeHtml(cmd)}</div>`).join('') : '<div style="font-size:12px; color:var(--text-3);">No session permissions granted yet.</div>'}
        </div>
      `;
    } else if (section === 'appearance') {
      content.innerHTML = `
        <div class="settings-section">
          <h4>Appearance & Theme</h4>
          <p style="font-size:12px; color:var(--text-2);">CodeSage uses an optimized developer dark theme by default.</p>
        </div>
      `;
    }
  };

  window.saveAccountSettings = async function () {
    const name = document.getElementById('settings-name').value.trim();
    try {
      const res = await window.API.updateMe({ name });
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

  function setupGlobalKeybindings() {
    document.addEventListener('keydown', (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'n') {
        e.preventDefault();
        newChat();
      } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'b') {
        e.preventDefault();
        toggleSidebar();
      }
    });
  }

  function escapeHtml(str) {
    if (!str) return '';
    return str
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  // Initialize on DOM ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bootstrapApp);
  } else {
    bootstrapApp();
  }
})();
