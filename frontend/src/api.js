/**
 * CodeSage API Client
 * Handles authentication, REST endpoints, and SSE chat streaming.
 */

const API = (() => {
  const BASE_URL = window.location.origin.includes('5000') 
    ? '' 
    : (window.CODER_API_URL || 'http://localhost:5000');

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

  async function createWorkspace(name, repo_path) {
    return request('/api/workspaces', {
      method: 'POST',
      body: JSON.stringify({ name, repo_path }),
    });
  }

  async function getWorkspace(id) {
    return request(`/api/workspaces/${id}`);
  }

  async function deleteWorkspace(id) {
    return request(`/api/workspaces/${id}`, { method: 'DELETE' });
  }

  async function indexWorkspace(id) {
    return request(`/api/workspaces/${id}/index`, { method: 'POST' });
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

  async function restoreGitCheckpoint(id) {
    return request(`/api/workspaces/${id}/git/restore`, { method: 'POST' });
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
  async function streamChat({ conversation_id, message, provider, model, use_repo, workspace_id, mode }, { onActivity, onChunk, onDone, onError }) {
    const url = `${BASE_URL}/api/chat`;
    const token = getToken();
    const headers = {
      'Content-Type': 'application/json',
    };
    if (token) headers['Authorization'] = `Bearer ${token}`;

    const body = JSON.stringify({
      conversation_id,
      message,
      provider,
      model,
      use_repo,
      workspace_id,
      mode,
    });

    try {
      const response = await fetch(url, {
        method: 'POST',
        headers,
        body,
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        throw new Error(errorData.error || `Error ${response.status}: ${response.statusText}`);
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder('utf-8');
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop(); // keep partial line in buffer

        for (const line of lines) {
          const trimmed = line.trim();
          if (!trimmed || !trimmed.startsWith('data: ')) continue;
          const jsonStr = trimmed.substring(6);
          try {
            const event = JSON.parse(jsonStr);
            if (event.type === 'activity' && onActivity) {
              onActivity(event.items);
            } else if (event.type === 'chunk' && onChunk) {
              onChunk(event.content);
            } else if (event.type === 'done' && onDone) {
              onDone(event);
            } else if (event.type === 'error' && onError) {
              onError(new Error(event.message || 'Stream error'));
            }
          } catch (e) {
            console.warn('Failed to parse SSE line:', line, e);
          }
        }
      }
    } catch (err) {
      if (onError) onError(err);
      else throw err;
    }
  }

  // Code actions
  async function proposeEdit(workspace_id, path, instruction, provider, model) {
    return request('/api/propose_edit', {
      method: 'POST',
      body: JSON.stringify({ workspace_id, path, instruction, provider, model }),
    });
  }

  async function applyEdit(workspace_id, path, content) {
    return request('/api/apply_edit', {
      method: 'POST',
      body: JSON.stringify({ workspace_id, path, content }),
    });
  }

  async function diagnose(workspace_id, problem, provider, model) {
    return request('/api/diagnose', {
      method: 'POST',
      body: JSON.stringify({ workspace_id, problem, provider, model }),
    });
  }

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
    proposeEdit,
    applyEdit,
    diagnose,
    getModels,
    getUsage,
    getHealth,
  };
})();

window.API = API;
