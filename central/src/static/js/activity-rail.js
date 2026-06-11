const ACTIVITY_HISTORY_STORAGE_KEY = "vlm:activityHistory";
const ACTIVITY_HISTORY_MAX_ITEMS = 120;
const ACTIVITY_AUDIT_REFRESH_MS = 30000;
const activityInFlightActions = new Map();
let activityLocalHistory = [];
let activityAuditUserHistory = [];
let activityAuditLastFetchAt = 0;
let trackedJobRefreshInFlight = false;
let trackedJobLastRefreshAt = 0;
const TRACKED_JOB_REFRESH_MS = 15000;
const TRACKED_JOB_MAX_AGE_MS = 6 * 60 * 60 * 1000;
const TRACKED_JOB_ERROR_STALE_MS = 15 * 60 * 1000;

function _escapeActivityHtml(value) {
    if (typeof globalThis.escapeHtml === "function") {
        return globalThis.escapeHtml(value);
    }
    const str = String(value ?? "");
    return str
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");
}

function _formatActivityDate(iso) {
    if (!iso) {
        return "-";
    }
    const dt = new Date(String(iso));
    if (Number.isNaN(dt.getTime())) {
        return "-";
    }
    return dt.toLocaleString("fr-FR");
}

function _saveActivityHistoryToStorage() {
    try {
        localStorage.setItem(ACTIVITY_HISTORY_STORAGE_KEY, JSON.stringify(activityLocalHistory.slice(0, ACTIVITY_HISTORY_MAX_ITEMS)));
    } catch (_) {
    }
}

function _loadActivityHistoryFromStorage() {
    try {
        const raw = localStorage.getItem(ACTIVITY_HISTORY_STORAGE_KEY);
        const parsed = raw ? JSON.parse(raw) : [];
        activityLocalHistory = Array.isArray(parsed) ? parsed.slice(0, ACTIVITY_HISTORY_MAX_ITEMS) : [];
    } catch (_) {
        activityLocalHistory = [];
    }
}

function _activityStatusTone(status) {
    const normalized = String(status || "in-progress");
    if (normalized === "ok") return "text-emerald-300";
    if (normalized === "error") return "text-rose-300";
    return "text-amber-300";
}

function _updateActivityDockCount() {
    const dock = document.getElementById("activity-docked-widgets");
    const count = document.getElementById("activity-docked-count");
    if (!(count instanceof HTMLElement)) {
        return;
    }
    const size = dock instanceof HTMLElement ? dock.children.length : 0;
    count.textContent = String(size);
}

function dockMiniWidget(node) {
    const dock = document.getElementById("activity-docked-widgets");
    if (!(dock instanceof HTMLElement) || !(node instanceof HTMLElement)) {
        return;
    }
    node.classList.add("activity-docked-widget");
    dock.appendChild(node);
    _updateActivityDockCount();
}

function setActivityRailOpen(open) {
    const rail = document.getElementById("activity-rail");
    const shouldOpen = Boolean(open);
    if (!(rail instanceof HTMLElement)) {
        return;
    }
    rail.classList.toggle("activity-rail-open", shouldOpen);
    document.body.classList.toggle("activity-rail-expanded", shouldOpen);
}

function beginUserActivity(input) {
    const id = String(input?.id || `activity:${Date.now()}:${Math.random().toString(36).slice(2, 8)}`);
    const item = {
        id,
        title: String(input?.title || "Action"),
        target: String(input?.target || ""),
        details: String(input?.details || ""),
        status: "in-progress",
        started_at: new Date().toISOString(),
    };
    activityInFlightActions.set(id, item);
    return id;
}

function endUserActivity(id, status = "ok", details = "") {
    const key = String(id || "");
    if (!key) {
        return;
    }
    const existing = activityInFlightActions.get(key);
    if (existing) {
        activityInFlightActions.delete(key);
        const done = {
            ...existing,
            status: String(status || "ok"),
            details: String(details || existing.details || ""),
            ended_at: new Date().toISOString(),
        };
        activityLocalHistory.unshift(done);
    } else {
        activityLocalHistory.unshift({
            id: key,
            title: "Action",
            target: "",
            details: String(details || ""),
            status: String(status || "ok"),
            started_at: new Date().toISOString(),
            ended_at: new Date().toISOString(),
        });
    }
    activityLocalHistory = activityLocalHistory.slice(0, ACTIVITY_HISTORY_MAX_ITEMS);
    _saveActivityHistoryToStorage();
}

function rebuildInFlightActivityFromTrackedJobs() {
    const recovered = new Map(activityInFlightActions);

    const recoverFromMap = (stateMap, prefix, title, defaultDetails) => {
        for (const [labKey, state] of stateMap.entries()) {
            if (!state?.inProgress) {
                continue;
            }
            const parts = String(labKey || "").split("::");
            const vmId = parts[0] || "";
            const labName = parts[1] || "";
            if (!vmId || !labName) {
                continue;
            }

            const id = `${prefix}:${labKey}`;
            if (recovered.has(id)) {
                continue;
            }

            recovered.set(id, {
                id,
                title,
                target: `${labName} @ ${vmId}`,
                details: state?.jobId ? defaultDetails : "Initialisation...",
                status: "in-progress",
                started_at: state?.startedAt || new Date().toISOString(),
            });
        }
    };

    recoverFromMap(labReconfigureState, "reconfigure", "Reconfiguration", "Suivi en cours");
    recoverFromMap(labSetDefaultState, "set-default", "Base par defaut", "Suivi en cours");
    recoverFromMap(labConfigDiffState, "config-diff", "Diff configuration", "Comparaison en cours");

    activityInFlightActions.clear();
    for (const [id, item] of recovered.entries()) {
        activityInFlightActions.set(id, item);
    }
}

function renderActivityRail() {
    const rail = document.getElementById("activity-rail");
    if (!(rail instanceof HTMLElement)) {
        return;
    }
    rail.classList.remove("hidden");

    const currentList = document.getElementById("activity-current-list");
    const currentCount = document.getElementById("activity-current-count");
    const historyList = document.getElementById("activity-history-list");
    const historyCount = document.getElementById("activity-history-count");

    const currentItems = Array.from(activityInFlightActions.values());
    if (currentCount instanceof HTMLElement) {
        currentCount.textContent = String(currentItems.length);
    }
    if (currentList instanceof HTMLElement) {
        currentList.innerHTML = currentItems.length
            ? currentItems.map((item) => `
                <article class="rounded border border-amber-700/40 bg-amber-950/10 px-2 py-1.5">
                    <p class="text-xs text-slate-100 font-medium">${_escapeActivityHtml(item.title || "Action")}</p>
                    <p class="text-[11px] text-slate-400">${_escapeActivityHtml(item.target || "")}</p>
                    <p class="text-[11px] text-amber-300">${_escapeActivityHtml(item.details || "En cours...")}</p>
                </article>
            `).join("")
            : '<p class="text-[11px] text-slate-500">Aucune action en cours.</p>';
    }

    const mergedHistory = [...(Array.isArray(activityAuditUserHistory) ? activityAuditUserHistory : []), ...activityLocalHistory]
        .slice(0, ACTIVITY_HISTORY_MAX_ITEMS);

    if (historyCount instanceof HTMLElement) {
        historyCount.textContent = String(mergedHistory.length);
    }
    if (historyList instanceof HTMLElement) {
        historyList.innerHTML = mergedHistory.length
            ? mergedHistory.map((item) => {
                const status = String(item?.status || "ok");
                return `
                    <article class="rounded border border-slate-700 bg-slate-900/40 px-2 py-1.5">
                        <div class="flex items-center justify-between gap-2">
                            <p class="text-xs text-slate-100 font-medium">${_escapeActivityHtml(item?.title || "Action")}</p>
                            <span class="text-[10px] ${_activityStatusTone(status)}">${_escapeActivityHtml(status)}</span>
                        </div>
                        <p class="text-[11px] text-slate-400">${_escapeActivityHtml(item?.target || "")}</p>
                        <p class="text-[11px] text-slate-300">${_escapeActivityHtml(item?.details || "")}</p>
                        <p class="text-[10px] text-slate-500">${_escapeActivityHtml(_formatActivityDate(item?.ended_at || item?.started_at))}</p>
                    </article>
                `;
            }).join("")
            : '<p class="text-[11px] text-slate-500">Aucun historique disponible.</p>';
    }

    _updateActivityDockCount();
}

async function refreshActivityAuditHistory(force = false) {
    const now = Date.now();
    if (!force && now - activityAuditLastFetchAt < ACTIVITY_AUDIT_REFRESH_MS) {
        return;
    }
    activityAuditLastFetchAt = now;
    try {
        const response = await apiFetch(`/api/activity/history?limit=${ACTIVITY_HISTORY_MAX_ITEMS}&action_prefix=lab.`);
        if (response.ok) {
            const payload = await response.json();
            const items = Array.isArray(payload?.items) ? payload.items : [];
            activityAuditUserHistory = items.map((event, index) => {
                const action = String(event?.action || "lab.action");
                const status = String(event?.status || "ok").toLowerCase();
                const vmId = String(event?.vm_id || "");
                const labName = String(event?.lab_name || "");
                const details = event?.details && typeof event.details === "object" ? event.details : {};

                const title = action.startsWith("lab.")
                    ? action.slice(4).replaceAll("_", " ")
                    : action;

                const mappedStatus = status === "started"
                    ? "in-progress"
                    : (status === "ok" ? "ok" : "error");

                let detailText = "";
                if (typeof details.error === "string" && details.error) {
                    detailText = details.error;
                } else if (typeof details.stage === "string" && details.stage) {
                    detailText = `stage=${details.stage}`;
                }

                return {
                    id: `audit:${String(event?.timestamp || "")}:${action}:${labName}:${index}`,
                    title,
                    target: [labName, vmId].filter(Boolean).join(" @ "),
                    details: detailText,
                    status: mappedStatus,
                    started_at: String(event?.timestamp || ""),
                    ended_at: String(event?.timestamp || ""),
                };
            });
        }
    } catch (_) {
        activityAuditUserHistory = Array.isArray(activityAuditUserHistory) ? activityAuditUserHistory : [];
    }
    renderActivityRail();
}

function _restoreTrackedJobWindows() {
    void refreshTrackedJobStates({ force: true });
}

_loadActivityHistoryFromStorage();
void refreshActivityAuditHistory(true);
setInterval(() => {
    void refreshActivityAuditHistory(false);
}, ACTIVITY_AUDIT_REFRESH_MS);
