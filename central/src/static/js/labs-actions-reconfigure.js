// labs-actions-reconfigure.js
// Extracted from app.js – reconfigure lab actions: _performReconfigure, showReconfigureProgressModal, showReconfigureModal

async function _performReconfigure(vmId, labName, labKey, routerNames) {
    const mode = await showReconfigureModeModal({ vmId, labName, routerNames });
    if (!mode) {
        return;
    }

    _setLabActionState("reconfig", labKey, { inProgress: true, completedAt: null, startedAt: new Date().toISOString(), jobId: null });
    beginUserActivity({
        id: `reconfigure:${labKey}`,
        title: "Reconfiguration",
        target: `${labName} @ ${vmId}`,
        details: `${mode === "cleanup" ? "Nettoyage configuration" : "Configuration de base"} (${routerNames.length} équipement(s))`,
    });
    renderState(latestStateItems);
    renderActivityRail();

    const requestBody = {};
    if (routerNames.length > 0 && routerNames.length < 100) {
        requestBody.router_names = routerNames;
    }
    requestBody.mode = mode;

    apiFetch(`/api/vms/${encodeURIComponent(vmId)}/labs/${encodeURIComponent(labName)}/reconfigure`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(requestBody),
    })
        .then(async (response) => {
            if (!response.ok) {
                let payloadText = await response.text();
                let payloadDetail = payloadText;
                try {
                    const parsed = JSON.parse(payloadText);
                    const detail = parsed?.detail;
                    payloadDetail = detail ?? payloadText;
                    payloadText = typeof detail === "string" ? detail : payloadText;
                } catch (_) {}
                throw { message: payloadText || `HTTP ${response.status}`, detail: payloadDetail };
            }
            return await response.json();
        })
        .then((data) => {
            const jobId = data?.job_id;
            if (!jobId) {
                throw { message: "Réponse invalide: job_id manquant", detail: null };
            }
            _setLabActionState("reconfig", labKey, {
                inProgress: true,
                completedAt: null,
                startedAt: new Date().toISOString(),
                jobId,
            });
            showReconfigureProgressModal(vmId, labName, jobId, labKey);
        })
        .catch((error) => {
            _setLabActionState("reconfig", labKey, { inProgress: false, completedAt: null, jobId: null });
            endUserActivity(`reconfigure:${labKey}`, "error", error?.message || "Erreur inconnue");
            const feedback = buildReconfigureFeedback(
                vmId,
                labName,
                error?.detail,
                `Reconfiguration échouée: ${error?.message || "Erreur inconnue"}`,
            );
            showReconfigureModal(feedback);
            renderState(latestStateItems);
            renderActivityRail();
        });
}

function showReconfigureProgressModal(vmId, labName, jobId, labKey, options = {}) {
    const startMinimized = Boolean(options?.startMinimized);
    const existing = document.getElementById("reconfigure-modal-overlay");
    if (existing) existing.remove();

    const existingMini = document.getElementById("reconfigure-modal-minimized");
    if (existingMini) existingMini.remove();

    const overlay = document.createElement("div");
    overlay.id = "reconfigure-modal-overlay";
    overlay.className = "fixed inset-0 z-50 bg-black/70 backdrop-blur-sm flex items-center justify-center p-4";

    overlay.innerHTML = `
        <div class="w-full max-w-3xl h-[72vh] rounded-xl border border-amber-700/60 bg-slate-900 shadow-2xl flex flex-col" id="reconfig-modal-inner">
            <div class="flex items-start justify-between gap-4 p-4 border-b border-slate-700">
                <div>
                    <h3 class="text-base font-semibold text-slate-100" id="reconfig-modal-title">Reconfiguration ${escapeHtml(vmId)}/${escapeHtml(labName)}</h3>
                    <p class="text-sm text-amber-300 mt-1" id="reconfig-modal-summary">Démarrage en cours\u2026</p>
                </div>
                <div class="flex items-center gap-2 shrink-0">
                    <button type="button" id="reconfigure-modal-stop" class="text-xs px-2 py-1 rounded border border-rose-700 text-rose-200 hover:bg-rose-900/30">Arreter</button>
                    <button type="button" id="reconfigure-modal-minimize" class="text-xs px-2 py-1 rounded border border-slate-600 hover:bg-slate-800">Minimiser</button>
                    <button type="button" id="reconfigure-modal-close" class="text-xs px-2 py-1 rounded border border-slate-600 hover:bg-slate-800">Fermer</button>
                </div>
            </div>
            <div class="p-4 flex-1 min-h-0 flex flex-col">
                <div id="reconfig-router-list" class="space-y-1 min-h-[2rem] flex-1 overflow-y-auto pr-1">
                    <p class="text-xs text-slate-400 animate-pulse">Connexion à l'agent\u2026</p>
                </div>
                <p class="text-xs text-slate-500 mt-3" id="reconfig-poll-note">Sondage toutes les 10 secondes</p>
            </div>
        </div>
    `;

    document.body.appendChild(overlay);

    let pollTimer = null;
    let finished = false;
    let minimized = false;
    let cancelRequested = false;

    const miniWidget = document.createElement("div");
    miniWidget.id = "reconfigure-modal-minimized";
    miniWidget.className = "hidden rounded-lg border border-amber-700/60 bg-slate-900/95 p-3";
    miniWidget.innerHTML = `
        <p class="text-xs text-slate-300 font-semibold">Reconfiguration ${escapeHtml(vmId)}/${escapeHtml(labName)}</p>
        <p class="text-xs text-amber-300 mt-1" id="reconfig-mini-summary">En cours...</p>
        <div class="mt-2 flex items-center gap-2">
            <button type="button" id="reconfig-mini-restore" class="text-xs px-2 py-1 rounded border border-slate-600 hover:bg-slate-800">Restaurer</button>
            <button type="button" id="reconfig-mini-close" class="text-xs px-2 py-1 rounded border border-slate-600 hover:bg-slate-800">Fermer</button>
        </div>
    `;
    dockMiniWidget(miniWidget);

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
        const current = labReconfigureState.get(labKey) || {};
        if (current.inProgress) {
            _setLabActionState("reconfig", labKey, {
                inProgress: true,
                completedAt: null,
                jobId,
            });
        }
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

    overlay.querySelector("#reconfigure-modal-close").addEventListener("click", closeModal);
    overlay.querySelector("#reconfigure-modal-minimize").addEventListener("click", () => setMinimized(true));
    overlay.addEventListener("click", (e) => { if (e.target === overlay) closeModal(); });
    miniWidget.querySelector("#reconfig-mini-restore").addEventListener("click", () => setMinimized(false));
    miniWidget.querySelector("#reconfig-mini-close").addEventListener("click", closeModal);

    const stopButton = overlay.querySelector("#reconfigure-modal-stop");
    if (stopButton instanceof HTMLButtonElement) {
        stopButton.addEventListener("click", async () => {
            if (cancelRequested || finished) return;
            cancelRequested = true;
            stopButton.disabled = true;
            stopButton.textContent = "Arret...";
            try {
                const resp = await apiFetch(
                    `/api/vms/${encodeURIComponent(vmId)}/labs/${encodeURIComponent(labName)}/reconfigure-job/${encodeURIComponent(jobId)}/cancel`,
                    { method: "POST" },
                );
                if (!resp.ok) {
                    stopButton.disabled = false;
                    stopButton.textContent = "Arreter";
                    cancelRequested = false;
                    return;
                }
                const summaryEl = overlay.querySelector("#reconfig-modal-summary");
                if (summaryEl) summaryEl.textContent = "Arret demande...";
            } catch (_) {
                stopButton.disabled = false;
                stopButton.textContent = "Arreter";
                cancelRequested = false;
            }
        });
    }

    if (startMinimized) {
        setMinimized(true);
    }

    const renderJob = (job) => {
        const titleEl = overlay.querySelector("#reconfig-modal-title");
        const summaryEl = overlay.querySelector("#reconfig-modal-summary");
        const listEl = overlay.querySelector("#reconfig-router-list");
        const noteEl = overlay.querySelector("#reconfig-poll-note");
        const innerEl = overlay.querySelector("#reconfig-modal-inner");

        const total = Number(job.total || 0);
        const completedCount = Number(job.completed_count || 0);
        const successCount = Number(job.success_count || 0);
        const results = Array.isArray(job.results) ? job.results : [];
        const status = String(job.status || "").toLowerCase();
        const statusDone = status === "completed" || status === "error" || status === "cancelled" || status === "canceled";
        const statusCancelling = status === "cancelling";
        const miniSummaryEl = miniWidget.querySelector("#reconfig-mini-summary");

        // Build router rows
        const rowsHtml = results.map((r) => {
            if (r.ok) {
                return `<div class="flex items-center gap-2 text-xs py-0.5">
                    <span class="text-emerald-400 font-mono w-3">\u2713</span>
                    <span class="text-slate-200 font-mono">${escapeHtml(r.node || "?")}</span>
                    <span class="text-emerald-400">OK</span>
                </div>`;
            }
            return `<div class="flex items-start gap-2 text-xs py-0.5">
                <span class="text-rose-400 font-mono w-3 mt-px">\u2717</span>
                <span class="text-slate-200 font-mono shrink-0">${escapeHtml(r.node || "?")}</span>
                <span class="text-rose-300 break-all">${escapeHtml(r.error || "Erreur")}</span>
            </div>`;
        }).join("");

        listEl.innerHTML = rowsHtml || `<p class="text-xs text-slate-400 animate-pulse">En attente de résultats\u2026</p>`;

        if (statusDone) {
            cleanup();
            const cancelled = status === "cancelled" || status === "canceled";
            const allOk = !cancelled && job.ok === true;
            const innerBorder = allOk ? "border-emerald-700/60" : "border-rose-700/60";
            innerEl.className = innerEl.className.replace(/border-\S+/g, innerBorder);
            titleEl.textContent = `Reconfiguration ${vmId}/${labName}`;
            summaryEl.className = `text-sm mt-1 ${allOk ? "text-emerald-300" : "text-rose-300"}`;
            summaryEl.textContent = cancelled
                ? "Operation annulee"
                : job.error
                ? `Erreur: ${job.error}`
                : `${allOk ? "\u2713 Succès" : "\u2717 Partiel"}: ${successCount}/${total} équipement(s) reconfiguré(s).`;
            noteEl.textContent = `Terminé le ${new Date().toLocaleTimeString("fr-FR")}`;
            if (miniSummaryEl) {
                miniSummaryEl.textContent = cancelled
                    ? "Annule"
                    : job.error
                    ? `Erreur: ${job.error}`
                    : `${allOk ? "Terminé" : "Terminé (partiel)"}: ${successCount}/${total}`;
            }

            // Update global state for the lab card badge
            const _rcAt = new Date().toISOString();
            _setLabActionState("reconfig", labKey, { inProgress: false, completedAt: _rcAt, jobId: null });
            _saveCompletedAt('reconfig', labKey, _rcAt);
            endUserActivity(
                `reconfigure:${labKey}`,
                cancelled ? "warning" : (allOk ? "ok" : "error"),
                cancelled ? "Operation annulee" : (job.error ? String(job.error) : `${successCount}/${total} équipement(s) reconfiguré(s)`),
            );
            // Re-enable the reconfigure button
            const buttons = document.querySelectorAll("button[data-action='reconfigure-lab']");
            for (const btn of buttons) {
                if (btn.getAttribute("data-vm-id") === vmId && btn.getAttribute("data-lab-name") === labName) {
                    btn.disabled = false;
                    btn.textContent = "Reconfiguration";
                    break;
                }
            }
            refreshState();
            renderActivityRail();
        } else if (statusCancelling) {
            summaryEl.textContent = "Arret en cours...";
            if (miniSummaryEl) {
                miniSummaryEl.textContent = "Arret en cours...";
            }
        } else {
            summaryEl.textContent = total
                ? `En cours\u2026 ${completedCount}/${total} routeur(s) traité(s)`
                : "En cours\u2026";
            if (miniSummaryEl) {
                miniSummaryEl.textContent = total
                    ? `${completedCount}/${total} routeur(s) traité(s)`
                    : "En cours...";
            }
        }
    };

    const poll = async () => {
        if (finished) return;
        try {
            const resp = await apiFetch(
                `/api/vms/${encodeURIComponent(vmId)}/labs/${encodeURIComponent(labName)}/reconfigure-job/${encodeURIComponent(jobId)}`
            );
            if (!resp.ok) return;
            const job = await resp.json();
            if (job && !finished) renderJob(job);
        } catch (_) {
            // network error – will retry on next interval
        }
    };

    // First poll after 2s (gives agent time to populate router list)
    setTimeout(poll, 2000);
    pollTimer = setInterval(poll, 10000);
}



function showReconfigureModal({ title, summary, detailLines = [], isError = false }) {
    const existing = document.getElementById("reconfigure-modal-overlay");
    if (existing) {
        existing.remove();
    }

    const overlay = document.createElement("div");
    overlay.id = "reconfigure-modal-overlay";
    overlay.className = "fixed inset-0 z-50 bg-black/70 backdrop-blur-sm flex items-center justify-center p-4";

    const tone = isError
        ? "border-rose-700/60 text-rose-200"
        : "border-emerald-700/60 text-emerald-200";

    const detailsHtml = detailLines.length
        ? `<div class=\"mt-3 max-h-64 overflow-auto rounded border border-slate-700 bg-slate-950/60 p-3\">${detailLines
            .map((line) => `<p class=\"text-xs text-slate-300 leading-5\">${escapeHtml(line)}</p>`)
            .join("")}</div>`
        : "";

    overlay.innerHTML = `
        <div class=\"w-full max-w-2xl rounded-xl border ${tone} bg-slate-900 shadow-2xl\">
            <div class=\"flex items-start justify-between gap-4 p-4 border-b border-slate-700\">
                <div>
                    <h3 class=\"text-base font-semibold text-slate-100\">${escapeHtml(title)}</h3>
                    <p class=\"text-sm text-slate-300 mt-1\">${escapeHtml(summary)}</p>
                </div>
                <button type=\"button\" id=\"reconfigure-modal-close\" class=\"text-xs px-2 py-1 rounded border border-slate-600 hover:bg-slate-800\">Fermer</button>
            </div>
            <div class=\"p-4\">${detailsHtml}</div>
        </div>
    `;

    document.body.appendChild(overlay);

    const close = () => overlay.remove();
    const closeButton = document.getElementById("reconfigure-modal-close");
    if (closeButton) {
        closeButton.addEventListener("click", close);
    }

    overlay.addEventListener("click", (event) => {
        if (event.target === overlay) {
            close();
        }
    });
}


function showReconfigureModeModal({ vmId, labName, routerNames }) {
    return new Promise((resolve) => {
        const existing = document.getElementById("reconfigure-mode-modal-overlay");
        if (existing) {
            existing.remove();
        }

        const overlay = document.createElement("div");
        overlay.id = "reconfigure-mode-modal-overlay";
        overlay.className = "fixed inset-0 z-50 bg-black/70 backdrop-blur-sm flex items-center justify-center p-4";

        const targetLabel = routerNames.length === 1
            ? `l'équipement ${routerNames[0]}`
            : `${routerNames.length} équipements`;

        overlay.innerHTML = `
            <div class="w-full max-w-2xl rounded-xl border border-slate-700 bg-slate-900 shadow-2xl">
                <div class="flex items-start justify-between gap-4 p-4 border-b border-slate-700">
                    <div>
                        <h3 class="text-base font-semibold text-slate-100">Reconfigurer ${escapeHtml(vmId)}/${escapeHtml(labName)}</h3>
                        <p class="text-sm text-slate-300 mt-1">Choisir le mode pour ${escapeHtml(targetLabel)}.</p>
                    </div>
                    <button type="button" id="reconfigure-mode-close" class="text-xs px-2 py-1 rounded border border-slate-600 hover:bg-slate-800">Annuler</button>
                </div>
                <div class="p-4 grid gap-3 md:grid-cols-2">
                    <button type="button" data-mode="cleanup" class="text-left rounded-xl border border-amber-700/60 bg-amber-950/20 hover:bg-amber-900/30 p-4">
                        <span class="block text-sm font-semibold text-amber-200">Nettoyage configuration</span>
                        <span class="block text-xs text-slate-300 mt-2">Fait un diff entre la configuration courante et la config_db, supprime les lignes en trop puis applique la configuration de base.</span>
                    </button>
                    <button type="button" data-mode="base" class="text-left rounded-xl border border-fuchsia-700/60 bg-fuchsia-950/20 hover:bg-fuchsia-900/30 p-4">
                        <span class="block text-sm font-semibold text-fuchsia-200">Configuration de base</span>
                        <span class="block text-xs text-slate-300 mt-2">Applique simplement la configuration de base sans nettoyage préalable. Adapté aux nouveaux déploiements.</span>
                    </button>
                </div>
            </div>
        `;

        document.body.appendChild(overlay);

        const close = (value = null) => {
            overlay.remove();
            resolve(value);
        };

        const closeButton = overlay.querySelector("#reconfigure-mode-close");
        if (closeButton) {
            closeButton.addEventListener("click", () => close(null));
        }

        for (const button of overlay.querySelectorAll("button[data-mode]")) {
            button.addEventListener("click", () => close(button.getAttribute("data-mode") || "base"));
        }

        overlay.addEventListener("click", (event) => {
            if (event.target === overlay) {
                close(null);
            }
        });
    });
}

// Expose explicitly for cross-file handlers loaded in separate script tags.
window._performReconfigure = _performReconfigure;
window.showReconfigureProgressModal = showReconfigureProgressModal;
window.showReconfigureModal = showReconfigureModal;
window.showReconfigureModeModal = showReconfigureModeModal;
