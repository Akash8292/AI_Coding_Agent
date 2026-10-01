/**
 * CodeSage Central State Management
 */

const Store = (() => {
  const saved = (() => {
    try { return JSON.parse(localStorage.getItem('codesage_prefs') || '{}'); } catch (e) { return {}; }
  })();

  const state = {
    user: null,
    workspaces: [],
    activeWorkspace: null,
    conversations: [],
    activeConversationId: null,
    messages: [],
    models: {},
    selectedProvider: saved.provider || 'gemini',
    selectedModel: saved.model || '',
    mode: saved.mode || 'quick',
    useRepo: saved.useRepo !== undefined ? saved.useRepo : true,
    isStreaming: false,
    streamingContent: '',
    activityLog: [],
    files: [],
    gitStatus: null,
    pendingDiff: null,
    pendingPermission: null,
    sessionPermissions: new Set(),
    usage: null,
  };

  const listeners = new Set();

  function getState() {
    return state;
  }

  function subscribe(listener) {
    listeners.add(listener);
    return () => listeners.delete(listener);
  }

  function emit(changedKeys = []) {
    for (const listener of listeners) {
      try {
        listener(state, changedKeys);
      } catch (err) {
        console.error('Store listener error:', err);
      }
    }
  }

  function setState(updates) {
    const changedKeys = Object.keys(updates);
    Object.assign(state, updates);
    emit(changedKeys);
  }

  function setUser(user) {
    setState({ user });
  }

  function setWorkspaces(workspaces) {
    setState({ workspaces });
  }

  function setActiveWorkspace(workspace) {
    setState({ activeWorkspace: workspace, files: [], gitStatus: null });
  }

  function setConversations(conversations) {
    setState({ conversations });
  }

  function setActiveConversation(convId, messages = []) {
    setState({
      activeConversationId: convId,
      messages: messages,
      isStreaming: false,
      streamingContent: '',
      activityLog: [],
    });
  }

  function addMessage(msg) {
    const messages = [...state.messages, msg];
    setState({ messages });
  }

  function updateLastMessage(content, activityLog = null) {
    if (!state.messages.length) return;
    const messages = [...state.messages];
    const last = { ...messages[messages.length - 1], content };
    if (activityLog) last.activity_log = activityLog;
    messages[messages.length - 1] = last;
    setState({ messages });
  }

  function setModels(models) {
    let provider = state.selectedProvider;
    let model = state.selectedModel;

    // Pick first available provider if current isn't valid or available
    if (models && (!models[provider] || !models[provider].available)) {
      const keys = Object.keys(models);
      const availableProvider = keys.find(k => models[k].available && models[k].status === 'ready')
        || keys.find(k => models[k].available);
      if (availableProvider) {
        provider = availableProvider;
        model = models[provider].default_model || (models[provider].models && models[provider].models[0]);
      }
    } else if (models && models[provider] && (!model || !(models[provider].models || []).includes(model))) {
      model = models[provider].default_model || (models[provider].models && models[provider].models[0]);
    }

    setState({
      models,
      selectedProvider: provider,
      selectedModel: model,
    });
  }

  function savePrefs() {
    try {
      localStorage.setItem('codesage_prefs', JSON.stringify({
        provider: state.selectedProvider, model: state.selectedModel, mode: state.mode, useRepo: state.useRepo,
      }));
    } catch (e) { /* storage unavailable — preferences just won't persist */ }
  }

  function setModel(provider, model) {
    setState({
      selectedProvider: provider,
      selectedModel: model,
    });
    savePrefs();
  }

  /** Replace a message's attached change/command record (after apply/reject/etc). */
  function updateAttached(kind, record) {
    for (const m of state.messages) {
      if (m[kind] && m[kind].id === record.id) m[kind] = record;
    }
  }

  function setMode(mode) {
    setState({ mode });
    savePrefs();
  }

  function setUseRepo(useRepo) {
    setState({ useRepo });
    savePrefs();
  }

  function setStreaming(isStreaming, streamingContent = undefined) {
    if (streamingContent !== undefined) {
      setState({ isStreaming, streamingContent });
    } else {
      setState({ isStreaming });
    }
  }

  function appendStreamingChunk(chunk) {
    const streamingContent = state.streamingContent + chunk;
    setState({ streamingContent });
  }

  function setActivityLog(activityLog) {
    setState({ activityLog });
  }

  function addActivity(item) {
    const activityLog = [...state.activityLog, item];
    setState({ activityLog });
  }

  function setFiles(files) {
    setState({ files });
  }

  function setGitStatus(gitStatus) {
    setState({ gitStatus });
  }

  function setPendingDiff(diffData) {
    setState({ pendingDiff: diffData });
  }

  function setPendingPermission(permissionData) {
    setState({ pendingPermission: permissionData });
  }

  function allowSessionPermission(command) {
    state.sessionPermissions.add(command);
    emit(['sessionPermissions']);
  }

  function hasSessionPermission(command) {
    return state.sessionPermissions.has(command);
  }

  return {
    getState,
    subscribe,
    setState,
    setUser,
    setWorkspaces,
    setActiveWorkspace,
    setConversations,
    setActiveConversation,
    addMessage,
    updateLastMessage,
    setModels,
    setModel,
    setMode,
    setUseRepo,
    setStreaming,
    appendStreamingChunk,
    setActivityLog,
    addActivity,
    setFiles,
    setGitStatus,
    setPendingDiff,
    setPendingPermission,
    allowSessionPermission,
    hasSessionPermission,
    updateAttached,
  };
})();

window.Store = Store;
