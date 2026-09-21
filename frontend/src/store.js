/**
 * CodeSage Central State Management
 */

const Store = (() => {
  const state = {
    user: null,
    workspaces: [],
    activeWorkspace: null,
    conversations: [],
    activeConversationId: null,
    messages: [],
    models: {},
    selectedProvider: 'openai',
    selectedModel: 'gpt-4o',
    mode: 'quick',
    useRepo: true,
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
      const availableProvider = Object.keys(models).find(k => models[k].available);
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

  function setModel(provider, model) {
    setState({
      selectedProvider: provider,
      selectedModel: model,
    });
  }

  function setMode(mode) {
    setState({ mode });
  }

  function setUseRepo(useRepo) {
    setState({ useRepo });
  }

  function setStreaming(isStreaming, streamingContent = '') {
    setState({ isStreaming, streamingContent });
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
  };
})();

window.Store = Store;
