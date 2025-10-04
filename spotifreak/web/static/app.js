const supervisorStatusEl = document.getElementById("supervisor-status");
const syncListEl = document.getElementById("sync-list");
const lastUpdatedEl = document.getElementById("last-updated");
const notificationsEl = document.getElementById("notifications");
const refreshButton = document.getElementById("refresh-button");
const historyTailInput = document.getElementById("history-tail");
const syncTemplate = document.getElementById("sync-card-template");

let refreshTimer = null;

async function fetchJson(url, options) {
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
    throw new Error(message);
  }
  if (response.status === 204) {
    return null;
  }
  return response.json();
}

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

function describeOptions(options) {
  if (!options || typeof options !== "object" || Array.isArray(options) && options.length === 0) {
    return "—";
  }
  const entries = Object.entries(options);
  if (!entries.length) {
    return "—";
  }
  return entries
    .map(([key, value]) => `${key}: ${formatOptionValue(value)}`)
    .join("\n");
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
    card.querySelector(".sync-options").textContent = describeOptions(sync.options);

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
  if (refreshTimer) {
    clearInterval(refreshTimer);
  }
  refreshTimer = setInterval(() => refreshData(false), 5000);
}

refreshButton.addEventListener("click", () => {
  refreshData(true);
});

historyTailInput.addEventListener("change", () => {
  const value = Number.parseInt(historyTailInput.value, 10);
  if (!Number.isFinite(value) || value <= 0) {
    historyTailInput.value = "5";
  }
  refreshData(false);
});

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    if (refreshTimer) {
      clearInterval(refreshTimer);
      refreshTimer = null;
    }
  } else {
    refreshData(false);
    startAutoRefresh();
  }
});

refreshData(false).finally(() => {
  startAutoRefresh();
});
