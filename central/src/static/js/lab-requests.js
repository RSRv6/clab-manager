function resetLabRequestsCache() {
    latestLabRequests = [];
    labRequestsLastSignature = "";
    labRequestsInFlight = false;

    const count = document.getElementById("lab-requests-count");
    if (count) {
        count.textContent = "0";
    }
    const body = document.getElementById("lab-requests-body");
    if (body) {
        body.innerHTML = '<p class="text-xs text-slate-500">Chargement des demandes...</p>';
    }
}

function renderLabRequestsPanel() {
    const panel = document.getElementById("lab-requests-panel");
    const help = document.getElementById("lab-requests-help");
    const form = document.getElementById("lab-request-form");
    const body = document.getElementById("lab-requests-body");
    const count = document.getElementById("lab-requests-count");
    if (!(panel instanceof HTMLElement) || !(body instanceof HTMLElement) || !(count instanceof HTMLElement)) {
        return;
    }

    if (!currentUser) {
        panel.classList.add("hidden");
        body.innerHTML = '<p class="text-xs text-slate-500">Authentification requise.</p>';
        count.textContent = "0";
        return;
    }

    panel.classList.remove("hidden");
    const adminMode = isAdmin();
    if (form instanceof HTMLElement) {
        form.classList.toggle("hidden", adminMode);
    }
    if (help instanceof HTMLElement) {
        help.textContent = adminMode
            ? "Vue admin: toutes les demandes sont visibles, avec prise en charge et suivi de statut."
            : "Soumettez vos besoins LAB. Seuls les admins (hors group-admin) et vous voyez votre demande.";
    }

    count.textContent = String(latestLabRequests.length);
    if (!latestLabRequests.length) {
        body.innerHTML = `<p class="text-xs text-slate-500">${adminMode ? "Aucune demande utilisateur en attente." : "Vous n'avez encore soumis aucune demande."}</p>`;
        return;
    }

    body.innerHTML = latestLabRequests.map((item) => {
        const requestId = String(item?.id || "");
        const status = String(item?.status || "pris_en_compte").toLowerCase();
        const assignedTo = String(item?.assigned_admin_full_name || item?.assigned_admin_username || "").trim();
        const ownerName = String(item?.owner_full_name || item?.owner_username || "").trim();
        const ownerUsername = String(item?.owner_username || "").trim();
        const createdAt = formatCompletionDate(item?.created_at);
        const updatedAt = formatCompletionDate(item?.updated_at);
        const claimLabel = item?.assigned_admin_username
            ? (String(item.assigned_admin_username || "").toLowerCase() === String(currentUser?.username || "").toLowerCase() ? "Reprendre" : "Reprendre la main")
            : "Prendre en charge";

        const adminControls = adminMode
            ? `
                <div class="mt-3 grid gap-2 md:grid-cols-[auto_1fr_auto] items-start">
                    <button type="button" data-action="lab-request-claim" data-request-id="${escapeHtml(requestId)}" class="text-[11px] px-2 py-1 rounded border border-fuchsia-700 text-fuchsia-200 hover:bg-fuchsia-900/30">${escapeHtml(claimLabel)}</button>
                    <div>
                        <label class="block text-[10px] uppercase tracking-wide text-slate-500 mb-1">Reponse admin</label>
                        <textarea data-field="lab-request-response" class="w-full rounded border border-slate-700 bg-slate-950 px-2 py-1.5 text-xs text-slate-100" rows="2" placeholder="Reponse visible par l'utilisateur">${escapeHtml(String(item?.admin_response || ""))}</textarea>
                    </div>
                    <div class="flex flex-col gap-2">
                        <select data-field="lab-request-status" class="rounded border border-slate-700 bg-slate-950 px-2 py-1.5 text-xs text-slate-100">
                            ${LAB_REQUEST_STATUSES.map((value) => `<option value="${value}" ${value === status ? "selected" : ""}>${labRequestStatusLabel(value)}</option>`).join("")}
                        </select>
                        <button type="button" data-action="lab-request-save" data-request-id="${escapeHtml(requestId)}" class="text-[11px] px-2 py-1 rounded border border-cyan-700 text-cyan-200 hover:bg-cyan-900/30">Enregistrer</button>
                        <button type="button" data-action="lab-request-delete" data-request-id="${escapeHtml(requestId)}" class="text-[11px] px-2 py-1 rounded border border-rose-700 text-rose-200 hover:bg-rose-900/30">Supprimer</button>
                    </div>
                </div>
            `
            : "";

        const contentHtml = `
            <article data-lab-request-id="${escapeHtml(requestId)}" class="rounded-lg border border-slate-800 bg-slate-900/50 p-3">
                <div class="flex flex-wrap items-center justify-between gap-2">
                    <p class="text-sm font-semibold text-slate-100">${escapeHtml(item?.suggestion_title || "Suggestion LAB")}</p>
                    <span class="text-[10px] px-2 py-0.5 rounded-full border ${labRequestStatusTone(status)}">${escapeHtml(labRequestStatusLabel(status))}</span>
                </div>
                <p class="text-xs text-slate-400 mt-1">Demandeur: ${escapeHtml(ownerName || ownerUsername)}</p>
                <p class="text-xs text-slate-400">Creee: ${escapeHtml(createdAt || "-")} · MAJ: ${escapeHtml(updatedAt || "-")}</p>
                <p class="text-xs text-slate-300 mt-2 whitespace-pre-wrap">${escapeHtml(String(item?.need_details || ""))}</p>
                ${item?.resources_requirements ? `<p class="text-xs text-slate-400 mt-2"><span class="text-slate-500">Ressources:</span> ${escapeHtml(item.resources_requirements)}</p>` : ""}
                ${item?.requested_router_images ? `<p class="text-xs text-slate-400 mt-1"><span class="text-slate-500">Images routeur:</span> ${escapeHtml(item.requested_router_images)}</p>` : ""}
                ${assignedTo ? `<p class="text-xs text-slate-400 mt-2">Pris en charge par: <span class="text-slate-200">${escapeHtml(assignedTo)}</span></p>` : '<p class="text-xs text-slate-500 mt-2">Aucun admin assigne.</p>'}
                ${item?.admin_response ? `<p class="text-xs text-cyan-200 mt-2 whitespace-pre-wrap"><span class="text-cyan-400">Reponse:</span> ${escapeHtml(item.admin_response)}</p>` : ""}
                ${adminControls}
            </article>
        `;

        if (!adminMode) {
            return contentHtml;
        }

        return `
            <details class="rounded-lg border border-slate-800 bg-slate-950/40" data-lab-request-details="${escapeHtml(requestId)}">
                <summary class="cursor-pointer px-3 py-2 flex flex-wrap items-center justify-between gap-2">
                    <span class="text-xs text-slate-200">${escapeHtml(ownerName || ownerUsername)} · ${escapeHtml(item?.suggestion_title || "Suggestion LAB")}</span>
                    <span class="text-[10px] px-2 py-0.5 rounded-full border ${labRequestStatusTone(status)}">${escapeHtml(labRequestStatusLabel(status))}</span>
                </summary>
                <div class="p-2">${contentHtml}</div>
            </details>
        `;
    }).join("");
}

function isEditingLabRequestPanel() {
    const active = document.activeElement;
    if (!(active instanceof HTMLElement)) {
        return false;
    }
    return Boolean(
        active.closest("#lab-request-form")
        || active.closest("#lab-requests-body")
    );
}

async function refreshLabRequests(options = {}) {
    const { force = false, silent = false } = options;
    if (!currentUser) {
        resetLabRequestsCache();
        renderLabRequestsPanel();
        return;
    }
    if (labRequestsInFlight && !force) {
        return;
    }
    if (!force && isEditingLabRequestPanel()) {
        return;
    }

    const body = document.getElementById("lab-requests-body");
    if (!silent && body instanceof HTMLElement) {
        body.innerHTML = '<p class="text-xs text-slate-500">Chargement des demandes...</p>';
    }

    labRequestsInFlight = true;
    try {
        const response = await apiFetch("/api/lab-requests");
        if (!response.ok) {
            const payloadText = await response.text();
            let detail = payloadText || `HTTP ${response.status}`;
            try {
                detail = JSON.parse(payloadText)?.detail || detail;
            } catch (_) {
            }
            throw new Error(_extractApiErrorDetail(detail));
        }

        const payload = await response.json();
        const items = Array.isArray(payload?.items) ? payload.items : [];
        const signature = items
            .map((item) => `${item?.id || ""}:${item?.status || ""}:${item?.updated_at || ""}:${item?.assigned_admin_username || ""}`)
            .join("|");

        if (!force && signature === labRequestsLastSignature) {
            return;
        }

        latestLabRequests = items;
        labRequestsLastSignature = signature;
        renderLabRequestsPanel();
    } catch (error) {
        if (body instanceof HTMLElement) {
            body.innerHTML = `<p class="text-xs text-rose-300">${escapeHtml(error?.message || "Impossible de charger les demandes.")}</p>`;
        }
    } finally {
        labRequestsInFlight = false;
    }
}
