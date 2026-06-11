function normalizeAuditSearchQuery(value) {
    return String(value || "").trim().toLowerCase();
}

function makeAuditSearchText(event) {
    return [
        event?.user?.username,
        event?.action,
        event?.status,
        event?.timestamp,
        event?.vm_id,
        event?.lab_name,
        event?.details ? JSON.stringify(event.details) : "",
    ].join(" ").toLowerCase();
}

function filterAuditItems(items, query) {
    const normalizedQuery = normalizeAuditSearchQuery(query);
    if (!normalizedQuery) {
        return Array.isArray(items) ? items : [];
    }
    return (Array.isArray(items) ? items : []).filter((event) => makeAuditSearchText(event).includes(normalizedQuery));
}

function getAuditEventSeverity(event) {
    const action = String(event?.action || "").toLowerCase();
    const status = String(event?.status || "").toLowerCase();
    const hasError = Boolean(event?.details?.error);

    if (status === "error" || hasError || action.includes("critical")) return "critical";
    if (status === "denied") return "denied";
    if (action.startsWith("auth.")) return "auth";
    if (action.startsWith("admin.")) return "admin";
    if (status === "ok") return "ok";
    return "info";
}

function filterAuditItemsBySeverity(items, severity) {
    const normalized = String(severity || "all").toLowerCase();
    if (normalized === "all") {
        return Array.isArray(items) ? items : [];
    }
    return (Array.isArray(items) ? items : []).filter((event) => getAuditEventSeverity(event) === normalized);
}

function getAuditEventTone(event) {
    const severity = getAuditEventSeverity(event);

    if (severity === "critical") {
        return {
            card: "border-rose-700/70 bg-rose-950/20 text-rose-100",
            badge: "border-rose-600/70 bg-rose-950/60 text-rose-200",
            meta: "text-rose-200/70",
            tag: "Critique",
        };
    }
    if (severity === "denied") {
        return {
            card: "border-amber-700/60 bg-amber-950/15 text-amber-100",
            badge: "border-amber-600/60 bg-amber-950/55 text-amber-200",
            meta: "text-amber-200/70",
            tag: "Refuse",
        };
    }
    if (severity === "auth") {
        return {
            card: "border-sky-700/45 bg-sky-950/10 text-sky-100",
            badge: "border-sky-600/50 bg-sky-950/50 text-sky-200",
            meta: "text-sky-200/65",
            tag: "Auth",
        };
    }
    if (severity === "admin") {
        return {
            card: "border-fuchsia-700/45 bg-fuchsia-950/10 text-fuchsia-100",
            badge: "border-fuchsia-600/50 bg-fuchsia-950/50 text-fuchsia-200",
            meta: "text-fuchsia-200/65",
            tag: "Admin",
        };
    }
    if (severity === "ok") {
        return {
            card: "border-emerald-700/45 bg-emerald-950/10 text-emerald-100",
            badge: "border-emerald-600/50 bg-emerald-950/50 text-emerald-200",
            meta: "text-emerald-200/65",
            tag: "OK",
        };
    }
    return {
        card: "border-slate-700/70 bg-slate-950/30 text-slate-100",
        badge: "border-slate-600/60 bg-slate-900/80 text-slate-300",
        meta: "text-slate-400",
        tag: "Info",
    };
}

function captureAuditInputFocus() {
    const active = document.activeElement;
    if (!(active instanceof HTMLInputElement) && !(active instanceof HTMLSelectElement)) {
        return null;
    }
    const action = active.dataset.action || "";
    if (
        action !== "admin-user-audit-search"
        && action !== "admin-system-audit-search"
        && action !== "admin-user-audit-severity"
        && action !== "admin-system-audit-severity"
    ) {
        return null;
    }
    return {
        action,
        username: active.dataset.username || "",
        selectionStart: active instanceof HTMLInputElement ? active.selectionStart : null,
        selectionEnd: active instanceof HTMLInputElement ? active.selectionEnd : null,
    };
}

function restoreAuditInputFocus(focusState) {
    if (!focusState) {
        return;
    }
    const selector = focusState.action === "admin-user-audit-search"
        ? `input[data-action="admin-user-audit-search"][data-username="${focusState.username}"]`
        : focusState.action === "admin-system-audit-search"
            ? 'input[data-action="admin-system-audit-search"]'
            : focusState.action === "admin-user-audit-severity"
                ? `select[data-action="admin-user-audit-severity"][data-username="${focusState.username}"]`
                : 'select[data-action="admin-system-audit-severity"]';
    const input = document.querySelector(selector);
    if (!(input instanceof HTMLInputElement) && !(input instanceof HTMLSelectElement)) {
        return;
    }
    input.focus();
    if (input instanceof HTMLInputElement && focusState.selectionStart !== null && focusState.selectionEnd !== null) {
        input.setSelectionRange(focusState.selectionStart, focusState.selectionEnd);
    }
}

function renderAuditEventRow(event) {
    const tone = getAuditEventTone(event);
    const target = [event.vm_id, event.lab_name].filter(Boolean).join(" / ");
    return `
        <div class="rounded-md border ${tone.card} px-2 py-1">
            <div class="flex items-start justify-between gap-2">
                <p class="text-[11px] font-medium leading-4">${escapeHtml(event.action || "action")}</p>
                <span class="shrink-0 rounded-full border px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide ${tone.badge}">${escapeHtml(tone.tag)}</span>
            </div>
            <p class="text-[10px] mt-0.5 leading-4 ${tone.meta}">${escapeHtml(event.timestamp || "")}</p>
            ${target ? `<p class="text-[10px] text-slate-400 mt-0.5 leading-4">${escapeHtml(target)}</p>` : ""}
        </div>
    `;
}

function renderAuditSeverityOptions(selected) {
    const current = String(selected || "all").toLowerCase();
    const options = [
        ["all", "Toutes severites"],
        ["critical", "Critique"],
        ["denied", "Refuse"],
        ["auth", "Auth"],
        ["admin", "Admin"],
        ["ok", "OK"],
        ["info", "Info"],
    ];
    return options.map(([value, label]) =>
        `<option value="${value}" ${current === value ? "selected" : ""}>${label}</option>`
    ).join("");
}
function renderCompactAuditEventRow(event) {
    const tone = getAuditEventTone(event);
    const target = [event?.vm_id, event?.lab_name].filter(Boolean).join("/");
    const actor = String(event?.user?.username || "anonyme");
    const parts = [
        escapeHtml(String(event?.action || "action")),
        escapeHtml(String(event?.timestamp || "")),
        escapeHtml(actor) + (target ? `·${escapeHtml(target)}` : ""),
    ];
    return `
        <div class="rounded border ${tone.card} px-2 py-1 flex items-center gap-2 min-w-0">
            <span class="shrink-0 rounded-full border px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide ${tone.badge}">${escapeHtml(tone.tag)}</span>
            <p class="text-[10px] font-mono truncate ${tone.meta}">${parts.join("  ")}</p>
        </div>
    `;
}

function showAuditLogModal({
    title,
    events,
    initialQuery,
    initialSeverity,
    emptyMessage,
    onQueryChange,
    onSeverityChange,
}) {
    const allEvents = Array.isArray(events) ? events : [];
    const overlay = showOverlayModal({
        title,
        widthClass: "max-w-5xl",
        bodyHtml: `
            <div class="space-y-3">
                <div class="grid grid-cols-1 sm:grid-cols-[1fr_auto_auto] gap-2">
                    <input id="audit-modal-search" type="search" value="${escapeHtml(String(initialQuery || ""))}" placeholder="Filtrer les logs" class="w-full rounded-lg border border-slate-700 bg-slate-950/70 px-2.5 py-2 text-xs text-slate-100 placeholder:text-slate-500 focus:outline-none focus:ring-2 focus:ring-fuchsia-500" />
                    <select id="audit-modal-severity" class="rounded-lg border border-slate-700 bg-slate-950/70 px-2.5 py-2 text-xs text-slate-100 focus:outline-none focus:ring-2 focus:ring-fuchsia-500">${renderAuditSeverityOptions(initialSeverity)}</select>
                    <p id="audit-modal-count" class="text-xs text-slate-400 self-center text-right"></p>
                </div>
                <div id="audit-modal-list" class="max-h-[62vh] overflow-y-auto space-y-1 pr-1"></div>
            </div>
        `,
    });

    const searchInput = overlay?.querySelector("#audit-modal-search");
    const severitySelect = overlay?.querySelector("#audit-modal-severity");
    const list = overlay?.querySelector("#audit-modal-list");
    const count = overlay?.querySelector("#audit-modal-count");
    if (!(searchInput instanceof HTMLInputElement) || !(severitySelect instanceof HTMLSelectElement) || !(list instanceof HTMLElement) || !(count instanceof HTMLElement)) {
        return;
    }

    const render = () => {
        const query = searchInput.value;
        const severity = severitySelect.value;
        const filtered = filterAuditItemsBySeverity(filterAuditItems(allEvents, query), severity);
        count.textContent = `${filtered.length}/${allEvents.length}`;
        list.innerHTML = filtered.length
            ? filtered.map(renderCompactAuditEventRow).join("")
            : `<p class="text-xs text-slate-500">${escapeHtml(emptyMessage || "Aucun log correspondant.")}</p>`;
    };

    searchInput.addEventListener("input", () => {
        onQueryChange?.(searchInput.value);
        render();
    });

    severitySelect.addEventListener("change", () => {
        onSeverityChange?.(severitySelect.value);
        render();
    });

    render();
    searchInput.focus();
}

function openUserAuditModal(username) {
    const usernameKey = String(username || "");
    if (!usernameKey) {
        return;
    }
    const events = (Array.isArray(latestAdminAuditItems) ? latestAdminAuditItems : [])
        .filter((event) => String(event?.user?.username || "") === usernameKey);
    if (!events.length) {
        showToastMessage("Logs", `Aucun log pour ${usernameKey}.`);
        return;
    }

    const initialQuery = adminAuditUserSearchQueries[usernameKey] || "";
    const initialSeverity = adminAuditUserSeverityFilters[usernameKey] || "all";
    showAuditLogModal({
        title: `Logs de ${usernameKey}`,
        events,
        initialQuery,
        initialSeverity,
        emptyMessage: "Aucun log correspondant.",
        onQueryChange: (value) => {
            adminAuditUserSearchQueries[usernameKey] = value;
        },
        onSeverityChange: (value) => {
            adminAuditUserSeverityFilters[usernameKey] = value;
        },
    });
}

function openSystemAuditModal() {
    const events = (Array.isArray(latestAdminAuditItems) ? latestAdminAuditItems : []).filter(
        (event) => !event.user?.username || event.user.username === "anonyme",
    );
    if (!events.length) {
        showToastMessage("Logs système", "Aucun événement système récent.");
        return;
    }

    showAuditLogModal({
        title: "Logs système / anonyme",
        events,
        initialQuery: adminSystemAuditSearchQuery,
        initialSeverity: adminSystemAuditSeverityFilter,
        emptyMessage: "Aucun log système correspondant.",
        onQueryChange: (value) => {
            adminSystemAuditSearchQuery = value;
        },
        onSeverityChange: (value) => {
            adminSystemAuditSeverityFilter = value;
        },
    });
}

async function openVmAuditModal(vmId) {
    const vmIdKey = String(vmId || "");
    if (!vmIdKey) {
        return;
    }

    try {
        const response = await apiFetch(`/api/admin/vms/${encodeURIComponent(vmIdKey)}/logs?limit=200`);
        if (!response.ok) {
            showToastMessage("Logs VM", `Erreur lors du chargement des logs pour ${vmIdKey}.`, true);
            return;
        }
        const payload = await response.json();
        const events = Array.isArray(payload?.items) ? payload.items : [];

        if (!events.length) {
            showToastMessage("Logs VM", `Aucun log pour ${vmIdKey}.`);
            return;
        }

        const initialQuery = adminAuditVmSearchQueries[vmIdKey] || "";
        const initialSeverity = adminAuditVmSeverityFilters[vmIdKey] || "all";
        showAuditLogModal({
            title: `Logs de ${vmIdKey}`,
            events,
            initialQuery,
            initialSeverity,
            emptyMessage: "Aucun log correspondant.",
            onQueryChange: (value) => {
                adminAuditVmSearchQueries[vmIdKey] = value;
            },
            onSeverityChange: (value) => {
                adminAuditVmSeverityFilters[vmIdKey] = value;
            },
        });
    } catch (error) {
        console.error("Error loading VM logs:", error);
        showToastMessage("Logs VM", `Erreur lors du chargement des logs pour ${vmIdKey}.`, true);
    }
}

// Affiche uniquement les événements sans utilisateur authentifié (système / anonyme)
function renderAuditEvents(items) {
    const container = document.getElementById("admin-audit");
    if (!container) {
        return;
    }

    const anonymeEvents = (Array.isArray(items) ? items : []).filter(
        (e) => !e.user?.username || e.user.username === "anonyme",
    );

    if (!anonymeEvents.length) {
        container.innerHTML = '<p class="text-xs text-slate-500">Aucun événement système récent.</p>';
        return;
    }

    const counters = {
        critical: 0,
        denied: 0,
        auth: 0,
        admin: 0,
        ok: 0,
        info: 0,
    };
    for (const event of anonymeEvents) {
        const severity = getAuditEventSeverity(event);
        if (Object.prototype.hasOwnProperty.call(counters, severity)) {
            counters[severity] += 1;
        }
    }

    container.innerHTML = `
        <div class="rounded-lg border border-slate-700 bg-slate-900/60 p-3 space-y-3">
            <div class="flex items-center justify-between gap-3">
                <div>
                    <p class="text-xs font-semibold text-slate-100">Système / anonyme</p>
                    <p class="text-[11px] text-slate-400 mt-1">${anonymeEvents.length} événement${anonymeEvents.length > 1 ? "s" : ""}</p>
                </div>
                <button type="button" data-action="open-system-audit-modal" class="text-xs px-2 py-1 rounded border border-fuchsia-700 text-fuchsia-200 hover:bg-fuchsia-900/30">Voir les logs</button>
            </div>
            <div class="flex flex-wrap items-center gap-2 text-[11px]">
                <span class="rounded-full border border-rose-700/60 bg-rose-950/30 px-2 py-0.5 text-rose-200">Critique: ${counters.critical}</span>
                <span class="rounded-full border border-amber-700/60 bg-amber-950/30 px-2 py-0.5 text-amber-200">Refuse: ${counters.denied}</span>
                <span class="rounded-full border border-sky-700/60 bg-sky-950/25 px-2 py-0.5 text-sky-200">Auth: ${counters.auth}</span>
                <span class="rounded-full border border-fuchsia-700/60 bg-fuchsia-950/25 px-2 py-0.5 text-fuchsia-200">Admin: ${counters.admin}</span>
                <span class="rounded-full border border-emerald-700/60 bg-emerald-950/25 px-2 py-0.5 text-emerald-200">OK: ${counters.ok}</span>
                <span class="rounded-full border border-slate-700 px-2 py-0.5 text-slate-300">Info: ${counters.info}</span>
            </div>
        </div>
    `;
}

function applyAdminAuditFilter() {
    const usersContainer = document.getElementById("admin-users");
    const usersDetailsState = captureDetailsState(usersContainer);

    const auditByUser = new Map();
    for (const event of latestAdminAuditItems) {
        const key = event.user?.username || "anonyme";
        if (!auditByUser.has(key)) {
            auditByUser.set(key, []);
        }
        auditByUser.get(key).push(event);
    }

    renderAdminUsers(latestAdminUsers, auditByUser);
    renderAuditEvents(latestAdminAuditItems);
    renderActivityRail();

    restoreDetailsState(usersContainer, usersDetailsState);
}
