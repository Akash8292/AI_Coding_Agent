/**
 * CommandPalette Component
 * Quick action launcher triggered by Ctrl+K / Cmd+K.
 */

const CommandPalette = (() => {
  let isOpen = false;
  let selectedIndex = 0;
  let currentFilteredCommands = [];

  const baseCommands = [
    { group: 'Navigation', text: 'New Chat', shortcut: 'Ctrl+N', icon: '+', action: () => window.newChat && window.newChat() },
    { group: 'Navigation', text: 'Toggle Sidebar', shortcut: 'Ctrl+B', icon: '☰', action: () => window.toggleSidebar && window.toggleSidebar() },
    { group: 'Navigation', text: 'Toggle Context Panel', shortcut: '', icon: '◨', action: () => window.toggleContextPanel && window.toggleContextPanel() },
    { group: 'Navigation', text: 'Settings', shortcut: 'Ctrl+,', icon: '⚙', action: () => window.showSettings && window.showSettings() },

    { group: 'Workspace', text: 'Add Workspace', shortcut: '', icon: '📁', action: () => window.showWorkspaceModal && window.showWorkspaceModal() },
    { group: 'Workspace', text: 'Re-index Workspace', shortcut: '', icon: '⚡', action: () => window.indexCurrentWorkspace && window.indexCurrentWorkspace() },
    { group: 'Workspace', text: 'Create Git Checkpoint', shortcut: '', icon: '💾', action: () => window.createCheckpoint && window.createCheckpoint() },
    { group: 'Workspace', text: 'Restore Git Checkpoint', shortcut: '', icon: '↩', action: () => window.restoreCheckpoint && window.restoreCheckpoint() },

    { group: 'AI & Mode', text: 'Switch to Quick Mode', shortcut: '', icon: '⚡', action: () => window.setMode && window.setMode('quick') },
    { group: 'AI & Mode', text: 'Switch to Deep Investigation Mode', shortcut: '', icon: '🔬', action: () => window.setMode && window.setMode('deep') },
    { group: 'AI & Mode', text: 'Toggle Repository Context', shortcut: '', icon: '📂', action: () => window.toggleRepoContext && window.toggleRepoContext() },
    { group: 'AI & Mode', text: 'Open Model Selector', shortcut: '', icon: '🤖', action: () => window.toggleModelSelector && window.toggleModelSelector() },
  ];

  function init() {
    document.addEventListener('keydown', (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        toggle();
      } else if (e.key === 'Escape' && isOpen) {
        close();
      }
    });
  }

  function toggle() {
    if (isOpen) close();
    else open();
  }

  function open() {
    const overlay = document.getElementById('command-palette-overlay');
    const input = document.getElementById('palette-input');
    if (!overlay || !input) return;

    isOpen = true;
    overlay.classList.remove('hidden');
    input.value = '';
    selectedIndex = 0;
    filter('');
    setTimeout(() => input.focus(), 50);
  }

  function close() {
    const overlay = document.getElementById('command-palette-overlay');
    if (!overlay) return;
    isOpen = false;
    overlay.classList.add('hidden');
  }

  function getCommands() {
    const commands = [...baseCommands];
    const state = window.Store.getState();

    // Add workspace switchers
    if (state.workspaces && state.workspaces.length) {
      for (const ws of state.workspaces) {
        commands.push({
          group: 'Workspaces',
          text: `Switch to: ${ws.name}`,
          shortcut: '',
          icon: '📂',
          action: () => window.selectWorkspace && window.selectWorkspace(ws.id),
        });
      }
    }

    // Add provider switchers
    if (state.models) {
      for (const [pKey, pInfo] of Object.entries(state.models)) {
        if (pInfo.available) {
          commands.push({
            group: 'Providers',
            text: `Use Provider: ${pInfo.name || pKey}`,
            shortcut: '',
            icon: '⚡',
            action: () => window.Store.setModel(pKey, pInfo.default_model),
          });
        }
      }
    }

    return commands;
  }

  function filter(query) {
    const q = (query || '').toLowerCase().trim();
    const all = getCommands();

    if (!q) {
      currentFilteredCommands = all;
    } else {
      currentFilteredCommands = all.filter(c =>
        c.text.toLowerCase().includes(q) || c.group.toLowerCase().includes(q)
      );
    }

    if (selectedIndex >= currentFilteredCommands.length) {
      selectedIndex = Math.max(0, currentFilteredCommands.length - 1);
    }

    renderResults();
  }

  function renderResults() {
    const container = document.getElementById('palette-results');
    if (!container) return;

    if (currentFilteredCommands.length === 0) {
      container.innerHTML = '<div style="padding: 16px; text-align: center; color: var(--text-2); font-size: 13px;">No matching commands</div>';
      return;
    }

    let html = '';
    let currentGroup = '';

    currentFilteredCommands.forEach((cmd, idx) => {
      if (cmd.group !== currentGroup) {
        currentGroup = cmd.group;
        html += `<div class="palette-group">${currentGroup.toUpperCase()}</div>`;
      }

      const isSelected = idx === selectedIndex;
      html += `
        <div class="palette-item ${isSelected ? 'selected' : ''}" 
             data-index="${idx}" 
             onclick="CommandPalette.execute(${idx})">
          <span class="palette-item-icon">${cmd.icon || '•'}</span>
          <span class="palette-item-text">${cmd.text}</span>
          ${cmd.shortcut ? `<span class="palette-item-shortcut">${cmd.shortcut}</span>` : ''}
        </div>
      `;
    });

    container.innerHTML = html;

    // Scroll selected item into view
    const selectedEl = container.querySelector('.palette-item.selected');
    if (selectedEl) {
      selectedEl.scrollIntoView({ block: 'nearest' });
    }
  }

  function handleKey(e) {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      if (currentFilteredCommands.length > 0) {
        selectedIndex = (selectedIndex + 1) % currentFilteredCommands.length;
        renderResults();
      }
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      if (currentFilteredCommands.length > 0) {
        selectedIndex = (selectedIndex - 1 + currentFilteredCommands.length) % currentFilteredCommands.length;
        renderResults();
      }
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (currentFilteredCommands[selectedIndex]) {
        execute(selectedIndex);
      }
    } else if (e.key === 'Escape') {
      e.preventDefault();
      close();
    }
  }

  function execute(idx) {
    const cmd = currentFilteredCommands[idx];
    close();
    if (cmd && typeof cmd.action === 'function') {
      setTimeout(() => cmd.action(), 50);
    }
  }

  return {
    init,
    toggle,
    open,
    close,
    filter,
    handleKey,
    execute,
  };
})();

window.CommandPalette = CommandPalette;
window.closeCommandPalette = CommandPalette.close;
window.filterPalette = CommandPalette.filter;
window.handlePaletteKey = CommandPalette.handleKey;
