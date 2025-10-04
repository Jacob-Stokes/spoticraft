const supervisorStatusEl = document.getElementById("supervisor-status");
const syncListEl = document.getElementById("sync-list");
const lastUpdatedEl = document.getElementById("last-updated");
const notificationsEl = document.getElementById("notifications");
const refreshButton = document.getElementById("refresh-button");
const historyTailInput = document.getElementById("history-tail");
const syncTemplate = document.getElementById("sync-card-template");

const themeSelect = document.getElementById("theme-select");
const prefersDark = window.matchMedia("(prefers-color-scheme: dark)");

const configTable = document.getElementById("config-table");
const configTableBody = configTable ? configTable.querySelector("tbody") : null;
const configEmptyState = document.getElementById("config-empty");
const configSearchInput = document.getElementById("config-search");
const newSyncButton = document.getElementById("new-sync-button");

const assetsTable = document.getElementById("assets-table");
const assetsTableBody = assetsTable ? assetsTable.querySelector("tbody") : null;
const assetsEmptyState = document.getElementById("assets-empty");
const uploadAssetButton = document.getElementById("upload-asset-button");
const assetFileInput = document.getElementById("asset-file-input");

const editorModal = document.getElementById("editor-modal");
const editorTitle = document.getElementById("editor-title");
const editorSubtitle = document.getElementById("editor-subtitle");
const editorCloseButton = document.getElementById("editor-close");
const templateSelect = document.getElementById("template-select");
const applyTemplateButton = document.getElementById("apply-template");
const duplicateButton = document.getElementById("duplicate-sync");
const deleteButton = document.getElementById("delete-sync");
const saveButton = document.getElementById("save-sync");
const editorSummary = document.getElementById("editor-summary");
const editorTextarea = document.getElementById("config-editor");

const CONFIG_UI_AVAILABLE = Boolean(
  configTable &&
  configTableBody &&
  configEmptyState &&
  configSearchInput &&
  newSyncButton &&
  editorModal &&
  editorTitle &&
  editorSubtitle &&
  editorCloseButton &&
  templateSelect &&
  applyTemplateButton &&
  duplicateButton &&
  deleteButton &&
  saveButton &&
  editorSummary &&
  editorTextarea,
);

const ASSETS_UI_AVAILABLE = Boolean(
  assetsTable &&
  assetsTableBody &&
  assetsEmptyState &&
  uploadAssetButton &&
  assetFileInput,
);

const THEME_STORAGE_KEY = "spotifreak-theme";
const THEME_OPTIONS = new Set(["light", "dark", "system"]);

const state = {
  refreshTimer: null,
  configs: [],
  filteredConfigs: [],
  templates: [],
  assets: [],
  theme: {
    mode: "system",
  },
  editor: {
    open: false,
    mode: "edit",
    syncId: null,
    content: "",
    originalContent: "",
    parsed: null,
    dirty: false,
    validationStatus: null,
    validationMessage: null,
    validationController: null,
    validationTimer: null,
  },
};

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const data = await response.json();
      if (data && typeof data.detail === "string") {
        message = data.detail;
      }
    } catch (error) {
      // ignore parse errors
    }
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  if (response.status === 204) {
    return null;
  }
  return response.json();
}

function loadThemePreference() {
  const stored = localStorage.getItem(THEME_STORAGE_KEY);
  if (stored && THEME_OPTIONS.has(stored)) {
    return stored;
  }
  return "system";
}

function resolveTheme(mode) {
  return mode === "system" ? (prefersDark.matches ? "dark" : "light") : mode;
}

function applyTheme(mode) {
  const resolved = resolveTheme(mode);
  document.documentElement.dataset.theme = resolved;
  if (themeSelect) {
    themeSelect.value = mode;
  }
}

function setTheme(mode) {
  const normalised = THEME_OPTIONS.has(mode) ? mode : "system";
  state.theme.mode = normalised;
  try {
    localStorage.setItem(THEME_STORAGE_KEY, normalised);
  } catch (error) {
    // ignore storage errors (private mode etc.)
  }
  applyTheme(normalised);
}

prefersDark.addEventListener("change", () => {
  if (state.theme.mode === "system") {
    applyTheme("system");
  }
});

function formatSchedule(schedule) {
  if (!schedule) {
    return "—";
  }
  if (schedule.interval) {
    return `Interval: ${schedule.interval}`;
  }
  if (schedule.cron) {
    return `Cron: ${schedule.cron}`;
  }
  return "Unknown";
}

function formatTimestamp(value) {
  if (!value) {
    return "—";
  }
  try {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) {
      return value;
    }
    return date.toLocaleString();
  } catch (error) {
    return value;
  }
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes)) {
    return "—";
  }
  if (bytes === 0) {
    return "0 B";
  }
  const units = ["B", "KB", "MB", "GB"];
  const exponent = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  const value = bytes / 1024 ** exponent;
  return `${value.toFixed(value >= 10 || exponent === 0 ? 0 : 1)} ${units[exponent]}`;
}

function formatDuration(startedAt, completedAt) {
  if (!startedAt || !completedAt) {
    return null;
  }
  const start = new Date(startedAt);
  const end = new Date(completedAt);
  const diffMs = end - start;
  if (Number.isNaN(diffMs) || diffMs < 0) {
    return null;
  }
  if (diffMs < 1000) {
    return `${diffMs} ms`;
  }
  if (diffMs < 60_000) {
    const seconds = (diffMs / 1000).toFixed(1);
    return `${seconds} s`;
  }
  const minutes = Math.floor(diffMs / 60_000);
  const seconds = Math.round((diffMs % 60_000) / 1000);
  return seconds ? `${minutes}m ${seconds}s` : `${minutes}m`;
}

function jobStatusDescriptor(job) {
  if (!job) {
    return { label: "Not scheduled", className: "error" };
  }
  if (job.paused) {
    return { label: "Paused", className: "paused" };
  }
  if (job.missed) {
    return { label: "Overdue", className: "error" };
  }
  if (job.next_run) {
    return { label: "Scheduled", className: "running" };
  }
  return { label: "Waiting", className: "waiting" };
}

function formatOptionValue(value) {
  if (value == null) {
    return "null";
  }
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  try {
    return JSON.stringify(value);
  } catch (error) {
    return String(value);
  }
}

function renderSupervisor(statusPayload) {
  if (!statusPayload || statusPayload.status !== "ok") {
    supervisorStatusEl.innerHTML = "<p class=\"error-text\">Supervisor unavailable.</p>";
    return;
  }

  const jobs = statusPayload.jobs || [];
  if (!jobs.length) {
    supervisorStatusEl.innerHTML = "<p class=\"muted\">No jobs scheduled.</p>";
    return;
  }

  const list = document.createElement("ul");
  list.className = "job-list";
  jobs.forEach((job) => {
    const li = document.createElement("li");
    const descriptor = jobStatusDescriptor(job);
    const label = document.createElement("span");
    label.className = `badge ${descriptor.className}`;
    label.textContent = descriptor.label;

    const meta = document.createElement("div");
    meta.className = "job-meta";
    const nextRun = job.next_run ? formatTimestamp(job.next_run) : "—";
    meta.textContent = `${job.id} • next: ${nextRun}`;

    li.appendChild(label);
    li.appendChild(meta);
    list.appendChild(li);
  });

  supervisorStatusEl.innerHTML = "";
  supervisorStatusEl.appendChild(list);
}

function createHistoryLoader(detailsEl, syncId) {
  detailsEl.addEventListener("toggle", async () => {
    if (!detailsEl.open) {
      return;
    }
    const listEl = detailsEl.querySelector("ol");
    listEl.innerHTML = "<li class=\"muted\">Loading…</li>";
    const tail = Number.parseInt(historyTailInput.value, 10) || 5;
    try {
      const data = await fetchJson(`/syncs/${encodeURIComponent(syncId)}/history?tail=${tail}`);
      const history = (data && data.history) || [];
      listEl.innerHTML = "";
      if (!history.length) {
        listEl.innerHTML = "<li class=\"muted\">No runs recorded yet.</li>";
        return;
      }
      history
        .slice()
        .reverse()
        .forEach((entry) => {
          const item = document.createElement("li");
          const status = entry.status ? entry.status.toUpperCase() : "UNKNOWN";
          const completed = formatTimestamp(entry.completed_at || entry.started_at);
          const duration = formatDuration(entry.started_at, entry.completed_at);

          const line = [status, completed].filter(Boolean).join(" • ");
          item.textContent = duration ? `${line} • ${duration}` : line;

          if (entry.error) {
            const errorEl = document.createElement("div");
            errorEl.className = "error-text";
            errorEl.textContent = entry.error;
            item.appendChild(errorEl);
          }

          if (entry.details) {
            const detailsEl = document.createElement("div");
            detailsEl.className = "muted";
            detailsEl.textContent = formatOptionValue(entry.details);
            item.appendChild(detailsEl);
          }

          listEl.appendChild(item);
        });
    } catch (error) {
      listEl.innerHTML = `<li class=\"error-text\">${error.message}</li>`;
    }
  });
}

function renderSyncs(syncs, jobs) {
  const jobMap = new Map((jobs || []).map((job) => [job.id, job]));
  syncListEl.innerHTML = "";

  if (!syncs || !syncs.length) {
    syncListEl.innerHTML = "<p class=\"muted\">No syncs configured.</p>";
    return;
  }

  syncs.forEach((sync) => {
    const fragment = document.importNode(syncTemplate.content, true);
    const card = fragment.querySelector(".sync-card");
    const job = jobMap.get(sync.id);

    card.querySelector(".sync-name").textContent = sync.id;
    card.querySelector(".sync-type").textContent = sync.type;
    card.querySelector(".sync-schedule").textContent = formatSchedule(sync.schedule);
    const descriptionEl = card.querySelector(".sync-description");
    if (descriptionEl) {
      descriptionEl.textContent = sync.description || "No description provided.";
    }

    const descriptor = jobStatusDescriptor(job);
    const badge = card.querySelector(".badge");
    badge.textContent = descriptor.label;
    badge.classList.add(descriptor.className);

    const nextRunEl = card.querySelector(".sync-next-run");
    if (job && job.next_run) {
      nextRunEl.textContent = formatTimestamp(job.next_run);
    } else if (descriptor.label === "Paused") {
      nextRunEl.textContent = "Paused";
    } else {
      nextRunEl.textContent = "—";
    }

    card.querySelectorAll("[data-command]").forEach((button) => {
      const command = button.dataset.command;
      button.addEventListener("click", async () => {
        if (command === "delete") {
          const confirmed = window.confirm(`Remove sync "${sync.id}" from the scheduler?`);
          if (!confirmed) {
            return;
          }
        }
        button.disabled = true;
        try {
          const payload = { command };
          const response = await fetchJson(`/syncs/${encodeURIComponent(sync.id)}/command`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
          });
          showToast(response?.message || `Command ${command} submitted`, "success");
          await refreshData(false);
        } catch (error) {
          showToast(error.message, "error");
        } finally {
          button.disabled = false;
        }
      });
    });

    const historyDetails = card.querySelector(".sync-history");
    createHistoryLoader(historyDetails, sync.id);

    syncListEl.appendChild(fragment);
  });
}

function showToast(message, kind = "success", timeout = 4000) {
  const toast = document.createElement("div");
  toast.className = `toast ${kind}`;
  const text = document.createElement("p");
  text.textContent = message;
  toast.appendChild(text);
  notificationsEl.appendChild(toast);
  setTimeout(() => toast.remove(), timeout);
}

async function refreshData(manual = false) {
  try {
    const [syncPayload, statusPayload] = await Promise.all([
      fetchJson("/syncs"),
      fetchJson("/status"),
    ]);
    renderSupervisor(statusPayload);
    renderSyncs(syncPayload?.syncs || [], statusPayload?.jobs || []);
    const now = new Date();
    lastUpdatedEl.textContent = `Updated ${now.toLocaleTimeString()}`;
    if (manual) {
      showToast("Dashboard updated", "success", 2500);
    }
  } catch (error) {
    renderSupervisor(null);
    syncListEl.innerHTML = `<p class=\"error-text\">${error.message}</p>`;
    showToast(error.message, "error", 5000);
  }
}

function startAutoRefresh() {
  if (state.refreshTimer) {
    clearInterval(state.refreshTimer);
  }
  state.refreshTimer = setInterval(() => refreshData(false), 5000);
}

async function loadTemplates() {
  try {
    const data = await fetchJson("/config/templates");
    state.templates = data?.templates || [];
    if (CONFIG_UI_AVAILABLE) {
      populateTemplateSelect("__current__");
    }
  } catch (error) {
    state.templates = [];
    if (CONFIG_UI_AVAILABLE) {
      populateTemplateSelect("__current__");
    }
    showToast(`Failed to load templates: ${error.message}`, "error", 6000);
  }
}

async function loadAssets() {
  if (!ASSETS_UI_AVAILABLE) {
    return;
  }
  try {
    const data = await fetchJson("/config/assets");
    state.assets = data?.assets || [];
    renderAssetTable();
  } catch (error) {
    state.assets = [];
    renderAssetTable();
    showToast(`Failed to load assets: ${error.message}`, "error", 6000);
  }
}

async function uploadAsset(file) {
  const formData = new FormData();
  formData.append("file", file, file.name);
  try {
    await fetchJson("/config/assets", {
      method: "POST",
      body: formData,
    });
    showToast(`Uploaded '${file.name}'`, "success", 3000);
    await loadAssets();
  } catch (error) {
    showToast(`Upload failed: ${error.message}`, "error", 6000);
  }
}

async function deleteAsset(name) {
  const confirmed = window.confirm(`Delete asset '${name}'? This cannot be undone.`);
  if (!confirmed) {
    return;
  }
  try {
    await fetchJson(`/config/assets/${encodeURIComponent(name)}`, { method: "DELETE" });
    showToast(`Deleted '${name}'`, "success", 3000);
    await loadAssets();
  } catch (error) {
    showToast(`Delete failed: ${error.message}`, "error", 6000);
  }
}

function formatScheduleSummary(schedule) {
  if (!schedule) return "—";
  if (schedule.interval) return schedule.interval;
  if (schedule.cron) return schedule.cron;
  return "—";
}

function applyConfigFilter() {
  if (!CONFIG_UI_AVAILABLE) {
    state.filteredConfigs = [];
    return;
  }
  const query = (configSearchInput.value || "").trim().toLowerCase();
  if (!query) {
    state.filteredConfigs = [...state.configs];
    return;
  }
  state.filteredConfigs = state.configs.filter((item) => {
    const haystack = `${item.id} ${item.type || ""}`.toLowerCase();
    return haystack.includes(query);
  });
}

function renderConfigTable() {
  if (!CONFIG_UI_AVAILABLE) {
    return;
  }
  applyConfigFilter();
  if (!state.filteredConfigs.length) {
    configTable.classList.add("hidden");
    configEmptyState.classList.remove("hidden");
    configEmptyState.textContent = state.configs.length
      ? "No configs match your filter."
      : "No sync configs found.";
    return;
  }

  configTable.classList.remove("hidden");
  configEmptyState.classList.add("hidden");
  configTableBody.innerHTML = "";

  state.filteredConfigs.forEach((cfg) => {
    const row = document.createElement("tr");

    const idCell = document.createElement("td");
    idCell.textContent = cfg.id;
    row.appendChild(idCell);

    const typeCell = document.createElement("td");
    typeCell.textContent = cfg.type || "—";
    row.appendChild(typeCell);

    const scheduleCell = document.createElement("td");
    scheduleCell.textContent = formatScheduleSummary(cfg.schedule);
    row.appendChild(scheduleCell);

    const statusCell = document.createElement("td");
    const badge = document.createElement("span");
    badge.classList.add("status-badge");
    if (cfg.valid) {
      badge.classList.add("status-ok");
      badge.textContent = "Valid";
    } else {
      badge.classList.add("status-error");
      badge.textContent = cfg.error ? "Error" : "Invalid";
      badge.title = cfg.error || "Invalid configuration";
    }
    statusCell.appendChild(badge);
    row.appendChild(statusCell);

    const actionCell = document.createElement("td");
    actionCell.className = "actions-col";
    const actionGroup = document.createElement("div");
    actionGroup.className = "action-group";

    const editButton = document.createElement("button");
    editButton.textContent = "Edit";
    editButton.dataset.action = "edit";
    editButton.dataset.syncId = cfg.id;
    actionGroup.appendChild(editButton);

    const duplicateButton = document.createElement("button");
    duplicateButton.textContent = "Duplicate";
    duplicateButton.dataset.action = "duplicate";
    duplicateButton.dataset.syncId = cfg.id;
    actionGroup.appendChild(duplicateButton);

    const deleteButton = document.createElement("button");
    deleteButton.textContent = "Delete";
    deleteButton.classList.add("danger");
    deleteButton.dataset.action = "delete";
    deleteButton.dataset.syncId = cfg.id;
    actionGroup.appendChild(deleteButton);

    actionCell.appendChild(actionGroup);
    row.appendChild(actionCell);

    configTableBody.appendChild(row);
  });
}

function renderAssetTable() {
  if (!ASSETS_UI_AVAILABLE) {
    return;
  }

  if (!state.assets.length) {
    assetsTable.classList.add("hidden");
    assetsEmptyState.classList.remove("hidden");
    assetsEmptyState.textContent = "No assets uploaded yet.";
    return;
  }

  assetsTable.classList.remove("hidden");
  assetsEmptyState.classList.add("hidden");
  assetsTableBody.innerHTML = "";

  state.assets.forEach((asset) => {
    const row = document.createElement("tr");

    const nameCell = document.createElement("td");
    const displayPath = asset.path || asset.name;
    if (asset.is_dir) {
      nameCell.textContent = `${displayPath}/`;
      nameCell.classList.add("muted");
    } else if (asset.url) {
      const link = document.createElement("a");
      link.href = asset.url;
      link.target = "_blank";
      link.rel = "noopener";
      link.textContent = displayPath;
      nameCell.appendChild(link);
    } else {
      nameCell.textContent = displayPath;
    }
    row.appendChild(nameCell);

    const sizeCell = document.createElement("td");
    sizeCell.textContent = asset.is_dir ? "—" : formatBytes(asset.size_bytes);
    row.appendChild(sizeCell);

    const modifiedCell = document.createElement("td");
    modifiedCell.textContent = formatTimestamp(asset.modified_at);
    row.appendChild(modifiedCell);

    const actionsCell = document.createElement("td");
    actionsCell.className = "actions-col";
    const group = document.createElement("div");
    group.className = "action-group";

    if (!asset.is_dir) {
      const copyButton = document.createElement("button");
      copyButton.textContent = "Copy path";
      copyButton.dataset.action = "copy";
      copyButton.dataset.assetPath = asset.path;
      group.appendChild(copyButton);

      const downloadButton = document.createElement("button");
      downloadButton.textContent = "Download";
      downloadButton.dataset.action = "download";
      downloadButton.dataset.assetUrl = asset.url;
      group.appendChild(downloadButton);

      const deleteButton = document.createElement("button");
      deleteButton.textContent = "Delete";
      deleteButton.classList.add("danger");
      deleteButton.dataset.action = "delete";
      deleteButton.dataset.assetPath = asset.path;
      group.appendChild(deleteButton);
    }

    actionsCell.appendChild(group);
    row.appendChild(actionsCell);

    assetsTableBody.appendChild(row);
  });
}

async function loadConfigs() {
  if (!CONFIG_UI_AVAILABLE) {
    return;
  }
  try {
    const data = await fetchJson("/config/syncs");
    state.configs = (data?.syncs || []).map((cfg) => ({ ...cfg }));
    renderConfigTable();
  } catch (error) {
    state.configs = [];
    renderConfigTable();
    showToast(`Failed to load configs: ${error.message}`, "error", 6000);
  }
}

function closeEditor(force = false) {
  if (!state.editor.open) {
    return;
  }
  if (!force && state.editor.dirty) {
    const confirmed = window.confirm("Discard unsaved changes?");
    if (!confirmed) {
      return;
    }
  }
  cancelValidation();
  state.editor = {
    open: false,
    mode: "edit",
    syncId: null,
    content: "",
    originalContent: "",
    parsed: null,
    dirty: false,
    validationStatus: null,
    validationMessage: null,
    validationController: null,
    validationTimer: null,
  };
  editorModal.classList.add("hidden");
  editorSummary.classList.add("hidden");
  document.body.style.overflow = "";
}

function updateEditorSummary() {
  const summary = state.editor.parsed;
  const validationStatus = state.editor.validationStatus;
  const validationMessage = state.editor.validationMessage;

  if (!summary && !validationStatus) {
    editorSummary.classList.add("hidden");
    editorSummary.innerHTML = "";
    return;
  }

  editorSummary.classList.remove("hidden");
  const lines = [];
  if (summary) {
    lines.push(`<strong>ID:</strong> ${summary.id || "—"}`);
    lines.push(`<strong>Type:</strong> ${summary.type || "—"}`);
    lines.push(`<strong>Schedule:</strong> ${formatSchedule(summary.schedule || {})}`);
    if (summary.description) {
      lines.push(`<strong>Description:</strong> ${summary.description}`);
    }
  }
  if (validationStatus === "error") {
    lines.push(`<span class=\"error-text\">${validationMessage}</span>`);
  }
  editorSummary.innerHTML = lines.join("<br>");
}

function setEditorDirty(isDirty) {
  state.editor.dirty = isDirty;
  saveButton.disabled = !isDirty;
}

function populateTemplateSelect(selectedId = "") {
  if (!CONFIG_UI_AVAILABLE) {
    return;
  }

  templateSelect.innerHTML = "";
  const customOption = document.createElement("option");
  customOption.value = "__current__";
  customOption.textContent = "Current content";
  templateSelect.appendChild(customOption);

  state.templates
    .filter((tpl) => tpl.valid !== false && typeof tpl.content === "string" && tpl.content.trim().length > 0)
    .forEach((tpl) => {
      const option = document.createElement("option");
      option.value = tpl.id;
      const suffix = tpl.source === "user" ? " (Custom)" : "";
      option.textContent = `${tpl.name}${suffix}`;
      option.title = tpl.description || "";
      templateSelect.appendChild(option);
    });

  templateSelect.value = selectedId && selectedId !== "custom" ? selectedId : "__current__";

  const hasTemplates = templateSelect.options.length > 1;
  templateSelect.disabled = !hasTemplates;
  applyTemplateButton.disabled = !hasTemplates;
}

function cancelValidation() {
  if (state.editor.validationController) {
    state.editor.validationController.abort();
    state.editor.validationController = null;
  }
  if (state.editor.validationTimer) {
    clearTimeout(state.editor.validationTimer);
    state.editor.validationTimer = null;
  }
}

function scheduleValidation() {
  cancelValidation();
  state.editor.validationTimer = setTimeout(async () => {
    state.editor.validationTimer = null;
    const controller = new AbortController();
    state.editor.validationController = controller;
    try {
      const detail = await fetchJson("/config/syncs/validate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: editorTextarea.value }),
        signal: controller.signal,
      });
      state.editor.validationStatus = "ok";
      state.editor.validationMessage = null;
      state.editor.parsed = detail.parsed || null;
      updateEditorSummary();
    } catch (error) {
      if (controller.signal.aborted) {
        return;
      }
      state.editor.validationStatus = "error";
      state.editor.validationMessage = error.message;
      updateEditorSummary();
    } finally {
      state.editor.validationController = null;
    }
  }, 500);
}

function openEditor({ mode, detail, title }) {
  state.editor.open = true;
  state.editor.mode = mode;
  state.editor.syncId = detail?.id || null;
  state.editor.content = detail?.content || "";
  state.editor.originalContent = detail?.content || "";
  state.editor.parsed = detail?.parsed || null;
  state.editor.dirty = false;
  state.editor.validationStatus = null;
  state.editor.validationMessage = null;
  editorTextarea.value = state.editor.content;
  editorTextarea.scrollTop = 0;
  editorSummary.classList.add("hidden");
  updateEditorSummary();

  editorTitle.textContent = title;
  editorSubtitle.textContent = mode === "create"
    ? "Provide a unique sync id in the YAML before saving."
    : `Editing ${detail.id}`;

  populateTemplateSelect(mode === "create" && state.templates.length ? state.templates[0].id : "__current__");
  applyTemplateButton.disabled = state.templates.length === 0;
  duplicateButton.classList.toggle("hidden", mode === "create");
  deleteButton.classList.toggle("hidden", mode === "create");
  saveButton.disabled = true;

  editorModal.classList.remove("hidden");
  document.body.style.overflow = "hidden";
}

async function handleEditSync(syncId, { duplicate = false } = {}) {
  try {
    const detail = await fetchJson(`/config/syncs/${encodeURIComponent(syncId)}`);
    const mode = duplicate ? "create" : "edit";
    const title = duplicate ? `Duplicate ${syncId}` : `Edit ${syncId}`;
    let adjustedDetail = detail;

    if (duplicate) {
      const newId = detail.id.endsWith("-copy") ? `${detail.id}-1` : `${detail.id}-copy`;
      const updatedContent = detail.content.replace(new RegExp(`id:\\s*${detail.id}`), `id: ${newId}`);
      adjustedDetail = { ...detail, id: newId, content: updatedContent };
    }

    openEditor({ mode, detail: adjustedDetail, title });
  } catch (error) {
    showToast(`Failed to open sync: ${error.message}`, "error", 6000);
  }
}

function handleNewSync() {
  if (!CONFIG_UI_AVAILABLE) {
    return;
  }
  const firstTemplate = state.templates.find(
    (tpl) => tpl.valid !== false && typeof tpl.content === "string" && tpl.content.trim().length > 0,
  );
  const detail = {
    id: firstTemplate ? firstTemplate.id : "",
    content: firstTemplate ? firstTemplate.content : "id: new-sync\ntype: playlist_mirror\nschedule:\n  interval: 10m\noptions: {}\n",
    parsed: null,
  };
  openEditor({ mode: "create", detail, title: "Create Sync" });
  if (firstTemplate) {
    templateSelect.value = firstTemplate.id;
    scheduleValidation();
  }
}

async function saveSync() {
  const content = editorTextarea.value;
  const method = state.editor.mode === "create" ? "POST" : "PUT";
  const endpoint = state.editor.mode === "create"
    ? "/config/syncs"
    : `/config/syncs/${encodeURIComponent(state.editor.syncId)}`;

  saveButton.disabled = true;
  try {
    const detail = await fetchJson(endpoint, {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
    });

    state.editor.syncId = detail.id;
    state.editor.originalContent = detail.content;
    state.editor.content = detail.content;
    state.editor.parsed = detail.parsed || null;
    state.editor.mode = "edit";
    state.editor.dirty = false;
    state.editor.validationStatus = "ok";
    state.editor.validationMessage = null;
    editorTitle.textContent = `Edit ${detail.id}`;
    editorSubtitle.textContent = `Editing ${detail.id}`;
    populateTemplateSelect("__current__");
    updateEditorSummary();
    showToast(`Saved sync '${detail.id}'`, "success");
    await loadConfigs();
  } catch (error) {
    showToast(`Save failed: ${error.message}`, "error", 6000);
  } finally {
    saveButton.disabled = false;
  }
}

async function deleteSync(syncId) {
  const confirmed = window.confirm(`Delete sync '${syncId}'? This cannot be undone.`);
  if (!confirmed) {
    return;
  }
  try {
    await fetchJson(`/config/syncs/${encodeURIComponent(syncId)}`, { method: "DELETE" });
    showToast(`Deleted '${syncId}'`, "success");
    closeEditor(true);
    await loadConfigs();
  } catch (error) {
    showToast(`Delete failed: ${error.message}`, "error", 6000);
  }
}

function applyTemplate() {
  if (!CONFIG_UI_AVAILABLE) {
    return;
  }
  const selected = templateSelect.value;
  if (!selected || selected === "__current__") {
    return;
  }
  const template = state.templates.find((tpl) => tpl.id === selected);
  if (!template || typeof template.content !== "string") {
    return;
  }
  editorTextarea.value = template.content;
  setEditorDirty(true);
  scheduleValidation();
}

function bindEventListeners() {
  refreshButton.addEventListener("click", () => {
    refreshData(true);
  });

  if (themeSelect) {
    themeSelect.addEventListener("change", (event) => {
      const value = event.target.value;
      setTheme(value);
    });
  }

  historyTailInput.addEventListener("change", () => {
    const value = Number.parseInt(historyTailInput.value, 10);
    if (!Number.isFinite(value) || value <= 0) {
      historyTailInput.value = "5";
    }
  });

  if (ASSETS_UI_AVAILABLE) {
    uploadAssetButton.addEventListener("click", () => {
      assetFileInput.click();
    });

    assetFileInput.addEventListener("change", async (event) => {
      const files = Array.from(event.target.files || []);
      if (!files.length) {
        return;
      }
      for (const file of files) {
        await uploadAsset(file);
      }
      assetFileInput.value = "";
    });

    assetsTableBody.addEventListener("click", (event) => {
      const button = event.target.closest("button");
      if (!button) {
        return;
      }
      const action = button.dataset.action;
      if (action === "copy") {
        const relPath = button.dataset.assetPath;
        if (!relPath) {
          return;
        }
        const relativePath = `assets/${relPath}`;
        navigator.clipboard
          .writeText(relativePath)
          .then(() => showToast(`Copied '${relativePath}'`, "success", 2500))
          .catch(() => showToast("Clipboard copy failed", "error", 5000));
      } else if (action === "download") {
        const url = button.dataset.assetUrl;
        if (url) {
          window.open(url, "_blank", "noopener");
        }
      } else if (action === "delete") {
        const relPath = button.dataset.assetPath;
        if (relPath) {
          deleteAsset(relPath);
        }
      }
    });
  }

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      if (state.refreshTimer) {
        clearInterval(state.refreshTimer);
        state.refreshTimer = null;
      }
    } else {
      refreshData(false);
      startAutoRefresh();
    }
  });

  configSearchInput.addEventListener("input", () => {
    renderConfigTable();
  });

  newSyncButton.addEventListener("click", () => {
    handleNewSync();
  });

  configTableBody.addEventListener("click", (event) => {
    const target = event.target.closest("button");
    if (!target) {
      return;
    }
    const syncId = target.dataset.syncId;
    const action = target.dataset.action;
    if (!syncId || !action) {
      return;
    }
    if (action === "edit") {
      handleEditSync(syncId);
    } else if (action === "duplicate") {
      handleEditSync(syncId, { duplicate: true });
    } else if (action === "delete") {
      deleteSync(syncId);
    }
  });

  editorCloseButton.addEventListener("click", () => closeEditor());

  templateSelect.addEventListener("change", () => {
    // no-op; apply on button press
  });

  applyTemplateButton.addEventListener("click", () => {
    applyTemplate();
  });

  duplicateButton.addEventListener("click", () => {
    if (!state.editor.syncId) {
      return;
    }
    handleEditSync(state.editor.syncId, { duplicate: true });
  });

  deleteButton.addEventListener("click", () => {
    if (!state.editor.syncId) {
      return;
    }
    deleteSync(state.editor.syncId);
  });

  saveButton.addEventListener("click", () => {
    saveSync();
  });

  editorTextarea.addEventListener("input", () => {
    const currentValue = editorTextarea.value;
    const dirty = currentValue !== state.editor.originalContent;
    setEditorDirty(dirty);
    state.editor.content = currentValue;
    scheduleValidation();
  });

  window.addEventListener("keydown", (event) => {
    if (!state.editor.open) {
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      closeEditor();
      return;
    }
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "s") {
      event.preventDefault();
      if (state.editor.dirty) {
        saveSync();
      }
    }
  });
}

async function initialise() {
  state.theme.mode = loadThemePreference();
  applyTheme(state.theme.mode);
  bindEventListeners();
  await Promise.all([refreshData(false), loadTemplates(), loadAssets()]);
  await loadConfigs();
  startAutoRefresh();
}

initialise().catch((error) => {
  showToast(`Failed to initialise dashboard: ${error.message}`, "error", 6000);
});
