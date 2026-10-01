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
      const info = (state.models || {})[state.selectedProvider];
      const modelErr = info && (info.model_errors || {})[state.selectedModel];
      labelEl.textContent = `${pName} • ${mName}` + ((info && info.status === 'error') || modelErr ? ' ⚠' : '');
      labelEl.title = modelErr || (info && info.error) || '';
    }

    if (!contentEl) return;

    const modelsData = state.models || {};
    const providerKeys = Object.keys(modelsData);

    if (providerKeys.length === 0) {
      contentEl.innerHTML = '<div style="padding: 12px; color: var(--text-2); font-size: 12px;">No providers configured</div>';
      return;
    }

    const esc = (v) => String(v ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;').replace(/'/g, '&#039;');
    const BADGE = {
      ready: ['available', 'Ready'],
      error: ['warning', 'Problem'],
      not_configured: ['unavailable', 'No API key'],
      disabled: ['unavailable', 'Optional · off'],
      unreachable: ['unavailable', 'Not running'],
    };
    let html = '';
    for (const pKey of providerKeys) {
      const pInfo = modelsData[pKey];
      const isAvailable = pInfo.available;
      const [cls, text] = BADGE[pInfo.status] || (isAvailable ? BADGE.ready : BADGE.not_configured);
      html += `<div class="model-provider-section">
        <div class="model-provider-header" title="${esc(pInfo.error || '')}">
          <span>${esc((pInfo.label || pKey).toUpperCase())}</span>
          <span class="provider-status ${cls}">${text}</span>
        </div>`;
      if (pInfo.error && (pInfo.status === 'error' || !isAvailable)) {
        html += `<div class="model-provider-error">${esc(pInfo.error)}</div>`;
      }
      if (isAvailable) {
        const modelsList = pInfo.models || [];
        if (!modelsList.length) {
          html += '<div class="model-option disabled"><span style="font-size: 11px;">No models found</span></div>';
        }
        for (const m of modelsList) {
          const isSelected = state.selectedProvider === pKey && state.selectedModel === m;
          html += `
            <div class="model-option ${isSelected ? 'selected' : ''}"
                 onclick="ModelSelector.selectModel('${esc(pKey)}', '${esc(m)}')">
              <span>${esc(m)}${m === pInfo.default_model ? ' <span class="model-meta">default</span>' : ''}${(pInfo.model_errors || {})[m] ? ` <span class="model-meta model-warn" title="${esc(pInfo.model_errors[m])}">⚠ recently failed</span>` : ''}</span>
              ${isSelected ? '<span style="font-size: 11px;">✓</span>' : ''}
            </div>`;
        }
      }
      html += '</div>';
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
