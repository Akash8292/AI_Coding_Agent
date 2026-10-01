/**
 * CodeSage API Client
 * Handles authentication, REST endpoints, and SSE chat streaming.
 */

const API = (() => {
  // The backend serves this frontend, so API calls are same-origin by default.
  // Set window.CODESAGE_API_URL before this script to point at a separate API host.
  const BASE_URL = (window.CODESAGE_API_URL || '').replace(/\/$/, '');

  function getToken() {
    return localStorage.getItem('codesage_token') || '';
  }

  function setToken(token) {
    if (token) {
      localStorage.setItem('codesage_token', token);
    } else {
      localStorage.removeItem('codesage_token');
    }
  }

  function clearToken() {
    localStorage.removeItem('codesage_token');
  }

  async function request(path, options = {}) {
    const url = `${BASE_URL}${path}`;
    const headers = {
      'Content-Type': 'application/json',
      ...(options.headers || {}),
    };

    const token = getToken();
    if (token) {
      headers['Authorization'] = `Bearer ${token}`;
    }

    const response = await fetch(url, {
      ...options,
      headers,
    });

    if (response.status === 401) {
      clearToken();
      if (typeof window.onAuthUnauthorized === 'function') {
        window.onAuthUnauthorized();
      }
    }

    const data = await response.json().catch(() => ({}));

    if (!response.ok) {
      const errorMsg = data.error || `HTTP ${response.status}: ${response.statusText}`;
      const err = new Error(errorMsg);
      err.status = response.status;
      err.data = data;
      throw err;
    }

    return data;
  }

  // Auth
  async function login(email, password) {
    const res = await request('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email, password }),
    });
    if (res.token) setToken(res.token);
    return res;
  }

  async function register(email, password, name) {
    const res = await request('/auth/register', {
      method: 'POST',
      body: JSON.stringify({ email, password, name }),
    });
    if (res.token) setToken(res.token);
    return res;
  }

  async function getMe() {
    return request('/auth/me');
  }

  async function updateMe(updates) {
    return request('/auth/me', {
      method: 'PUT',
      body: JSON.stringify(updates),
    });
  }

  // Workspaces
  async function listWorkspaces() {
    return request('/api/workspaces');
  }

  async function createWorkspace(name, repo_path, git_url) {
    return request('/api/workspaces', {
      method: 'POST',
      body: JSON.stringify({ name, repo_path, git_url }),
    });
  }

  async function getWorkspace(id) {
    return request(`/api/workspaces/${id}`);
  }

  async function deleteWorkspace(id) {
    return request(`/api/workspaces/${id}`, { method: 'DELETE' });
  }

  async function indexWorkspace(id, force = false) {
    return request(`/api/workspaces/${id}/index`, { method: 'POST', body: JSON.stringify({ force }) });
  }

  async function getIndexStatus(id) {
    return request(`/api/workspaces/${id}/index/status`);
  }

  async function listFiles(id) {
    return request(`/api/workspaces/${id}/files`);
  }

  async function searchWorkspace(id, query) {
    return request(`/api/workspaces/${id}/search?q=${encodeURIComponent(query)}`);
  }

  async function getGitStatus(id) {
    return request(`/api/workspaces/${id}/git/status`);
  }

  async function getGitDiff(id, path) {
    const query = path ? `?path=${encodeURIComponent(path)}` : '';
    return request(`/api/workspaces/${id}/git/diff${query}`);
  }

  async function getGitLog(id) {
    return request(`/api/workspaces/${id}/git/log`);
  }

  async function createGitCheckpoint(id, message) {
    return request(`/api/workspaces/${id}/git/checkpoint`, {
      method: 'POST',
      body: JSON.stringify({ message }),
    });
  }

  async function restoreGitCheckpoint(id, sha) {
    return request(`/api/workspaces/${id}/git/restore`, { method: 'POST', body: JSON.stringify({ sha }) });
  }

  async function listGitCheckpoints(id) {
    return request(`/api/workspaces/${id}/git/checkpoints`);
  }

  // Conversations
  async function listConversations(query = '', archived = false) {
    const params = new URLSearchParams();
    if (query) params.set('q', query);
    if (archived) params.set('archived', 'true');
    return request(`/api/conversations?${params.toString()}`);
  }

  async function createConversation(title, workspace_id, provider, model) {
    return request('/api/conversations', {
      method: 'POST',
      body: JSON.stringify({ title, workspace_id, provider, model }),
    });
  }

  async function getConversation(id) {
    return request(`/api/conversations/${id}`);
  }

  async function updateConversation(id, updates) {
    return request(`/api/conversations/${id}`, {
      method: 'PUT',
      body: JSON.stringify(updates),
    });
  }

  async function deleteConversation(id) {
    return request(`/api/conversations/${id}`, { method: 'DELETE' });
  }

  // Chat Streaming (SSE)
  // handlers: onStart, onPlan, onActivity, onChunk, onProgress, onProposal, onCommand,
  //           onDone, onCancelled, onError. Pass `signal` (AbortController) to abort.
  async function streamChat(body, handlers = {}, signal = undefined) {
    const headers = { 'Content-Type': 'application/json' };
    const token = getToken();
    if (token) headers['Authorization'] = `Bearer ${token}`;
    if (body.request_id) headers['X-Request-ID'] = body.request_id;

    let terminal = false;
    const dispatch = (event) => {
      const map = {
        start: 'onStart', plan: 'onPlan', activity: 'onActivity', chunk: 'onChunk',
        progress: 'onProgress', proposal: 'onProposal', command: 'onCommand',
        done: 'onDone', cancelled: 'onCancelled', error: 'onError',
      };
      if (['done', 'cancelled', 'error'].includes(event.type)) terminal = true;
      const fn = handlers[map[event.type]];
      if (!fn) return;
      if (event.type === 'error') {
        const err = new Error(event.message || 'Request failed');
        err.kind = event.kind; err.retryable = event.retryable; err.message_id = event.message_id;
        fn(err);
      } else if (event.type === 'chunk') {
        fn(event.content);
      } else if (event.type === 'activity') {
        fn(event.items);
      } else {
        fn(event);
      }
    };

    try {
      const response = await fetch(`${BASE_URL}/api/chat`, {
        method: 'POST', headers, body: JSON.stringify(body), signal,
      });
      if (response.status === 401 && typeof window.onAuthUnauthorized === 'function') {
        clearToken();
        window.onAuthUnauthorized();
      }
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        const err = new Error(data.error || `Error ${response.status}: ${response.statusText}`);
        err.kind = data.kind || `http_${response.status}`;
        err.retryable = response.status === 429 || response.status >= 500;
        throw err;
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder('utf-8');
      let buffer = '';
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buffer.indexOf('\n\n')) !== -1) {
          const block = buffer.slice(0, idx);
          buffer = buffer.slice(idx + 2);
          for (const line of block.split('\n')) {
            if (!line.startsWith('data: ')) continue;   // ': ping' heartbeats etc.
            try {
              dispatch(JSON.parse(line.slice(6)));
            } catch (e) {
              console.warn('Bad SSE event', line, e);
            }
          }
        }
      }
      if (!terminal) {
        const err = new Error('The connection closed before the response finished.');
        err.kind = 'connection'; err.retryable = true;
        throw err;
      }
    } catch (err) {
      if (err.name === 'AbortError') {
        if (!terminal && handlers.onCancelled) handlers.onCancelled({ aborted: true });
        return;
      }
      if (handlers.onError) handlers.onError(err);
      else throw err;
    }
  }

  async function cancelChat(request_id) {
    return request('/api/chat/cancel', { method: 'POST', body: JSON.stringify({ request_id }) });
  }

  // Proposed changes (review workflow)
  const getChange = (id, withContent = false) => request(`/api/changes/${id}${withContent ? '?content=1' : ''}`);
  const applyChange = (id) => request(`/api/changes/${id}/apply`, { method: 'POST' });
  const rejectChange = (id) => request(`/api/changes/${id}/reject`, { method: 'POST' });
  const revertChange = (id) => request(`/api/changes/${id}/revert`, { method: 'POST' });
  const listConversationChanges = (convId) => request(`/api/conversations/${convId}/changes`);

  // Commands (permission workflow)
  const approveCommand = (id) => request(`/api/commands/${id}/approve`, { method: 'POST' });
  const rejectCommand = (id) => request(`/api/commands/${id}/reject`, { method: 'POST' });

  // Metadata & System
  async function getModels() {
    return request('/api/models');
  }

  async function getUsage(period = 'today') {
    return request(`/api/usage?period=${encodeURIComponent(period)}`);
  }

  async function getHealth() {
    return request('/health');
  }

  return {
    getToken,
    setToken,
    clearToken,
    request,
    login,
    register,
    getMe,
    updateMe,
    listWorkspaces,
    createWorkspace,
    getWorkspace,
    deleteWorkspace,
    indexWorkspace,
    getIndexStatus,
    listFiles,
    searchWorkspace,
    getGitStatus,
    getGitDiff,
    getGitLog,
    createGitCheckpoint,
    restoreGitCheckpoint,
    listConversations,
    createConversation,
    getConversation,
    updateConversation,
    deleteConversation,
    streamChat,
    cancelChat,
    getChange,
    applyChange,
    rejectChange,
    revertChange,
    listConversationChanges,
    approveCommand,
    rejectCommand,
    listGitCheckpoints,
    getModels,
    getUsage,
    getHealth,
  };
})();

window.API = API;
