function _buildRouterDiffBlock(routerResult) {
    const node = String(routerResult?.node || "?");
    if (!routerResult?.ok) {
        return `
            <article class="rounded-lg border border-rose-700/50 bg-rose-950/20 p-3">
                <p class="text-sm font-semibold text-rose-200">${escapeHtml(node)}</p>
                <p class="text-xs text-rose-300 mt-1">${escapeHtml(String(routerResult?.error || "Erreur inconnue"))}</p>
            </article>
        `;
    }

    const diff = routerResult?.diff || {};
    const missing = Array.isArray(diff.missing_from_running) ? diff.missing_from_running : [];
    const extra = Array.isArray(diff.extra_in_running) ? diff.extra_in_running : [];
    const deleteCommands = Array.isArray(diff.delete_commands) ? diff.delete_commands : [];
    const commandSections = [];
    if (missing.length) {
        commandSections.push("# Commandes d'ajout", ...missing);
    }
    if (deleteCommands.length) {
        if (commandSections.length) commandSections.push("");
        commandSections.push("# Commandes de suppression", ...deleteCommands);
    }

    return `
        <article class="rounded-lg border border-slate-700 bg-slate-900/50 p-3 space-y-3">
            <div class="flex items-center justify-between gap-2">
                <h4 class="text-sm font-semibold text-slate-100">${escapeHtml(node)}</h4>
                <span class="text-[11px] px-2 py-0.5 rounded ${diff.match ? "bg-emerald-900/40 text-emerald-300 border border-emerald-700/50" : "bg-amber-900/40 text-amber-300 border border-amber-700/50"}">
                    ${diff.match ? "Conforme" : "Différences détectées"}
                </span>
            </div>
            <div class="grid grid-cols-1 xl:grid-cols-2 gap-3">
                <section class="space-y-1">
                    <p class="text-xs font-medium text-slate-300">Lignes manquantes dans running</p>
                    <pre class="text-[11px] leading-4 whitespace-pre overflow-auto max-h-56 border border-slate-800 rounded bg-slate-950/70 p-2">${escapeHtml(missing.join("\n") || "Aucune")}</pre>
                </section>
                <section class="space-y-1">
                    <p class="text-xs font-medium text-slate-300">Lignes en trop dans running</p>
                    <pre class="text-[11px] leading-4 whitespace-pre overflow-auto max-h-56 border border-slate-800 rounded bg-slate-950/70 p-2">${escapeHtml(extra.join("\n") || "Aucune")}</pre>
                </section>
            </div>
            <section class="space-y-1">
                <p class="text-xs font-medium text-slate-300">Commandes correctives</p>
                <pre class="text-[11px] leading-4 whitespace-pre overflow-auto max-h-40 border border-slate-800 rounded bg-slate-950/70 p-2">${escapeHtml(commandSections.join("\n") || "Aucune commande corrective")}</pre>
            </section>
        </article>
    `;
}

const _CONFIG_DIFF_LAST_PREFIX = "vlm:configDiff:last:";

function _configDiffLastStorageKey(labKey) {
    return `${_CONFIG_DIFF_LAST_PREFIX}${String(labKey || "")}`;
}

function _saveLastConfigDiffResult(labKey, vmId, labName, routerNames, payload) {
    try {
        const key = _configDiffLastStorageKey(labKey);
        const normalizedRouters = Array.isArray(routerNames)
            ? routerNames.map((name) => String(name || "")).filter(Boolean)
            : [];
        const snapshot = {
            saved_at: new Date().toISOString(),
            vm_id: String(vmId || ""),
            lab_name: String(labName || ""),
            router_names: normalizedRouters,
            payload: payload || null,
        };
        localStorage.setItem(key, JSON.stringify(snapshot));
    } catch (_) {
        // Ignore localStorage failures.
    }
}

function _loadLastConfigDiffResult(labKey) {
    try {
        const raw = localStorage.getItem(_configDiffLastStorageKey(labKey));
        if (!raw) return null;
        const parsed = JSON.parse(raw);
        if (!parsed || typeof parsed !== "object") {
            return null;
        }
        return parsed;
    } catch (_) {
        return null;
    }
}

function showConfigDiffModal(vmId, labName, payload) {
    const root = payload?.result || payload || {};
    const routerResults = Array.isArray(root.results)
        ? root.results
        : (Array.isArray(root.router_results) ? root.router_results : []);
    showOverlayModal({
        title: `Diff config ${labName}`,
        tone: "amber",
        widthClass: "max-w-6xl",
        bodyHtml: `
            <div class="space-y-4">
                <p class="text-xs text-slate-400">VM ${escapeHtml(vmId)} · ${routerResults.length} équipement(s)</p>
                <div class="space-y-3 max-h-[75vh] overflow-auto pr-1">
                    ${routerResults.map((routerResult) => _buildRouterDiffBlock(routerResult)).join("") || '<p class="text-sm text-slate-400">Aucun résultat.</p>'}
                </div>
            </div>
        `,
    });
}

function showConfigDiffProgressModal(vmId, labName, jobId, labKey, options = {}) {
    const startMinimized = Boolean(options?.startMinimized);
    const expectedTotal = Number.isFinite(Number(options?.expectedTotal))
        ? Math.max(0, Number(options.expectedTotal))
        : 0;
    const existing = document.getElementById("config-diff-modal-overlay");
    if (existing) existing.remove();

    const existingMini = document.getElementById("config-diff-modal-minimized");
    if (existingMini) existingMini.remove();

    const overlay = document.createElement("div");
    overlay.id = "config-diff-modal-overlay";
    overlay.className = "fixed inset-0 z-50 bg-black/70 backdrop-blur-sm flex items-center justify-center p-4";
    overlay.innerHTML = `
        <div class="w-full max-w-3xl h-[60vh] rounded-xl border border-sky-700/60 bg-slate-900 shadow-2xl flex flex-col" id="config-diff-modal-inner">
            <div class="flex items-start justify-between gap-4 p-4 border-b border-slate-700">
                <div>
                    <h3 class="text-base font-semibold text-slate-100">Diff config ${escapeHtml(vmId)}/${escapeHtml(labName)}</h3>
                    <p class="text-sm text-sky-300 mt-1" id="config-diff-modal-summary">Comparaison en cours...</p>
                </div>
                <div class="flex items-center gap-2 shrink-0">
                    <button type="button" id="config-diff-modal-minimize" class="text-xs px-2 py-1 rounded border border-slate-600 hover:bg-slate-800">Minimiser</button>
                    <button type="button" id="config-diff-modal-close" class="text-xs px-2 py-1 rounded border border-slate-600 hover:bg-slate-800">Fermer</button>
                </div>
            </div>
            <div class="p-4 flex-1 min-h-0 flex flex-col">
                <div id="config-diff-job-list" class="space-y-1 min-h-[2rem] flex-1 overflow-y-auto pr-1 text-xs text-slate-300">
                    <p class="text-xs text-slate-400 animate-pulse">Connexion a l'agent...</p>
                </div>
                <p class="text-xs text-slate-500 mt-3" id="config-diff-poll-note">Sondage toutes les 5 secondes</p>
            </div>
        </div>
    `;
    document.body.appendChild(overlay);

    const miniWidget = document.createElement("div");
    miniWidget.id = "config-diff-modal-minimized";
    miniWidget.className = "hidden rounded-lg border border-sky-700/60 bg-slate-900/95 p-3";
    miniWidget.innerHTML = `
        <p class="text-xs text-slate-300 font-semibold">Diff ${escapeHtml(vmId)}/${escapeHtml(labName)}</p>
        <p class="text-xs text-sky-300 mt-1" id="config-diff-mini-summary">En cours...</p>
        <div class="mt-2 flex items-center gap-2">
            <button type="button" id="config-diff-mini-restore" class="text-xs px-2 py-1 rounded border border-slate-600 hover:bg-slate-800">Restaurer</button>
            <button type="button" id="config-diff-mini-close" class="text-xs px-2 py-1 rounded border border-slate-600 hover:bg-slate-800">Fermer</button>
        </div>
    `;
    dockMiniWidget(miniWidget);

    let finished = false;
    let pollTimer = null;
    let minimized = false;

    const cleanup = () => {
        finished = true;
        if (pollTimer !== null) {
            clearInterval(pollTimer);
            pollTimer = null;
        }
        miniWidget.remove();
        _updateActivityDockCount();
    };

    const closeModal = () => {
        cleanup();
        overlay.remove();
    };

    const setMinimized = (nextState) => {
        minimized = Boolean(nextState);
        overlay.style.display = minimized ? "none" : "flex";
        miniWidget.classList.toggle("hidden", !minimized);
        if (minimized) {
            setActivityRailOpen(true);
        }
        _updateActivityDockCount();
    };

    overlay.querySelector("#config-diff-modal-close").addEventListener("click", closeModal);
    overlay.querySelector("#config-diff-modal-minimize").addEventListener("click", () => setMinimized(true));
    overlay.addEventListener("click", (event) => {
        if (event.target === overlay) closeModal();
    });
    miniWidget.querySelector("#config-diff-mini-restore").addEventListener("click", () => setMinimized(false));
    miniWidget.querySelector("#config-diff-mini-close").addEventListener("click", closeModal);

    if (startMinimized) {
        setMinimized(true);
    }

    const renderJob = (job) => {
        const summaryEl = overlay.querySelector("#config-diff-modal-summary");
        const listEl = overlay.querySelector("#config-diff-job-list");
        const noteEl = overlay.querySelector("#config-diff-poll-note");
        const miniSummaryEl = miniWidget.querySelector("#config-diff-mini-summary");

        const status = String(job?.status || "").toLowerCase();
        const phase = String(job?.phase || "").toLowerCase();
        const currentNode = String(job?.current_node || "").trim();
        const total = Number(job?.total || 0);
        const completed = Number(job?.completed_count || 0);
        const success = Number(job?.success_count || 0);
        const targetCount = Array.isArray(job?.router_names) ? job.router_names.length : 0;
        const displayTargetCount = targetCount > 0 ? targetCount : expectedTotal;
        const hasKnownTotal = Number.isFinite(total) && total > 0;
        const isTerminal = status === "completed" || status === "error" || status === "cancelled" || status === "canceled";

        const progressText = hasKnownTotal
            ? `${completed}/${total}`
            : (displayTargetCount > 0 ? `Preparation (0/${displayTargetCount})` : "Initialisation...");
        const successText = hasKnownTotal
            ? String(success)
            : "-";

        listEl.innerHTML = `
            <p>Statut: <span class="text-slate-100">${escapeHtml(status || "running")}</span></p>
            ${phase ? `<p>Phase: <span class="text-slate-100">${escapeHtml(phase)}</span></p>` : ""}
            ${currentNode ? `<p>Noeud en cours: <span class="text-slate-100">${escapeHtml(currentNode)}</span></p>` : ""}
            <p>Progression: <span class="text-slate-100">${escapeHtml(progressText)}</span></p>
            <p>Succes: <span class="text-slate-100">${escapeHtml(successText)}</span></p>
            ${!hasKnownTotal && displayTargetCount > 0 ? `<p>Cibles: <span class="text-slate-100">${displayTargetCount} equipement(s)</span></p>` : ""}
        `;

        if (isTerminal) {
            cleanup();
            const completedAt = new Date().toISOString();
            _setLabActionState("configdiff", labKey, {
                inProgress: false,
                completedAt,
                startedAt: null,
                jobId: null,
            });
            _saveCompletedAt("configdiff", labKey, completedAt);

            if (status === "completed") {
                summaryEl.textContent = `Termine: ${success}/${total}`;
                if (miniSummaryEl) miniSummaryEl.textContent = `Termine: ${success}/${total}`;
                endUserActivity(`config-diff:${labKey}`, "ok", `Diff termine: ${success}/${total}`);
                if (job?.result) {
                    _saveLastConfigDiffResult(labKey, vmId, labName, Array.isArray(job?.router_names) ? job.router_names : [], job.result);
                    overlay.remove();
                    miniWidget.remove();
                    showConfigDiffModal(vmId, labName, job.result);
                    return;
                }
            } else if (status === "cancelled" || status === "canceled") {
                summaryEl.textContent = "Operation annulee";
                if (miniSummaryEl) miniSummaryEl.textContent = "Annule";
                endUserActivity(`config-diff:${labKey}`, "warning", "Operation annulee");
            } else {
                const err = String(job?.error || "Erreur de comparaison");
                summaryEl.textContent = `Erreur: ${err}`;
                if (miniSummaryEl) miniSummaryEl.textContent = `Erreur`;
                endUserActivity(`config-diff:${labKey}`, "error", err);
                showToastMessage("Diff impossible", err, true);
            }

            noteEl.textContent = `Termine le ${new Date().toLocaleTimeString("fr-FR")}`;
            renderState(latestStateItems);
            renderActivityRail();
            return;
        }

        summaryEl.textContent = total
            ? `En cours... ${completed}/${total}`
            : (displayTargetCount > 0 ? `Preparation des comparaisons (0/${displayTargetCount})` : "Initialisation...");
        if (miniSummaryEl) {
            miniSummaryEl.textContent = total
                ? `${completed}/${total}`
                : (displayTargetCount > 0 ? `Prep 0/${displayTargetCount}` : "Initialisation...");
        }
    };

    const poll = async () => {
        if (finished) return;
        try {
            const response = await apiFetch(`/api/vms/${encodeURIComponent(vmId)}/labs/${encodeURIComponent(labName)}/config-diff-job/${encodeURIComponent(jobId)}`);
            if (!response.ok) return;
            const job = await response.json();
            if (job && !finished) renderJob(job);
        } catch (_) {
            // Retry on next interval
        }
    };

    void poll();
    pollTimer = setInterval(() => { void poll(); }, 5000);
}

async function fetchAndShowConfigDiff(vmId, labName, routerNames) {
    const selectedCount = Array.isArray(routerNames) ? routerNames.length : 0;
    const labKey = findLabKey(vmId, labName);

    _setLabActionState("configdiff", labKey, {
        inProgress: true,
        completedAt: null,
        startedAt: new Date().toISOString(),
        jobId: null,
    });
    beginUserActivity({
        id: `config-diff:${labKey}`,
        title: "Diff configuration",
        target: `${labName} @ ${vmId}`,
        details: `Initialisation (${selectedCount} equipement(s))`,
    });
    renderState(latestStateItems);
    renderActivityRail();

    try {
        const response = await apiFetch(`/api/vms/${encodeURIComponent(vmId)}/labs/${encodeURIComponent(labName)}/config-diff-async`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                router_names: Array.isArray(routerNames) && routerNames.length ? routerNames : null,
            }),
        });
        if (!response.ok) {
            const payloadText = await response.text();
            let detail = payloadText || `HTTP ${response.status}`;
            try {
                detail = _extractApiErrorDetail(JSON.parse(payloadText)?.detail || detail);
            } catch (_) {
            }
            throw new Error(detail);
        }

        const payload = await response.json();
        const jobId = payload?.job_id;
        if (!jobId) {
            throw new Error("Reponse invalide: job_id manquant");
        }

        _setLabActionState("configdiff", labKey, {
            inProgress: true,
            completedAt: null,
            startedAt: new Date().toISOString(),
            jobId,
        });
        showConfigDiffProgressModal(vmId, labName, jobId, labKey, { expectedTotal: selectedCount });
    } catch (error) {
        _setLabActionState("configdiff", labKey, { inProgress: false, completedAt: null, startedAt: null, jobId: null });
        endUserActivity(`config-diff:${labKey}`, "error", error?.message || "Erreur inconnue");
        showToastMessage("Diff impossible", error?.message || "Erreur inconnue", true);
        renderState(latestStateItems);
        renderActivityRail();
    }
}

function showConfigDiffSelectModal(vmId, labName, routers) {
    const routerNames = (Array.isArray(routers) ? routers : [])
        .map((router) => {
            const raw = typeof router === "string" ? router : String(router?.name || "");
            const prefix = `clab-${labName}-`;
            return raw.startsWith(prefix) ? raw.slice(prefix.length) : raw;
        })
        .filter(Boolean);
    const labKey = findLabKey(vmId, labName);
    const lastSnapshot = _loadLastConfigDiffResult(labKey);
    const lastSavedAt = lastSnapshot?.saved_at ? new Date(String(lastSnapshot.saved_at)) : null;
    const hasLastDiffPayload = Boolean(lastSnapshot?.payload);
    const lastRouters = Array.isArray(lastSnapshot?.router_names)
        ? lastSnapshot.router_names.map((name) => String(name || "")).filter(Boolean)
        : [];
    const hasReusableSelection = lastRouters.length > 0;
    const lastSavedLabel = lastSavedAt && !Number.isNaN(lastSavedAt.getTime())
        ? lastSavedAt.toLocaleString("fr-FR")
        : "Date inconnue";

    const overlay = showOverlayModal({
        title: `Sélection diff ${labName}`,
        tone: "amber",
        widthClass: "max-w-2xl",
        bodyHtml: `
            <div class="space-y-4">
                <p class="text-sm text-slate-300">Sélectionnez les équipements à comparer avec la base de configuration.</p>
                ${hasLastDiffPayload ? `
                    <div class="rounded-lg border border-sky-700/50 bg-sky-950/20 p-3 space-y-2">
                        <p class="text-xs text-sky-200">Dernier Diff disponible (${escapeHtml(lastSavedLabel)})</p>
                        <div class="flex items-center gap-2 flex-wrap">
                            <button type="button" id="diff-open-last" class="text-xs px-3 py-2 rounded border border-sky-700 text-sky-200 hover:bg-sky-900/30">Voir le dernier Diff</button>
                            <button type="button" id="diff-rerun-last-selection" class="text-xs px-3 py-2 rounded border border-cyan-700 text-cyan-200 hover:bg-cyan-900/30" ${hasReusableSelection ? "" : "disabled"}>Relancer avec la même sélection</button>
                        </div>
                    </div>
                ` : ""}
                <div class="flex items-center gap-2">
                    <button type="button" id="select-all-diff" class="text-xs px-3 py-2 rounded border border-slate-600 hover:bg-slate-800">Tout sélectionner</button>
                    <button type="button" id="deselect-all-diff" class="text-xs px-3 py-2 rounded border border-slate-600 hover:bg-slate-800">Tout désélectionner</button>
                </div>
                <div class="max-h-80 overflow-auto rounded-lg border border-slate-700 bg-slate-950/50 p-3 space-y-2">
                    ${routerNames.map((name) => `
                        <label class="flex items-center gap-2 text-sm text-slate-200">
                            <input type="checkbox" class="router-diff-select accent-amber-500" value="${escapeHtml(name)}" checked />
                            <span>${escapeHtml(name)}</span>
                        </label>
                    `).join("") || '<p class="text-xs text-slate-500">Aucun équipement trouvé.</p>'}
                </div>
                <div class="flex items-center justify-end gap-2">
                    <button type="button" data-modal-close="true" class="text-xs px-3 py-2 rounded border border-slate-600 hover:bg-slate-800">Annuler</button>
                    <button type="button" id="diff-confirm" class="text-xs px-3 py-2 rounded border border-amber-700 text-amber-200 hover:bg-amber-900/30">Comparer</button>
                </div>
            </div>
        `,
    });

    const selectAllBtn = overlay?.querySelector("#select-all-diff");
    const deselectAllBtn = overlay?.querySelector("#deselect-all-diff");
    const confirmBtn = overlay?.querySelector("#diff-confirm");
    const openLastBtn = overlay?.querySelector("#diff-open-last");
    const rerunLastSelectionBtn = overlay?.querySelector("#diff-rerun-last-selection");

    if (selectAllBtn instanceof HTMLButtonElement) {
        selectAllBtn.addEventListener("click", () => {
            overlay.querySelectorAll(".router-diff-select").forEach((checkbox) => {
                if (checkbox instanceof HTMLInputElement) checkbox.checked = true;
            });
        });
    }
    if (deselectAllBtn instanceof HTMLButtonElement) {
        deselectAllBtn.addEventListener("click", () => {
            overlay.querySelectorAll(".router-diff-select").forEach((checkbox) => {
                if (checkbox instanceof HTMLInputElement) checkbox.checked = false;
            });
        });
    }
    if (confirmBtn instanceof HTMLButtonElement) {
        confirmBtn.addEventListener("click", () => {
            const selectedRouters = Array.from(overlay.querySelectorAll(".router-diff-select:checked"))
                .map((checkbox) => checkbox instanceof HTMLInputElement ? checkbox.value : "")
                .filter(Boolean);
            if (selectedRouters.length === 0) {
                showToastMessage("Erreur", "Veuillez sélectionner au moins un équipement.", true);
                return;
            }
            closeOverlayModal();
            fetchAndShowConfigDiff(vmId, labName, selectedRouters);
        });
    }
    if (openLastBtn instanceof HTMLButtonElement) {
        openLastBtn.addEventListener("click", () => {
            if (!lastSnapshot?.payload) {
                showToastMessage("Information", "Aucun dernier Diff disponible.", true);
                return;
            }
            closeOverlayModal();
            showConfigDiffModal(vmId, labName, lastSnapshot.payload);
        });
    }
    if (rerunLastSelectionBtn instanceof HTMLButtonElement) {
        rerunLastSelectionBtn.addEventListener("click", () => {
            if (!hasReusableSelection) {
                showToastMessage("Information", "Aucune sélection précédente disponible.", true);
                return;
            }
            closeOverlayModal();
            fetchAndShowConfigDiff(vmId, labName, lastRouters);
        });
    }
}

// Expose explicitly for cross-file handlers loaded in separate script tags.
window.showConfigDiffModal = showConfigDiffModal;
window.showConfigDiffProgressModal = showConfigDiffProgressModal;
window.fetchAndShowConfigDiff = fetchAndShowConfigDiff;
window.showConfigDiffSelectModal = showConfigDiffSelectModal;
