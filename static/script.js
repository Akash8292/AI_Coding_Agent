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
const runTestsBtn = document.getElementById("run-tests-btn");
const testOutputEl = document.getElementById("test-output");

const editToggle = document.getElementById("edit-toggle");
const editBody = document.getElementById("edit-body");
const editPathInput = document.getElementById("edit-path");
const editInstructionInput = document.getElementById("edit-instruction");
const proposeBtn = document.getElementById("propose-btn");

const diagnoseToggle = document.getElementById("diagnose-toggle");
const diagnoseBody = document.getElementById("diagnose-body");
const diagnoseProblemInput = document.getElementById("diagnose-problem");
const diagnoseBtn = document.getElementById("diagnose-btn");

const reviewPanel = document.getElementById("review-panel");
const editStatusEl = document.getElementById("edit-status");
const editExplanationEl = document.getElementById("edit-explanation");
const diffView = document.getElementById("diff-view");
const editActions = document.getElementById("edit-actions");
const applyBtn = document.getElementById("apply-btn");
const discardBtn = document.getElementById("discard-btn");

let pendingEdit = null; // { path, content }

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
    runTestsBtn.disabled = false;
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

// ---- Propose/apply file edits, diagnose-and-fix, run tests --------------

editToggle.addEventListener("click", () => {
  const showing = editBody.style.display !== "none";
  editBody.style.display = showing ? "none" : "flex";
  editToggle.textContent = (showing ? "▸" : "▾") + " Propose a code edit";
});

diagnoseToggle.addEventListener("click", () => {
  const showing = diagnoseBody.style.display !== "none";
  diagnoseBody.style.display = showing ? "none" : "flex";
  diagnoseToggle.textContent = (showing ? "▸" : "▾") + " Diagnose & fix a bug";
});

function renderDiff(diffLines) {
  const classMap = {
    add: "diff-add",
    remove: "diff-remove",
    hunk: "diff-hunk",
    header: "diff-header",
    context: "diff-context",
  };
  return diffLines
    .map((l) => {
      const escaped = l.text
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");
      return `<span class="${classMap[l.type] || "diff-context"}">${escaped || " "}</span>`;
    })
    .join("\n");
}

function resetReviewUI() {
  editExplanationEl.style.display = "none";
  editExplanationEl.textContent = "";
  diffView.style.display = "none";
  diffView.innerHTML = "";
  editActions.style.display = "none";
  pendingEdit = null;
}

function showReviewResult(data, { notFoundMsg } = {}) {
  reviewPanel.style.display = "flex";
  resetReviewUI();

  if (data.error) {
    editStatusEl.textContent = `Error: ${data.error}`;
    if (data.raw_response) {
      diffView.style.display = "block";
      diffView.textContent = data.raw_response;
    }
    return;
  }

  if (data.explanation) {
    editExplanationEl.textContent = data.explanation;
    editExplanationEl.style.display = "block";
  }

  if (data.unchanged) {
    editStatusEl.textContent = `Model proposed no changes to ${data.path}.` +
      (notFoundMsg || "");
    return;
  }

  editStatusEl.textContent = `Review the proposed change to ${data.path}:`;
  diffView.innerHTML = renderDiff(data.diff);
  diffView.style.display = "block";
  editActions.style.display = "flex";
  pendingEdit = { path: data.path, content: data.proposed_content };
}

proposeBtn.addEventListener("click", async () => {
  const path = editPathInput.value.trim();
  const instruction = editInstructionInput.value.trim();
  if (!path || !instruction) return;

  proposeBtn.disabled = true;
  reviewPanel.style.display = "flex";
  resetReviewUI();
  editStatusEl.textContent = "Asking the model to propose a change...";

  try {
    const res = await fetch("/api/propose_edit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        path,
        instruction,
        provider: providerSelect.value,
        model: modelSelect.value,
      }),
    });
    showReviewResult(await res.json());
  } catch (e) {
    editStatusEl.textContent = `Error: ${e.message}`;
  } finally {
    proposeBtn.disabled = false;
  }
});

diagnoseBtn.addEventListener("click", async () => {
  const problem = diagnoseProblemInput.value.trim();
  if (!problem) return;

  diagnoseBtn.disabled = true;
  reviewPanel.style.display = "flex";
  resetReviewUI();
  editStatusEl.textContent = "Searching the repo and diagnosing the problem...";

  try {
    const res = await fetch("/api/diagnose", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        problem,
        provider: providerSelect.value,
        model: modelSelect.value,
      }),
    });
    const data = await res.json();
    let note = "";
    if (data.other_candidates && data.other_candidates.length) {
      note = ` (other files that looked related: ${data.other_candidates.join(", ")})`;
    }
    showReviewResult(data, { notFoundMsg: note });
  } catch (e) {
    editStatusEl.textContent = `Error: ${e.message}`;
  } finally {
    diagnoseBtn.disabled = false;
  }
});

applyBtn.addEventListener("click", async () => {
  if (!pendingEdit) return;
  applyBtn.disabled = true;
  try {
    const res = await fetch("/api/apply_edit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(pendingEdit),
    });
    const data = await res.json();
    if (data.error) {
      editStatusEl.textContent = `Error applying edit: ${data.error}`;
    } else {
      editStatusEl.textContent = `Applied to ${data.path}. Original backed up to ${data.backup}.`;
      diffView.style.display = "none";
      diffView.innerHTML = "";
      editActions.style.display = "none";
      pendingEdit = null;
    }
  } catch (e) {
    editStatusEl.textContent = `Error: ${e.message}`;
  } finally {
    applyBtn.disabled = false;
  }
});

discardBtn.addEventListener("click", () => {
  editStatusEl.textContent = "Discarded — nothing was written to disk.";
  resetReviewUI();
});

runTestsBtn.addEventListener("click", async () => {
  runTestsBtn.disabled = true;
  testOutputEl.style.display = "block";
  testOutputEl.textContent = "Running tests...";
  try {
    const res = await fetch("/api/run_tests", { method: "POST" });
    const data = await res.json();
    if (data.error) {
      testOutputEl.textContent = `Error: ${data.error}`;
    } else {
      const verdict = data.passed ? "✅ PASSED" : "❌ FAILED";
      testOutputEl.textContent =
        `${verdict}  (command: ${data.command}, exit code ${data.exit_code})\n\n` +
        (data.stdout || "") +
        (data.stderr ? `\n--- stderr ---\n${data.stderr}` : "");
    }
  } catch (e) {
    testOutputEl.textContent = `Error: ${e.message}`;
  } finally {
    runTestsBtn.disabled = false;
  }
});

loadModels();