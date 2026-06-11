// labs-actions-set-default.js
// Extracted from app.js – set-default lab action: showSetDefaultProgressModal

function showSetDefaultProgressModal(vmId, labName, jobId, labKey, options = {}) {
    const startMinimized = Boolean(options?.startMinimized);
    const existing = document.getElementById("set-default-modal-overlay");
    if (existing) existing.remove();

    const existingMini = document.getElementById("set-default-modal-minimized");
    if (existingMini) existingMini.remove();

    const overlay = document.createElement("div");
    overlay.id = "set-default-modal-overlay";
    overlay.className = "fixed inset-0 z-50 bg-black/70 backdrop-blur-sm flex items-center justify-center p-4";

    overlay.innerHTML = `
        <div class="w-full max-w-3xl h-[72vh] rounded-xl border border-emerald-700/60 bg-slate-900 shadow-2xl flex flex-col" id="set-default-modal-inner">
            <div class="flex items-start justify-between gap-4 p-4 border-b border-slate-700">
                <div>
                    <h3 class="text-base font-semibold text-slate-100" id="set-default-modal-title">Base par défaut ${escapeHtml(vmId)}/${escapeHtml(labName)}</h3>
                    <p class="text-sm text-emerald-300 mt-1" id="set-default-modal-summary">Capture en cours…</p>
                </div>
                <div class="flex items-center gap-2 shrink-0">
                    <button type="button" id="set-default-modal-stop" class="text-xs px-2 py-1 rounded border border-rose-700 text-rose-200 hover:bg-rose-900/30">Arreter</button>
                    <button type="button" id="set-default-modal-minimize" class="text-xs px-2 py-1 rounded border border-slate-600 hover:bg-slate-800">Minimiser</button>
                    <button type="button" id="set-default-modal-close" class="text-xs px-2 py-1 rounded border border-slate-600 hover:bg-slate-800">Fermer</button>
                </div>
            </div>
            <div class="p-4 flex-1 min-h-0 flex flex-col">
                <div id="set-default-router-list" class="space-y-1 min-h-[2rem] flex-1 overflow-y-auto pr-1">
                    <p class="text-xs text-slate-400 animate-pulse">Connexion à l'agent…</p>
                </div>
                <p class="text-xs text-slate-500 mt-3" id="set-default-poll-note">Sondage toutes les 10 secondes</p>
            </div>
        </div>
    `;

    document.body.appendChild(overlay);

    let pollTimer = null;
    let finished = false;
    let minimized = false;
    let cancelRequested = false;

    const miniWidget = document.createElement("div");
    miniWidget.id = "set-default-modal-minimized";
    miniWidget.className = "hidden rounded-lg border border-emerald-700/60 bg-slate-900/95 p-3";
    miniWidget.innerHTML = `
        <p class="text-xs text-slate-300 font-semibold">Base par défaut ${escapeHtml(vmId)}/${escapeHtml(labName)}</p>
        <p class="text-xs text-emerald-300 mt-1" id="set-default-mini-summary">En cours...</p>
        <div class="mt-2 flex items-center gap-2">
            <button type="button" id="set-default-mini-restore" class="text-xs px-2 py-1 rounded border border-slate-600 hover:bg-slate-800">Restaurer</button>
            <button type="button" id="set-default-mini-close" class="text-xs px-2 py-1 rounded border border-slate-600 hover:bg-slate-800">Fermer</button>
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
        const current = labSetDefaultState.get(labKey) || {};
        if (current.inProgress) {
            _setLabActionState("setdefault", labKey, {
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

    overlay.querySelector("#set-default-modal-close").addEventListener("click", closeModal);
    overlay.querySelector("#set-default-modal-minimize").addEventListener("click", () => setMinimized(true));
    overlay.addEventListener("click", (e) => { if (e.target === overlay) closeModal(); });
    miniWidget.querySelector("#set-default-mini-restore").addEventListener("click", () => setMinimized(false));
    miniWidget.querySelector("#set-default-mini-close").addEventListener("click", closeModal);

    const stopButton = overlay.querySelector("#set-default-modal-stop");
    if (stopButton instanceof HTMLButtonElement) {
        stopButton.addEventListener("click", async () => {
            if (cancelRequested || finished) return;
            cancelRequested = true;
            stopButton.disabled = true;
            stopButton.textContent = "Arret...";
            try {
                const resp = await apiFetch(
                    `/api/vms/${encodeURIComponent(vmId)}/labs/${encodeURIComponent(labName)}/set-default-job/${encodeURIComponent(jobId)}/cancel`,
                    { method: "POST" },
                );
                if (!resp.ok) {
                    stopButton.disabled = false;
                    stopButton.textContent = "Arreter";
                    cancelRequested = false;
                    return;
                }
                const summaryEl = overlay.querySelector("#set-default-modal-summary");
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
        const titleEl = overlay.querySelector("#set-default-modal-title");
        const summaryEl = overlay.querySelector("#set-default-modal-summary");
        const listEl = overlay.querySelector("#set-default-router-list");
        const noteEl = overlay.querySelector("#set-default-poll-note");
        const innerEl = overlay.querySelector("#set-default-modal-inner");

        const total = Number(job.total || 0);
        const completedCount = Number(job.completed_count || 0);
        const savedCount = Number(job.saved_count || 0);
        const results = Array.isArray(job.results) ? job.results : [];
        const status = String(job.status || "").toLowerCase();
        const statusDone = status === "completed" || status === "error" || status === "cancelled" || status === "canceled";
        const statusCancelling = status === "cancelling";
        const miniSummaryEl = miniWidget.querySelector("#set-default-mini-summary");

        const rowsHtml = results.map((r) => {
            if (r.ok) {
                return `<div class="flex items-center gap-2 text-xs py-0.5">
                    <span class="text-emerald-400 font-mono w-3">✓</span>
                    <span class="text-slate-200 font-mono">${escapeHtml(r.node || "?")}</span>
                    <span class="text-emerald-400">Capturé</span>
                </div>`;
            }
            return `<div class="flex items-start gap-2 text-xs py-0.5">
                <span class="text-rose-400 font-mono w-3 mt-px">✗</span>
                <span class="text-slate-200 font-mono shrink-0">${escapeHtml(r.node || "?")}</span>
                <span class="text-rose-300 break-all">${escapeHtml(r.error || "Erreur")}</span>
            </div>`;
        }).join("");

        listEl.innerHTML = rowsHtml || `<p class="text-xs text-slate-400 animate-pulse">En attente de résultats…</p>`;

        if (statusDone) {
            cleanup();
            const cancelled = status === "cancelled" || status === "canceled";
            const allOk = !cancelled && job.ok === true;
            const innerBorder = allOk ? "border-emerald-700/60" : "border-rose-700/60";
            innerEl.className = innerEl.className.replace(/border-\S+/g, innerBorder);
            titleEl.textContent = `Base par défaut ${vmId}/${labName}`;
            summaryEl.className = `text-sm mt-1 ${allOk ? "text-emerald-300" : "text-rose-300"}`;
            if (cancelled) {
                summaryEl.textContent = "Operation annulee";
            } else if (job.error) {
                summaryEl.textContent = `Erreur: ${job.error}`;
            } else {
                const backup = job.backup_file ? ` Backup: ${job.backup_file}` : "";
                summaryEl.textContent = `${allOk ? "✓ Succès" : "✗ Partiel"}: ${savedCount}/${total} équipement(s) sauvegardé(s).${backup}`;
            }
            noteEl.textContent = `Terminé le ${new Date().toLocaleTimeString("fr-FR")}`;
            if (miniSummaryEl) {
                miniSummaryEl.textContent = cancelled
                    ? "Annule"
                    : job.error
                    ? `Erreur: ${job.error}`
                    : `${allOk ? "Terminé" : "Terminé (partiel)"}: ${savedCount}/${total}`;
            }

            const _sdAt = new Date().toISOString();
            _setLabActionState("setdefault", labKey, { inProgress: false, completedAt: _sdAt, jobId: null });
            _saveCompletedAt('setdefault', labKey, _sdAt);
            endUserActivity(
                `set-default:${labKey}`,
                cancelled ? "warning" : (allOk ? "ok" : "error"),
                cancelled ? "Operation annulee" : (job.error ? String(job.error) : `${savedCount}/${total} équipement(s) sauvegardé(s)`),
            );
            const buttons = document.querySelectorAll("button[data-action='set-default-config']");
            for (const btn of buttons) {
                if (btn.getAttribute("data-vm-id") === vmId && btn.getAttribute("data-lab-name") === labName) {
                    btn.disabled = false;
                    btn.textContent = "Set current as default";
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
                ? `En cours… ${completedCount}/${total} équipement(s) capturé(s)`
                : "En cours…";
            if (miniSummaryEl) {
                miniSummaryEl.textContent = total
                    ? `${completedCount}/${total} équipement(s) capturé(s)`
                    : "En cours...";
            }
        }
    };

    const poll = async () => {
        if (finished) return;
        try {
            const resp = await apiFetch(
                `/api/vms/${encodeURIComponent(vmId)}/labs/${encodeURIComponent(labName)}/set-default-job/${encodeURIComponent(jobId)}`
            );
            if (!resp.ok) return;
            const job = await resp.json();
            if (job && !finished) renderJob(job);
        } catch (_) {
        }
    };

    setTimeout(poll, 2000);
    pollTimer = setInterval(poll, 10000);
}


