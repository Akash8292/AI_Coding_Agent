/**
 * ModelSelector Component
 * Manages provider and model picking dropdown.
 */

const ModelSelector = (() => {
  let isOpen = false;

  function init() {
    window.Store.subscribe((state, changedKeys) => {
      if (changedKeys.includes('models') || changedKeys.includes('selectedProvider') || changedKeys.includes('selectedModel')) {
        render();
      }
    });

    document.addEventListener('click', (e) => {
      const container = document.getElementById('model-selector-container');
      if (container && !container.contains(e.target) && isOpen) {
        closeDropdown();
      }
    });
  }

  function toggleDropdown() {
    if (isOpen) {
      closeDropdown();
    } else {
      openDropdown();
    }
  }

  function openDropdown() {
    const dropdown = document.getElementById('model-dropdown');
    if (dropdown) {
      dropdown.classList.remove('hidden');
      isOpen = true;
    }
  }

  function closeDropdown() {
    const dropdown = document.getElementById('model-dropdown');
    if (dropdown) {
      dropdown.classList.add('hidden');
      isOpen = false;
    }
  }

  function selectModel(provider, model) {
    window.Store.setModel(provider, model);
    closeDropdown();
  }

  function render() {
    const state = window.Store.getState();
    const labelEl = document.getElementById('current-provider-label');
    const contentEl = document.getElementById('model-dropdown-content');

    if (labelEl) {
      const pName = state.selectedProvider ? state.selectedProvider.toUpperCase() : 'SELECT';
      const mName = state.selectedModel || 'Model';
      labelEl.textContent = `${pName} • ${mName}`;
    }

    if (!contentEl) return;

    const modelsData = state.models || {};
    const providerKeys = Object.keys(modelsData);

    if (providerKeys.length === 0) {
      contentEl.innerHTML = '<div style="padding: 12px; color: var(--text-2); font-size: 12px;">No providers configured</div>';
      return;
    }

    let html = '';

    for (const pKey of providerKeys) {
      const pInfo = modelsData[pKey];
      const isAvailable = pInfo.available;
      const statusBadge = isAvailable
        ? '<span class="provider-status available">Ready</span>'
        : '<span class="provider-status unavailable">No API Key</span>';

      html += `<div class="model-provider-section">
        <div class="model-provider-header">
          <span>${(pInfo.name || pKey).toUpperCase()}</span>
          ${statusBadge}
        </div>`;

      const modelsList = pInfo.models || [];
      if (modelsList.length === 0) {
        html += `<div class="model-option disabled"><span style="font-size: 11px;">No models listed</span></div>`;
      } else {
        for (const m of modelsList) {
          const isSelected = state.selectedProvider === pKey && state.selectedModel === m;
          const selectedClass = isSelected ? 'selected' : '';
          const disabledClass = !isAvailable ? 'disabled' : '';

          html += `
            <div class="model-option ${selectedClass} ${disabledClass}" 
                 onclick="${isAvailable ? `ModelSelector.selectModel('${pKey}', '${m}')` : ''}">
              <span>${m}</span>
              ${isSelected ? '<span style="font-size: 11px;">✓</span>' : ''}
            </div>
          `;
        }
      }

      html += `</div>`;
    }

    contentEl.innerHTML = html;
  }

  return {
    init,
    toggleDropdown,
    openDropdown,
    closeDropdown,
    selectModel,
    render,
  };
})();

window.ModelSelector = ModelSelector;
window.toggleModelSelector = ModelSelector.toggleDropdown;
