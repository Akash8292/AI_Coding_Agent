const chatEl = document.getElementById("chat");
const form = document.getElementById("chat-form");
const input = document.getElementById("input");
const sendBtn = document.getElementById("send-btn");
const modelSelect = document.getElementById("model-select");
const providerSelect = document.getElementById("provider-select");
const statusDot = document.getElementById("status-dot");
const repoPathInput = document.getElementById("repo-path");
const indexBtn = document.getElementById("index-btn");
const useRepoCheckbox = document.getElementById("use-repo");
const repoStatusEl = document.getElementById("repo-status");

let history = []; // [{role, content}]
let pollTimer = null;
let providersData = null; // { default_provider, ollama: {...}, kimi: {...} }

function renderMarkdown(text) {
  const html = marked.parse(text, { breaks: true });
  return html;
}

function addMessage(role, content) {
  const div = document.createElement("div");
  div.className = `msg ${role}`;
  div.innerHTML = renderMarkdown(content);
  chatEl.appendChild(div);
  chatEl.scrollTop = chatEl.scrollHeight;
  return div;
}

function highlightAll(container) {
  container.querySelectorAll("pre code").forEach((block) => {
    hljs.highlightElement(block);
  });
}

const PROVIDER_ORDER = ["ollama", "kimi", "openai", "claude", "gemini"];
const PROVIDER_LABELS = { ollama: "Ollama (local)" }; // others come from the backend

function populateProviders() {
  providerSelect.innerHTML = "";
  PROVIDER_ORDER.forEach((key) => {
    const info = providersData[key];
    if (!info) return; // provider not returned by backend for some reason
    const opt = document.createElement("option");
    opt.value = key;
    opt.textContent = PROVIDER_LABELS[key] || info.label || key;
    providerSelect.appendChild(opt);
  });
}

function populateModelsForProvider(providerKey) {
  const info = providersData && providersData[providerKey];
  modelSelect.innerHTML = "";

  if (!info) {
    statusDot.className = "status-dot bad";
    statusDot.title = "Could not reach backend";
    return;
  }

  if (info.available && info.models && info.models.length) {
    info.models.forEach((m) => {
      const opt = document.createElement("option");
      opt.value = m;
      opt.textContent = m;
      if (m === info.current) opt.selected = true;
      modelSelect.appendChild(opt);
    });
    statusDot.className = "status-dot ok";
    statusDot.title = providerKey === "ollama" ? "Ollama connected" : `${info.label || providerKey} API key configured`;
  } else {
    const opt = document.createElement("option");
    opt.textContent = (info.current || "unavailable") + " (unavailable)";
    modelSelect.appendChild(opt);
    statusDot.className = "status-dot bad";
    statusDot.title = info.error || "Not available";
  }
}

async function loadModels() {
  try {
    const res = await fetch("/api/models");
    providersData = await res.json();
    populateProviders();
    providerSelect.value = providersData.default_provider || "ollama";
    populateModelsForProvider(providerSelect.value);
  } catch (e) {
    statusDot.className = "status-dot bad";
    statusDot.title = "Could not reach backend";
  }
}

providerSelect.addEventListener("change", () => {
  populateModelsForProvider(providerSelect.value);
});

function renderRepoStatus(s) {
  if (s.status === "idle") {
    repoStatusEl.textContent = "No repo indexed yet.";
  } else if (s.status === "indexing") {
    repoStatusEl.textContent =
      `Indexing ${s.path} — files ${s.indexed_files}/${s.total_files}, ` +
      `chunks embedded: ${s.total_chunks}...`;
  } else if (s.status === "done") {
    repoStatusEl.textContent =
      `Indexed: ${s.path} (${s.total_chunks} chunks from ${s.total_files} files). ` +
      `Repo context is ready to use.`;
    useRepoCheckbox.disabled = false;
    useRepoCheckbox.checked = true;
  } else if (s.status === "error") {
    repoStatusEl.textContent = `Indexing failed: ${s.error}`;
  }
}

async function pollStatus() {
  try {
    const res = await fetch("/api/index_status");
    const s = await res.json();
    renderRepoStatus(s);
    if (s.status === "indexing") {
      pollTimer = setTimeout(pollStatus, 1000);
    } else {
      indexBtn.disabled = false;
    }
  } catch (e) {
    repoStatusEl.textContent = "Could not reach backend for index status.";
    indexBtn.disabled = false;
  }
}

indexBtn.addEventListener("click", async () => {
  const path = repoPathInput.value.trim();
  if (!path) return;
  indexBtn.disabled = true;
  repoStatusEl.textContent = "Starting index...";
  try {
    const res = await fetch("/api/index_repo", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
    const data = await res.json();
    if (data.error) {
      repoStatusEl.textContent = `Error: ${data.error}`;
      indexBtn.disabled = false;
      return;
    }
    clearTimeout(pollTimer);
    pollStatus();
  } catch (e) {
    repoStatusEl.textContent = `Error: ${e.message}`;
    indexBtn.disabled = false;
  }
});

// pick up an already-indexed repo on page load (e.g. after refresh)
pollStatus();

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const message = input.value.trim();
  if (!message) return;

  addMessage("user", message);
  history.push({ role: "user", content: message });
  input.value = "";
  sendBtn.disabled = true;

  const assistantDiv = addMessage("assistant", "");
  let assistantText = "";

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message,
        history: history.slice(0, -1),
        provider: providerSelect.value,
        model: modelSelect.value,
        use_repo: useRepoCheckbox.checked,
      }),
    });

    const reader = res.body.getReader();
    const decoder = new TextDecoder();

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      assistantText += decoder.decode(value, { stream: true });
      assistantDiv.innerHTML = renderMarkdown(assistantText);
      highlightAll(assistantDiv);
      chatEl.scrollTop = chatEl.scrollHeight;
    }

    history.push({ role: "assistant", content: assistantText });
  } catch (err) {
    assistantDiv.innerHTML = renderMarkdown(
      `**Error:** could not reach backend (${err.message})`
    );
  } finally {
    sendBtn.disabled = false;
    input.focus();
  }
});

input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    form.requestSubmit();
  }
});

loadModels();