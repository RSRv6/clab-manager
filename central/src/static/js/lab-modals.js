function normalizeReconfigureDetail(detail) {
    if (typeof detail === "string") {
        try {
            const parsed = JSON.parse(detail);
            if (parsed && typeof parsed === "object" && parsed.detail) {
                return normalizeReconfigureDetail(parsed.detail);
            }
            return parsed;
        } catch (_) {
            return { ok: false, error: detail };
        }
    }

    if (!detail || typeof detail !== "object") {
        return { ok: false, error: String(detail || "Erreur inconnue") };
    }

    return detail;
}

function showReconfigureSelectModal(vmId, labName, labKey, routers) {
    closeOverlayModal();

    const overlay = document.createElement("div");
    overlay.id = "overlay-modal";
    overlay.className = "fixed inset-0 z-50 bg-black/70 backdrop-blur-sm flex items-center justify-center p-4";

    const routerCheckboxes = routers
        .map((router) => {
            const fullName = String(router.name || "");
            const labPrefix = `clab-${labName}-`;
            const shortName = fullName.startsWith(labPrefix) ? fullName.slice(labPrefix.length) : fullName || "?";
            const kind = String(router.kind || "").split("_").pop() || router.kind || "?";
            return `
                <label class="flex items-center gap-2 rounded px-2 py-1.5 hover:bg-slate-800/50">
                    <input type="checkbox" class="router-select" value="${escapeHtml(shortName)}" checked />
                    <span class="text-sm text-slate-300">${escapeHtml(shortName)}</span>
                    <span class="text-xs text-slate-500">(${escapeHtml(kind)})</span>
                </label>
            `;
        })
        .join("");

    const bodyHtml = `
        <div class="space-y-3">
            <p class="text-sm text-slate-400">Sélectionnez les équipements à reconfigurer :</p>
            <div class="max-h-64 overflow-y-auto border border-slate-700 rounded bg-slate-950/50 p-2 space-y-1">
                ${routerCheckboxes}
            </div>
            <div class="flex gap-2">
                <button type="button" id="select-all-routers" class="text-xs px-2 py-1 rounded border border-slate-600 text-slate-300 hover:bg-slate-800">Tous</button>
                <button type="button" id="deselect-all-routers" class="text-xs px-2 py-1 rounded border border-slate-600 text-slate-300 hover:bg-slate-800">Aucun</button>
            </div>
            <div class="flex gap-2 pt-2">
                <button type="button" id="reconfigure-confirm" class="flex-1 text-xs px-3 py-2 rounded border border-fuchsia-700 text-fuchsia-200 hover:bg-fuchsia-900/30 font-medium">Reconfigurer</button>
                <button type="button" data-modal-close="true" class="text-xs px-3 py-2 rounded border border-slate-600 text-slate-300 hover:bg-slate-800">Annuler</button>
            </div>
        </div>
    `;

    overlay.innerHTML = `
        <div class="w-full max-w-md rounded-xl border border-slate-700 bg-slate-900 shadow-2xl">
            <div class="flex items-start justify-between gap-4 p-4 border-b border-slate-700">
                <h3 class="text-base font-semibold text-slate-100">Reconfigurer ${escapeHtml(labName)}</h3>
                <button type="button" data-modal-close="true" class="text-xs px-2 py-1 rounded border border-slate-600 hover:bg-slate-800">×</button>
            </div>
            <div class="p-4">${bodyHtml}</div>
        </div>
    `;

    overlay.addEventListener("click", (event) => {
        const element = event.target;
        if (!(element instanceof HTMLElement)) {
            return;
        }
        if (element === overlay || element.closest("[data-modal-close='true']")) {
            closeOverlayModal();
        }
    });

    const selectAllBtn = overlay.querySelector("#select-all-routers");
    const deselectAllBtn = overlay.querySelector("#deselect-all-routers");
    const confirmBtn = overlay.querySelector("#reconfigure-confirm");

    if (selectAllBtn instanceof HTMLButtonElement) {
        selectAllBtn.addEventListener("click", () => {
            overlay.querySelectorAll(".router-select").forEach((checkbox) => {
                if (checkbox instanceof HTMLInputElement) checkbox.checked = true;
            });
        });
    }

    if (deselectAllBtn instanceof HTMLButtonElement) {
        deselectAllBtn.addEventListener("click", () => {
            overlay.querySelectorAll(".router-select").forEach((checkbox) => {
                if (checkbox instanceof HTMLInputElement) checkbox.checked = false;
            });
        });
    }

    if (confirmBtn instanceof HTMLButtonElement) {
        confirmBtn.addEventListener("click", () => {
            const selectedRouters = Array.from(overlay.querySelectorAll(".router-select:checked"))
                .map((checkbox) => (checkbox instanceof HTMLInputElement ? checkbox.value : ""))
                .filter(Boolean);

            if (selectedRouters.length === 0) {
                showToastMessage("Erreur", "Veuillez sélectionner au moins un équipement.", true);
                return;
            }

            closeOverlayModal();
            _performReconfigure(vmId, labName, labKey, selectedRouters);
        });
    }

    document.body.appendChild(overlay);
}

function renderSandboxSteps(container, steps) {
    if (!(container instanceof HTMLElement)) {
        return;
    }
    const entries = Array.isArray(steps) ? steps : [];
    if (!entries.length) {
        container.innerHTML = '<p class="text-[11px] text-slate-500">Aucune étape disponible.</p>';
        return;
    }

    container.innerHTML = entries.map((step) => {
        const ok = Boolean(step?.ok);
        const label = escapeHtml(step?.name || "step");
        const message = escapeHtml(step?.message || "");
        const data = step?.data ? escapeHtml(JSON.stringify(step.data)) : "";
        return `
            <div class="rounded border ${ok ? "border-emerald-700/50 bg-emerald-950/15" : "border-rose-700/50 bg-rose-950/15"} px-2 py-1">
                <div class="flex items-center gap-2">
                    <span class="font-mono text-xs ${ok ? "text-emerald-300" : "text-rose-300"}">${ok ? "✓" : "✗"}</span>
                    <span class="text-xs text-slate-100">${label}</span>
                </div>
                ${message ? `<p class="mt-0.5 text-[11px] text-slate-300">${message}</p>` : ""}
                ${data ? `<p class="mt-0.5 text-[10px] text-slate-500 break-all">${data}</p>` : ""}
            </div>
        `;
    }).join("");
}

function _capacityBarTone(percent) {
    if (percent <= 75) return "bg-emerald-500";
    if (percent <= 100) return "bg-amber-500";
    return "bg-rose-500";
}

function renderSandboxCapacity(container, capacityCheck) {
    if (!(container instanceof HTMLElement)) return;

    if (!capacityCheck) {
        container.innerHTML = '<p class="text-[11px] text-slate-500">Évaluation en attente (cliquez sur Valider pour estimer).</p>';
        return;
    }

    if (capacityCheck?.error && !capacityCheck?.required) {
        container.innerHTML = `<p class="text-[11px] text-rose-300">${escapeHtml(String(capacityCheck.error))}</p>`;
        return;
    }

    const required = capacityCheck?.required || {};
    const resources = capacityCheck?.resources || {};
    const fits = capacityCheck?.fits || {};
    const memReq = Number(required.memory_mb || 0);
    const memBudget = Number(resources.memory_budget_mb || 0);
    const cpuReq = Number(required.cpu_units || 0);
    const cpuBudget = Number(resources.cpu_units_budget || 0);
    const guard = Number(capacityCheck?.guard_band_percent || 20);

    const memPct = memBudget > 0 ? (memReq / memBudget) * 100 : 0;
    const cpuPct = cpuBudget > 0 ? (cpuReq / cpuBudget) * 100 : 0;
    const memBar = Math.max(0, Math.min(100, memPct));
    const cpuBar = Math.max(0, Math.min(100, cpuPct));

    const suggestion = (capacityCheck?.recommendation?.suggested_by_kind || [])
        .map((entry) => `${escapeHtml(entry.kind)}: ${entry.suggested_max}/${entry.current}`)
        .join(" · ");

    const statusText = (fits.cpu && fits.memory)
        ? "Capacité OK"
        : "Capacité insuffisante";
    const statusClass = (fits.cpu && fits.memory) ? "text-emerald-300" : "text-rose-300";

    container.innerHTML = `
        <div class="space-y-2">
            <div class="flex items-center justify-between text-[11px]">
                <span class="text-slate-400">Marge de sécurité</span>
                <span class="text-slate-200">${guard}%</span>
            </div>
            <div>
                <div class="flex items-center justify-between text-[11px] mb-1">
                    <span class="text-slate-300">RAM</span>
                    <span class="text-slate-400">${memReq.toFixed(0)} / ${memBudget.toFixed(0)} MB (${memPct.toFixed(1)}%)</span>
                </div>
                <div class="h-2 rounded bg-slate-800 overflow-hidden">
                    <div class="h-2 ${_capacityBarTone(memPct)}" style="width:${memBar}%"></div>
                </div>
            </div>
            <div>
                <div class="flex items-center justify-between text-[11px] mb-1">
                    <span class="text-slate-300">CPU</span>
                    <span class="text-slate-400">${cpuReq.toFixed(0)} / ${cpuBudget.toFixed(0)} units (${cpuPct.toFixed(1)}%)</span>
                </div>
                <div class="h-2 rounded bg-slate-800 overflow-hidden">
                    <div class="h-2 ${_capacityBarTone(cpuPct)}" style="width:${cpuBar}%"></div>
                </div>
            </div>
            <p class="text-xs ${statusClass}">${statusText}</p>
            ${suggestion ? `<p class="text-[11px] text-amber-300">Suggestion max: ${suggestion}</p>` : ""}
        </div>
    `;
}

function showSandboxTraceModal(title, steps, subtitle = "") {
    const overlay = showOverlayModal({
        title: title || "Journal sandbox",
        tone: "violet",
        widthClass: "max-w-3xl",
        bodyHtml: `
            <div class="space-y-3">
                <p id="sandbox-trace-subtitle" class="text-xs text-slate-400">${subtitle ? escapeHtml(subtitle) : ""}</p>
                <div id="sandbox-trace-steps" class="max-h-[28rem] overflow-y-auto space-y-1 rounded-lg border border-slate-800 bg-slate-950/50 p-2">
                    <p class="text-[11px] text-slate-500">Aucune étape disponible.</p>
                </div>
                <div class="flex items-center justify-end gap-2">
                    <button type="button" data-modal-close="true" class="text-xs px-3 py-2 rounded border border-slate-600 hover:bg-slate-800">Fermer</button>
                </div>
            </div>
        `,
    });

    const stepsNode = overlay?.querySelector("#sandbox-trace-steps");
    const subtitleNode = overlay?.querySelector("#sandbox-trace-subtitle");
    renderSandboxSteps(stepsNode, steps);
    return {
        setSteps(nextSteps) {
            renderSandboxSteps(stepsNode, nextSteps);
        },
        setSubtitle(nextSubtitle) {
            if (subtitleNode instanceof HTMLElement) {
                subtitleNode.textContent = String(nextSubtitle || "");
            }
        },
    };
}

function showSandboxYamlModal(vmId, labName) {
    const overlay = showOverlayModal({
        title: `Sandbox YAML · ${labName}`,
        tone: "violet",
        widthClass: "max-w-5xl",
        bodyHtml: `
            <div class="space-y-3">
                <p class="text-xs text-slate-400">Mode sandbox: éditez le YAML, validez, puis appliquez. Le réseau management et le subnet management sont immuables. Le déploiement suit: backup, destroy, écriture YAML, deploy, rollback si nécessaire.</p>
                <textarea id="sandbox-yaml-editor" class="w-full min-h-[24rem] rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-xs text-slate-100 font-mono focus:outline-none focus:ring-2 focus:ring-violet-500" spellcheck="false"></textarea>
                <p id="sandbox-yaml-feedback" class="hidden text-xs"></p>
                <div>
                    <p class="text-[11px] uppercase tracking-wide text-slate-500 mb-1">Journal des étapes</p>
                    <div id="sandbox-yaml-steps" class="max-h-48 overflow-y-auto space-y-1 rounded-lg border border-slate-800 bg-slate-950/50 p-2">
                        <p class="text-[11px] text-slate-500">Aucune étape disponible.</p>
                    </div>
                </div>
                <div>
                    <p class="text-[11px] uppercase tracking-wide text-slate-500 mb-1">Capacité estimée (RAM / CPU)</p>
                    <div id="sandbox-capacity" class="rounded-lg border border-slate-800 bg-slate-950/50 p-2">
                        <p class="text-[11px] text-slate-500">Évaluation en attente (cliquez sur Valider pour estimer).</p>
                    </div>
                </div>
                <div class="flex items-center justify-end gap-2">
                    <button type="button" data-modal-close="true" class="text-xs px-3 py-2 rounded border border-slate-600 hover:bg-slate-800">Fermer</button>
                    <button type="button" id="sandbox-yaml-topo" class="text-xs px-3 py-2 rounded border border-sky-700 text-sky-200 hover:bg-sky-900/30" title="Ouvrir dans l'éditeur visuel de topologie">
                        <svg class="inline w-3 h-3 mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 20l-5.447-2.724A1 1 0 013 16.382V5.618a1 1 0 011.447-.894L9 7m0 13l6-3m-6 3V7m6 10l4.553 2.276A1 1 0 0021 18.382V7.618a1 1 0 00-.553-.894L15 4m0 13V4m0 0L9 7"/></svg>
                        Topology Builder
                    </button>
                    <button type="button" id="sandbox-yaml-validate" class="text-xs px-3 py-2 rounded border border-cyan-700 text-cyan-200 hover:bg-cyan-900/30">Valider</button>
                    <button type="button" id="sandbox-yaml-apply" class="text-xs px-3 py-2 rounded border border-violet-700 text-violet-200 hover:bg-violet-900/30">Appliquer</button>
                </div>
            </div>
        `,
    });

    const editor = overlay?.querySelector("#sandbox-yaml-editor");
    const feedback = overlay?.querySelector("#sandbox-yaml-feedback");
    const stepsNode = overlay?.querySelector("#sandbox-yaml-steps");
    const capacityNode = overlay?.querySelector("#sandbox-capacity");
    const validateBtn = overlay?.querySelector("#sandbox-yaml-validate");
    const applyBtn = overlay?.querySelector("#sandbox-yaml-apply");
    const topoBtn = overlay?.querySelector("#sandbox-yaml-topo");

    if (topoBtn instanceof HTMLButtonElement) {
        topoBtn.addEventListener("click", () => {
            window.open(
                `/topology-builder?vm_id=${encodeURIComponent(vmId)}&lab_name=${encodeURIComponent(labName)}&source=sandbox`,
                '_blank'
            );
        });
    }
    if (!(editor instanceof HTMLTextAreaElement)) {
        return;
    }

    const setFeedback = (message, isError = false) => {
        if (!(feedback instanceof HTMLElement)) return;
        feedback.classList.remove("hidden", "text-rose-300", "text-emerald-300", "text-amber-300");
        feedback.classList.add(isError ? "text-rose-300" : "text-emerald-300");
        feedback.textContent = message;
    };

    const setBusy = (busy) => {
        if (validateBtn instanceof HTMLButtonElement) validateBtn.disabled = busy;
        if (applyBtn instanceof HTMLButtonElement) applyBtn.disabled = busy;
    };

    apiFetch(`/api/vms/${encodeURIComponent(vmId)}/labs/${encodeURIComponent(labName)}/sandbox-yaml`)
        .then(async (response) => {
            if (!response.ok) {
                const txt = await response.text();
                let detail = txt || `HTTP ${response.status}`;
                try { detail = JSON.parse(txt)?.detail || detail; } catch (_) {}
                throw new Error(_extractApiErrorDetail(detail));
            }
            return response.json();
        })
        .then((payload) => {
            editor.value = String(payload.yaml_text || "");
        })
        .catch((error) => {
            setFeedback(error?.message || "Chargement YAML impossible.", true);
        });

    const submit = async (apply) => {
        setBusy(true);
        const labKey = findLabKey(vmId, labName);

        if (apply) {
            beginUserActivity({
                id: `yaml-deploy:${labKey}`,
                title: "Déploiement YAML",
                target: `${labName} @ ${vmId}`,
                details: "Déploiement en cours",
            });
        }

        if (feedback instanceof HTMLElement) feedback.classList.add("hidden");
        renderSandboxSteps(stepsNode, [{ name: "request", ok: true, message: apply ? "Application en cours..." : "Validation en cours..." }]);
        if (capacityNode instanceof HTMLElement) {
            capacityNode.innerHTML = '<p class="text-[11px] text-slate-500">Évaluation capacité en cours...</p>';
        }
        try {
            const response = await apiFetch(`/api/vms/${encodeURIComponent(vmId)}/labs/${encodeURIComponent(labName)}/sandbox-yaml`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ yaml_text: editor.value, apply }),
            });
            if (!response.ok) {
                const txt = await response.text();
                let detail = txt || `HTTP ${response.status}`;
                try {
                    const parsed = JSON.parse(txt);
                    detail = parsed?.detail || detail;
                    renderSandboxSteps(stepsNode, detail?.steps || []);
                    renderSandboxCapacity(capacityNode, detail?.capacity_check || null);
                } catch (_) {}
                if (apply) {
                    endUserActivity(`yaml-deploy:${labKey}`, "error", _extractApiErrorDetail(detail));
                }
                throw new Error(_extractApiErrorDetail(detail));
            }
            const payload = await response.json();
            renderSandboxSteps(stepsNode, payload?.result?.steps || []);
            renderSandboxCapacity(capacityNode, payload?.result?.capacity_check || null);
            const validation = payload?.result?.validation || {};
            const warnCount = Array.isArray(validation.warnings) ? validation.warnings.length : 0;
            if (apply) {
                endUserActivity(`yaml-deploy:${labKey}`, "ok", warnCount ? `Déploiement réussi (${warnCount} avertissement(s))` : "Déploiement réussi");
                await refreshState();
                setFeedback(`Déploiement sandbox terminé${warnCount ? ` (${warnCount} avertissement(s))` : ""}.`);
            } else {
                const errors = Array.isArray(validation.errors) ? validation.errors : [];
                const warnings = Array.isArray(validation.warnings) ? validation.warnings : [];
                if (errors.length) {
                    setFeedback(`Validation KO: ${errors.join(" | ")}`, true);
                } else if (warnings.length) {
                    if (feedback instanceof HTMLElement) {
                        feedback.classList.remove("hidden", "text-rose-300", "text-emerald-300");
                        feedback.classList.add("text-amber-300");
                        feedback.textContent = `Validation OK avec avertissements: ${warnings.join(" | ")}`;
                    }
                } else {
                    setFeedback("Validation OK");
                }
            }
        } catch (error) {
            if (apply) {
                endUserActivity(`yaml-deploy:${labKey}`, "error", error?.message || "Déploiement échoué");
            }
            setFeedback(error?.message || "Action impossible", true);
        } finally {
            setBusy(false);
        }
    };

    validateBtn?.addEventListener("click", () => submit(false));
    applyBtn?.addEventListener("click", () => {
        const confirmed = window.confirm("Appliquer ce YAML va redeployer le LAB. Continuer ?");
        if (!confirmed) return;
        submit(true);
    });
}

function showInventoryYamlModal(vmId, labName, topologyFile = "") {
    const overlay = showOverlayModal({
        title: `Inventaire YAML · ${labName}`,
        tone: "slate",
        widthClass: "max-w-5xl",
        bodyHtml: `
            <div class="space-y-3">
                <p class="text-xs text-slate-400">Édition admin directe du fichier de topologie détecté sur l'agent${topologyFile ? ` (${escapeHtml(topologyFile)})` : ""}.</p>
                <textarea id="inventory-yaml-editor" class="w-full min-h-[24rem] rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-xs text-slate-100 font-mono focus:outline-none focus:ring-2 focus:ring-cyan-500" spellcheck="false"></textarea>
                <p id="inventory-yaml-feedback" class="hidden text-xs"></p>
                <div class="flex items-center justify-end gap-2">
                    <button type="button" data-modal-close="true" class="text-xs px-3 py-2 rounded border border-slate-600 hover:bg-slate-800">Fermer</button>
                    <button type="button" id="inventory-yaml-save" class="text-xs px-3 py-2 rounded border border-cyan-700 text-cyan-200 hover:bg-cyan-900/30">Save</button>
                </div>
            </div>
        `,
    });

    const editor = overlay?.querySelector("#inventory-yaml-editor");
    const feedback = overlay?.querySelector("#inventory-yaml-feedback");
    const saveBtn = overlay?.querySelector("#inventory-yaml-save");

    if (!(editor instanceof HTMLTextAreaElement)) {
        return;
    }

    const setFeedback = (message, tone = "success") => {
        if (!(feedback instanceof HTMLElement)) {
            return;
        }
        feedback.classList.remove("hidden", "text-rose-300", "text-emerald-300", "text-amber-300");
        if (tone === "error") {
            feedback.classList.add("text-rose-300");
        } else if (tone === "warning") {
            feedback.classList.add("text-amber-300");
        } else {
            feedback.classList.add("text-emerald-300");
        }
        feedback.textContent = String(message || "");
    };

    const setBusy = (busy) => {
        if (!(saveBtn instanceof HTMLButtonElement)) {
            return;
        }
        saveBtn.disabled = busy;
        saveBtn.textContent = busy ? "Save..." : "Save";
    };

    setBusy(true);
    apiFetch(`/api/admin/vms/${encodeURIComponent(vmId)}/labs/${encodeURIComponent(labName)}/topology-yaml`)
        .then(async (response) => {
            if (!response.ok) {
                const text = await response.text();
                let detail = text || `HTTP ${response.status}`;
                try { detail = _extractApiErrorDetail(JSON.parse(text)) || detail; } catch (_) {}
                throw new Error(detail);
            }
            return response.json();
        })
        .then((payload) => {
            editor.value = String(payload?.yaml_text || "");
        })
        .catch((error) => {
            setFeedback(error?.message || "Chargement YAML impossible.", "error");
        })
        .finally(() => {
            setBusy(false);
        });

    saveBtn?.addEventListener("click", async () => {
        setBusy(true);
        if (feedback instanceof HTMLElement) {
            feedback.classList.add("hidden");
        }
        try {
            const response = await apiFetch(`/api/admin/vms/${encodeURIComponent(vmId)}/labs/${encodeURIComponent(labName)}/topology-yaml`, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ yaml_text: editor.value }),
            });
            if (!response.ok) {
                const text = await response.text();
                let detail = text || `HTTP ${response.status}`;
                try { detail = _extractApiErrorDetail(JSON.parse(text)) || detail; } catch (_) {}
                throw new Error(detail);
            }
            await response.json();
            setFeedback("YAML sauvegardé sur l'agent.", "success");
            showToastMessage("YAML sauvegardé", `${labName} (${vmId})`, false);
            await refreshState();
            if (currentView === "admin" && isAdmin()) {
                void refreshAdminPanel({ force: true, silent: true });
            }
        } catch (error) {
            setFeedback(error?.message || "Sauvegarde impossible.", "error");
        } finally {
            setBusy(false);
        }
    });
}

function showReservationModal(vmId, labName) {
    const overlay = showOverlayModal({
        title: `Réserver ${labName}`,
        tone: "amber",
        widthClass: "max-w-2xl",
        bodyHtml: `
            <div class="space-y-4">
                <div class="flex items-center gap-2 border-b border-slate-700 pb-2">
                    <button type="button" id="reservation-tab-new" class="text-xs px-3 py-1.5 rounded border border-amber-700 text-amber-200 bg-amber-900/30">Réserver</button>
                    <button type="button" id="reservation-tab-scheduled" class="text-xs px-3 py-1.5 rounded border border-slate-700 text-slate-300 hover:bg-slate-800">Réservations planifiées</button>
                </div>

                <section id="reservation-panel-new" class="space-y-4">
                    <form id="reservation-form" class="space-y-4">
                        <p class="text-sm text-slate-300">Le LAB sera réservé au nom de <span class="font-semibold text-slate-100">${escapeHtml(currentUser?.full_name || currentUser?.username || "")}</span>. La durée maximale est de ${MAX_RESERVATION_HOURS} heures.</p>
                        <div>
                            <label for="reservation-start-mode" class="block text-xs uppercase tracking-wide text-slate-400 mb-1">Début</label>
                            <select id="reservation-start-mode" name="start_mode" class="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-amber-500">
                                <option value="now" selected>Maintenant</option>
                                <option value="future">Planifiée</option>
                            </select>
                        </div>
                        <div id="reservation-start-at-wrap" class="hidden">
                            <label for="reservation-start-at" class="block text-xs uppercase tracking-wide text-slate-400 mb-1">Date et heure de début</label>
                            <input id="reservation-start-at" name="starts_at" type="datetime-local" class="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-amber-500" />
                        </div>
                        <div>
                            <div class="flex items-center justify-between mb-1">
                                <label for="reservation-duration" class="block text-xs uppercase tracking-wide text-slate-400">Durée (30 min)</label>
                                <span id="reservation-duration-value" class="text-sm font-semibold text-amber-300">1h</span>
                            </div>
                            <input id="reservation-duration" name="duration_hours" type="range" min="0.5" max="${MAX_RESERVATION_HOURS}" step="0.5" value="1" required class="w-full accent-amber-500" />
                            <div class="mt-1 flex items-center justify-between text-[11px] text-slate-500">
                                <span>30m</span>
                                <span>${MAX_RESERVATION_HOURS}h</span>
                            </div>
                        </div>
                        <p id="reservation-error" class="hidden text-sm text-rose-300"></p>
                        <div class="flex items-center justify-end gap-2">
                            <button type="button" data-modal-close="true" class="text-xs px-3 py-2 rounded border border-slate-600 hover:bg-slate-800">Annuler</button>
                            <button type="submit" id="reservation-submit" class="text-xs px-3 py-2 rounded border border-amber-700 text-amber-200 hover:bg-amber-900/30">Confirmer la réservation</button>
                        </div>
                    </form>
                </section>

                <section id="reservation-panel-scheduled" class="hidden space-y-3">
                    <p class="text-xs text-slate-400">Visibles pour tous. Le propriétaire peut annuler; les admins peuvent annuler et modifier.</p>
                    <div id="reservation-scheduled-list" class="max-h-[18rem] overflow-auto space-y-2 rounded-lg border border-slate-700 bg-slate-950/50 p-2">
                        <p class="text-xs text-slate-500">Chargement...</p>
                    </div>
                    <div class="flex items-center justify-end gap-2">
                        <button type="button" id="reservation-scheduled-refresh" class="text-xs px-3 py-2 rounded border border-slate-600 hover:bg-slate-800">Rafraîchir</button>
                    </div>
                </section>
            </div>
        `,
    });

    const tabNew = overlay.querySelector("#reservation-tab-new");
    const tabScheduled = overlay.querySelector("#reservation-tab-scheduled");
    const panelNew = overlay.querySelector("#reservation-panel-new");
    const panelScheduled = overlay.querySelector("#reservation-panel-scheduled");
    const scheduledList = overlay.querySelector("#reservation-scheduled-list");
    const scheduledRefresh = overlay.querySelector("#reservation-scheduled-refresh");
    const form = overlay.querySelector("#reservation-form");
    const errorNode = overlay.querySelector("#reservation-error");
    const submitButton = overlay.querySelector("#reservation-submit");
    const durationInput = overlay.querySelector("#reservation-duration");
    const durationValue = overlay.querySelector("#reservation-duration-value");
    const startMode = overlay.querySelector("#reservation-start-mode");
    const startAtWrap = overlay.querySelector("#reservation-start-at-wrap");
    const startAtInput = overlay.querySelector("#reservation-start-at");

    const setTab = (tabName) => {
        const scheduledMode = tabName === "scheduled";
        panelNew?.classList.toggle("hidden", scheduledMode);
        panelScheduled?.classList.toggle("hidden", !scheduledMode);
    };

    const canManageScheduled = (entry) => {
        const owner = String(entry?.owner_username || "").toLowerCase();
        const me = String(currentUser?.username || "").toLowerCase();
        return isAdmin() || (owner && owner === me);
    };

    const renderScheduledList = (items) => {
        if (!(scheduledList instanceof HTMLElement)) return;
        const rows = Array.isArray(items) ? items : [];
        if (!rows.length) {
            scheduledList.innerHTML = '<p class="text-xs text-slate-500">Aucune reservation planifiée</p>';
            return;
        }
        scheduledList.innerHTML = rows.map((entry) => {
            const startsAt = new Date(String(entry?.starts_at || ""));
            const expiresAt = new Date(String(entry?.expires_at || ""));
            const startsLabel = Number.isFinite(startsAt.getTime()) ? startsAt.toLocaleString("fr-FR") : String(entry?.starts_at || "-");
            const expiresLabel = Number.isFinite(expiresAt.getTime()) ? expiresAt.toLocaleString("fr-FR") : String(entry?.expires_at || "-");
            const id = String(entry?.reservation_id || "");
            const manageable = canManageScheduled(entry);
            return `
                <article class="rounded border border-slate-700 bg-slate-900/40 px-3 py-2 space-y-1">
                    <p class="text-xs text-slate-100 font-medium">${escapeHtml(String(entry?.reserved_by || entry?.owner_username || "Utilisateur"))}</p>
                    <p class="text-[11px] text-slate-400">Début: ${escapeHtml(startsLabel)}</p>
                    <p class="text-[11px] text-slate-400">Fin: ${escapeHtml(expiresLabel)}</p>
                    <div class="flex items-center gap-2 pt-1">
                        ${manageable ? `<button type="button" data-action="cancel-scheduled-reservation" data-reservation-id="${escapeHtml(id)}" class="text-xs px-2 py-1 rounded border border-rose-700 text-rose-300 hover:bg-rose-900/30">Annuler</button>` : ""}
                        ${manageable ? `<button type="button" data-action="edit-scheduled-reservation" data-reservation-id="${escapeHtml(id)}" class="text-xs px-2 py-1 rounded border border-amber-700 text-amber-200 hover:bg-amber-900/30">Modifier</button>` : ""}
                    </div>
                </article>
            `;
        }).join("");
    };

    const loadScheduledReservations = async () => {
        if (!(scheduledList instanceof HTMLElement)) return;
        scheduledList.innerHTML = '<p class="text-xs text-slate-500">Chargement...</p>';
        try {
            const response = await apiFetch(`/api/vms/${encodeURIComponent(vmId)}/labs/${encodeURIComponent(labName)}/reservation/scheduled`);
            if (!response.ok) {
                if (response.status === 404) {
                    renderScheduledList([]);
                    return;
                }
                const payloadText = await response.text();
                let detail = payloadText || `HTTP ${response.status}`;
                try { detail = JSON.parse(payloadText)?.detail || detail; } catch (_) {}
                throw new Error(detail);
            }
            const payload = await response.json();
            renderScheduledList(payload?.items || []);
        } catch (error) {
            const message = String(error?.message || "");
            if (message.toLowerCase() === "not found") {
                renderScheduledList([]);
                return;
            }
            scheduledList.innerHTML = `<p class="text-xs text-rose-300">${escapeHtml(message || "Chargement impossible")}</p>`;
        }
    };

    const ensureScheduledReservationApiReady = async () => {
        const response = await apiFetch(`/api/vms/${encodeURIComponent(vmId)}/labs/${encodeURIComponent(labName)}/reservation/scheduled`);
        if (response.status === 404) {
            throw new Error("La planification n'est pas disponible sur ce serveur (mise à jour/restart central requis).");
        }
        if (!response.ok) {
            const payloadText = await response.text();
            let detail = payloadText || `HTTP ${response.status}`;
            try { detail = JSON.parse(payloadText)?.detail || detail; } catch (_) {}
            throw new Error(detail);
        }
    };

    if (tabNew instanceof HTMLButtonElement) {
        tabNew.addEventListener("click", () => setTab("new"));
    }
    if (tabScheduled instanceof HTMLButtonElement) {
        tabScheduled.addEventListener("click", () => { setTab("scheduled"); void loadScheduledReservations(); });
    }
    if (scheduledRefresh instanceof HTMLButtonElement) {
        scheduledRefresh.addEventListener("click", () => { void loadScheduledReservations(); });
    }

    if (scheduledList instanceof HTMLElement) {
        scheduledList.addEventListener("click", async (event) => {
            const target = event.target;
            if (!(target instanceof HTMLElement)) return;

            const cancelBtn = target.closest("button[data-action='cancel-scheduled-reservation']");
            if (cancelBtn instanceof HTMLButtonElement) {
                const reservationId = cancelBtn.getAttribute("data-reservation-id") || "";
                if (!reservationId || !window.confirm("Annuler cette réservation planifiée ?")) return;
                cancelBtn.disabled = true;
                try {
                    const response = await apiFetch(`/api/vms/${encodeURIComponent(vmId)}/labs/${encodeURIComponent(labName)}/reservation/scheduled/${encodeURIComponent(reservationId)}`, { method: "DELETE" });
                    if (!response.ok) {
                        const payloadText = await response.text();
                        let detail = payloadText || `HTTP ${response.status}`;
                        try { detail = JSON.parse(payloadText)?.detail || detail; } catch (_) {}
                        throw new Error(detail);
                    }
                    await refreshState();
                    await loadScheduledReservations();
                } catch (error) {
                    showToastMessage("Annulation impossible", error?.message || "Erreur inconnue", true);
                } finally {
                    cancelBtn.disabled = false;
                }
                return;
            }

            const editBtn = target.closest("button[data-action='edit-scheduled-reservation']");
            if (editBtn instanceof HTMLButtonElement) {
                const reservationId = editBtn.getAttribute("data-reservation-id") || "";
                if (!reservationId) return;
                const nextStartRaw = window.prompt("Nouvelle date/heure (YYYY-MM-DDTHH:MM)", "");
                if (nextStartRaw === null) return;
                const nextStart = new Date(nextStartRaw.trim());
                if (!Number.isFinite(nextStart.getTime())) {
                    showToastMessage("Modification impossible", "Date invalide", true);
                    return;
                }
                const nextDurationRaw = window.prompt(`Nouvelle durée en heures (max ${MAX_RESERVATION_HOURS})`, "1");
                if (nextDurationRaw === null) return;
                const nextDuration = Number(nextDurationRaw);
                if (!Number.isFinite(nextDuration) || nextDuration <= 0 || nextDuration > MAX_RESERVATION_HOURS) {
                    showToastMessage("Modification impossible", "Durée invalide", true);
                    return;
                }
                editBtn.disabled = true;
                try {
                    const response = await apiFetch(`/api/vms/${encodeURIComponent(vmId)}/labs/${encodeURIComponent(labName)}/reservation/scheduled/${encodeURIComponent(reservationId)}`, {
                        method: "PATCH",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ starts_at: nextStart.toISOString(), duration_hours: nextDuration }),
                    });
                    if (!response.ok) {
                        const payloadText = await response.text();
                        let detail = payloadText || `HTTP ${response.status}`;
                        try { detail = JSON.parse(payloadText)?.detail || detail; } catch (_) {}
                        throw new Error(detail);
                    }
                    await refreshState();
                    await loadScheduledReservations();
                } catch (error) {
                    showToastMessage("Modification impossible", error?.message || "Erreur inconnue", true);
                } finally {
                    editBtn.disabled = false;
                }
            }
        });
    }

    if (!(form instanceof HTMLFormElement)
        || !(errorNode instanceof HTMLElement)
        || !(submitButton instanceof HTMLButtonElement)
        || !(durationInput instanceof HTMLInputElement)
        || !(durationValue instanceof HTMLElement)
        || !(startMode instanceof HTMLSelectElement)
        || !(startAtWrap instanceof HTMLElement)
        || !(startAtInput instanceof HTMLInputElement)) {
        return;
    }

    setTab("new");

    const now = new Date();
    const pad = (num) => String(num).padStart(2, "0");
    const localMin = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}T${pad(now.getHours())}:${pad(now.getMinutes())}`;
    startAtInput.min = localMin;
    startAtInput.value = localMin;

    const syncStartMode = () => {
        const isFuture = startMode.value === "future";
        startAtWrap.classList.toggle("hidden", !isFuture);
        startAtInput.required = isFuture;
    };
    syncStartMode();
    startMode.addEventListener("change", syncStartMode);

    const updateDurationPreview = () => {
        durationValue.textContent = formatDurationHours(durationInput.value);
    };
    updateDurationPreview();
    durationInput.addEventListener("input", updateDurationPreview);

    form.addEventListener("submit", async (event) => {
        event.preventDefault();

        const formData = new FormData(form);
        const durationHours = Number(formData.get("duration_hours") || 0);
        const startModeValue = String(formData.get("start_mode") || "now");
        const startsAtRaw = String(formData.get("starts_at") || "").trim();

        if (!Number.isFinite(durationHours) || durationHours <= 0 || durationHours > MAX_RESERVATION_HOURS) {
            errorNode.textContent = `Merci de renseigner une durée entre 0.5 et ${MAX_RESERVATION_HOURS} heures.`;
            errorNode.classList.remove("hidden");
            return;
        }

        let startsAtIso = null;
        if (startModeValue === "future") {
            if (!startsAtRaw) {
                errorNode.textContent = "Merci de renseigner une date/heure de début.";
                errorNode.classList.remove("hidden");
                return;
            }
            const startsAtDate = new Date(startsAtRaw);
            if (!Number.isFinite(startsAtDate.getTime())) {
                errorNode.textContent = "Date/heure de début invalide.";
                errorNode.classList.remove("hidden");
                return;
            }
            startsAtIso = startsAtDate.toISOString();

            try {
                await ensureScheduledReservationApiReady();
            } catch (error) {
                errorNode.textContent = error?.message || "Planification indisponible.";
                errorNode.classList.remove("hidden");
                return;
            }
        }

        errorNode.classList.add("hidden");
        submitButton.disabled = true;
        submitButton.textContent = startModeValue === "future" ? "Planification..." : "Réservation...";

        try {
            const response = await apiFetch(`/api/vms/${encodeURIComponent(vmId)}/labs/${encodeURIComponent(labName)}/reservation`, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                },
                body: JSON.stringify({
                    duration_hours: durationHours,
                    starts_at: startsAtIso,
                }),
            });

            if (!response.ok) {
                const payloadText = await response.text();
                let detail = payloadText || `HTTP ${response.status}`;
                try {
                    const parsed = JSON.parse(payloadText);
                    detail = parsed?.detail || detail;
                } catch (_) {
                }
                throw new Error(detail);
            }

            await refreshState();
            if (startsAtIso) {
                setTab("scheduled");
                await loadScheduledReservations();
                const startLabel = new Date(startsAtIso).toLocaleString("fr-FR");
                showToastMessage("Réservation planifiée", `Le LAB ${labName} est planifié à partir du ${startLabel}.`);
            } else {
                closeOverlayModal();
                showToastMessage("Réservation créée", `Le LAB ${labName} est réservé pour ${currentUser?.full_name || currentUser?.username || "vous"}.`);
            }
        } catch (error) {
            errorNode.textContent = error?.message || "Réservation impossible.";
            errorNode.classList.remove("hidden");
        } finally {
            submitButton.disabled = false;
            submitButton.textContent = "Confirmer la réservation";
        }
    });
}

async function showReservationExtensionModal(vmId, labName) {
    let reservation = null;
    try {
        const response = await apiFetch(`/api/vms/${encodeURIComponent(vmId)}/labs/${encodeURIComponent(labName)}/reservation`);
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }
        const payload = await response.json();
        reservation = payload?.reservation || null;
    } catch (error) {
        showToastMessage("Extension impossible", error?.message || "Impossible de lire la réservation.", true);
        return;
    }

    if (!reservation) {
        showToastMessage("Extension impossible", "Aucune réservation active.", true);
        return;
    }

    const durationHours = Number(reservation.duration_hours || 0);
    const availableExtra = Math.max(0, MAX_RESERVATION_TOTAL_HOURS - durationHours);
    if (availableExtra <= 0) {
        showToastMessage("Extension impossible", `La durée totale maximale (${MAX_RESERVATION_TOTAL_HOURS}h) est atteinte.`, true);
        return;
    }

    const expiresAt = new Date(reservation.expires_at || "");
    const remainingMs = expiresAt.getTime() - Date.now();
    const extensionWindowMs = RESERVATION_EXTENSION_WINDOW_HOURS * 3600 * 1000;
    if (!Number.isFinite(expiresAt.getTime()) || remainingMs <= 0) {
        showToastMessage("Extension impossible", "La réservation est expirée.", true);
        return;
    }
    if (remainingMs > extensionWindowMs) {
        const waitLabel = formatRemainingReservation(new Date(Date.now() + (remainingMs - extensionWindowMs)).toISOString());
        showToastMessage("Extension indisponible", `L'extension est possible uniquement dans la dernière heure. Réessayez dans ${waitLabel}.`, true);
        return;
    }

    const overlay = showOverlayModal({
        title: `Étendre ${labName}`,
        tone: "amber",
        widthClass: "max-w-xl",
        bodyHtml: `
            <form id="reservation-extend-form" class="space-y-4">
                <p class="text-sm text-slate-300">
                    Durée actuelle: <span class="font-semibold text-slate-100">${durationHours}h</span> ·
                    durée totale max: <span class="font-semibold text-slate-100">${MAX_RESERVATION_TOTAL_HOURS}h</span>.
                </p>
                <p class="text-xs text-amber-300">Extension possible maintenant (dernière heure de session).</p>
                <div>
                    <label for="reservation-additional-hours" class="block text-xs uppercase tracking-wide text-slate-400 mb-1">Heures supplémentaires (max ${availableExtra}h)</label>
                    <input id="reservation-additional-hours" name="additional_hours" type="number" min="0.5" max="${availableExtra}" step="0.5" value="${Math.min(1, availableExtra)}" required class="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-amber-500" />
                </div>
                <p id="reservation-extend-error" class="hidden text-sm text-rose-300"></p>
                <div class="flex items-center justify-end gap-2">
                    <button type="button" data-modal-close="true" class="text-xs px-3 py-2 rounded border border-slate-600 hover:bg-slate-800">Annuler</button>
                    <button type="submit" id="reservation-extend-submit" class="text-xs px-3 py-2 rounded border border-amber-700 text-amber-200 hover:bg-amber-900/30">Confirmer l'extension</button>
                </div>
            </form>
        `,
    });

    const form = overlay.querySelector("#reservation-extend-form");
    const errorNode = overlay.querySelector("#reservation-extend-error");
    const submitButton = overlay.querySelector("#reservation-extend-submit");

    if (!(form instanceof HTMLFormElement) || !(errorNode instanceof HTMLElement) || !(submitButton instanceof HTMLButtonElement)) {
        return;
    }

    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const formData = new FormData(form);
        const additionalHours = Number(formData.get("additional_hours") || 0);

        if (!Number.isFinite(additionalHours) || additionalHours <= 0 || additionalHours > availableExtra) {
            errorNode.textContent = `Merci de renseigner une extension entre 0.5h et ${availableExtra}h.`;
            errorNode.classList.remove("hidden");
            return;
        }

        errorNode.classList.add("hidden");
        submitButton.disabled = true;
        submitButton.textContent = "Extension...";

        try {
            const response = await apiFetch(`/api/vms/${encodeURIComponent(vmId)}/labs/${encodeURIComponent(labName)}/reservation/extend`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ additional_hours: additionalHours }),
            });

            if (!response.ok) {
                const payloadText = await response.text();
                let detail = payloadText || `HTTP ${response.status}`;
                try {
                    const parsed = JSON.parse(payloadText);
                    detail = parsed?.detail || detail;
                } catch (_) {
                }
                throw new Error(detail);
            }

            closeOverlayModal();
            await refreshState();
            showToastMessage("Réservation étendue", `Le LAB ${labName} a été prolongé de ${additionalHours}h.`);
        } catch (error) {
            errorNode.textContent = error?.message || "Extension impossible.";
            errorNode.classList.remove("hidden");
        } finally {
            submitButton.disabled = false;
            submitButton.textContent = "Confirmer l'extension";
        }
    });
}

function buildReconfigureFeedback(vmId, labName, payload, fallbackMessage) {
    const detail = normalizeReconfigureDetail(payload);
    const safeVm = vmId || "VM";
    const safeLab = labName || "LAB";

    if (!detail || typeof detail !== "object") {
        return {
            title: `Reconfiguration ${safeVm}/${safeLab}`,
            summary: fallbackMessage || "Résultat indisponible.",
            detailLines: [],
            isError: true,
        };
    }

    const successCount = Number(detail.success_count ?? 0);
    const total = Number(detail.total ?? 0);
    const results = Array.isArray(detail.results) ? detail.results : [];
    const failures = results.filter((item) => item && item.ok === false);

    if (detail.ok === true) {
        return {
            title: `Reconfiguration ${safeVm}/${safeLab}`,
            summary: `Succès: ${successCount}/${total} équipement(s) reconfiguré(s).`,
            detailLines: [],
            isError: false,
        };
    }

    if (failures.length) {
        return {
            title: `Reconfiguration partielle ${safeVm}/${safeLab}`,
            summary: `Succès: ${successCount}/${total}. ${failures.length} échec(s).`,
            detailLines: failures.map((item) => `${item.node || "node"}: ${item.error || "Erreur inconnue"}`),
            isError: true,
        };
    }

    return {
        title: `Reconfiguration ${safeVm}/${safeLab}`,
        summary: detail.error || fallbackMessage || `Succès: ${successCount}/${total}.`,
        detailLines: [],
        isError: detail.ok !== true,
    };
}
