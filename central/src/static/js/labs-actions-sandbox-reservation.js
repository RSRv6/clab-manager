function _saveCompletedAt(type, labKey, iso) {
    try { localStorage.setItem(`vlm:completedAt:${type}:${labKey}`, iso); } catch (_) {}
}
function _loadCompletedAt(type, labKey) {
    try { return localStorage.getItem(`vlm:completedAt:${type}:${labKey}`) || null; } catch (_) { return null; }
}

const _LAB_ACTION_STATE_PREFIX = "vlm:actionState:";
function _labActionMap(type) {
    return String(type || "") === "setdefault" ? labSetDefaultState : labReconfigureState;
}
function _labActionStorageKey(type, labKey) {
    return `${_LAB_ACTION_STATE_PREFIX}${String(type || "")}::${String(labKey || "")}`;
}
function _normalizeLabActionState(raw) {
    const inProgress = Boolean(raw?.inProgress);
    const completedAt = raw?.completedAt ? String(raw.completedAt) : null;
    const startedAt = raw?.startedAt ? String(raw.startedAt) : null;
    const jobId = raw?.jobId ? String(raw.jobId) : null;

    if (!inProgress && !completedAt && !startedAt && !jobId) {
        return null;
    }
    return { inProgress, completedAt, startedAt, jobId };
}
function _setLabActionState(type, labKey, nextState) {
    const safeType = String(type || "").trim();
    const safeLabKey = String(labKey || "").trim();
    if (!safeType || !safeLabKey) {
        return;
    }

    const map = _labActionMap(safeType);
    const normalized = _normalizeLabActionState(nextState);
    const storageKey = _labActionStorageKey(safeType, safeLabKey);

    if (!normalized) {
        map.delete(safeLabKey);
        try { localStorage.removeItem(storageKey); } catch (_) {}
        return;
    }

    map.set(safeLabKey, normalized);
    try { localStorage.setItem(storageKey, JSON.stringify(normalized)); } catch (_) {}
}
function _loadAllInProgressStates() {
    labReconfigureState.clear();
    labSetDefaultState.clear();

    try {
        const keys = [];
        for (let index = 0; index < localStorage.length; index += 1) {
            const key = localStorage.key(index);
            if (key && key.startsWith(_LAB_ACTION_STATE_PREFIX)) {
                keys.push(key);
            }
        }

        for (const key of keys) {
            const suffix = key.slice(_LAB_ACTION_STATE_PREFIX.length);
            const separator = suffix.indexOf("::");
            if (separator < 0) {
                continue;
            }

            const type = suffix.slice(0, separator);
            const keyLab = suffix.slice(separator + 2);
            if (!type || !keyLab) {
                continue;
            }

            let parsed = null;
            try {
                parsed = JSON.parse(localStorage.getItem(key) || "null");
            } catch (_) {
                parsed = null;
            }

            const normalized = _normalizeLabActionState(parsed);
            if (!normalized) {
                localStorage.removeItem(key);
                continue;
            }
            _labActionMap(type).set(keyLab, normalized);
        }
    } catch (_) {
    }
}

function _stripCredentialQueryParamsFromUrl() {
    try {
        const url = new URL(window.location.href);
        const sensitiveParams = ["username", "password", "pass", "pwd"];
        const hadSensitive = sensitiveParams.some((name) => url.searchParams.has(name));
        if (!hadSensitive) {
            return;
        }

        sensitiveParams.forEach((name) => url.searchParams.delete(name));
        const cleaned = `${url.pathname}${url.search}${url.hash}`;
        window.history.replaceState({}, document.title, cleaned || "/");
    } catch (_) {
    }
}


async function performLabActionRequest(options) {
    const vmId = String(options?.vmId || "");
    const labName = String(options?.labName || "");
    const action = String(options?.action || "");
    const topologyFile = String(options?.topologyFile || "");
    const button = options?.button;
    const busyLabel = String(options?.busyLabel || "Traitement...");
    const successTitle = String(options?.successTitle || "Action effectuée");
    const errorTitle = String(options?.errorTitle || "Action impossible");

    if (!vmId || !labName || !action) {
        return;
    }

    const trackedActivityId = beginUserActivity({
        id: `lab-action:${action}:${vmId}:${labName}:${Date.now()}`,
        title: `Action ${action.toUpperCase()}`,
        target: `${labName} @ ${vmId}`,
        details: "Traitement en cours",
    });

    let originalLabel = null;
    if (button instanceof HTMLButtonElement) {
        originalLabel = button.textContent;
        button.disabled = true;
        button.textContent = busyLabel;
    }

    const bodyPayload = topologyFile ? { topology_file: topologyFile } : null;
    try {
        const response = await apiFetch(`/api/vms/${encodeURIComponent(vmId)}/labs/${encodeURIComponent(labName)}/${encodeURIComponent(action)}`, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
            },
            body: bodyPayload ? JSON.stringify(bodyPayload) : null,
        });
        if (!response.ok) {
            const payload = await response.text();
            throw new Error(payload || `HTTP ${response.status}`);
        }
        showToastMessage(successTitle, `${labName} (${vmId})`, false);
        endUserActivity(trackedActivityId, "ok", `${action} terminé`);
        await refreshState();
        if (currentView === "admin" && isAdmin()) {
            void refreshAdminPanel({ force: true, silent: true });
        }
    } catch (error) {
        endUserActivity(trackedActivityId, "error", error?.message || "Erreur inconnue");
        showToastMessage(errorTitle, error?.message || "Erreur inconnue", true);
    } finally {
        if (button instanceof HTMLButtonElement) {
            button.disabled = false;
            button.textContent = originalLabel || "Action";
        }
    }
}

function showSshKeysFeedback(message, tone = "neutral") {
    const node = document.getElementById("ssh-keys-feedback");
    if (!(node instanceof HTMLElement)) {
        return;
    }
    node.textContent = message;
    node.className = `text-[11px] ${tone === "error" ? "text-rose-300" : tone === "success" ? "text-emerald-300" : "text-slate-400"}`;
    node.classList.remove("hidden");
}

async function loadMySshKeys() {
    const input = document.getElementById("ssh-keys-input");
    if (!(input instanceof HTMLTextAreaElement)) {
        return;
    }

    if (!currentUser) {
        input.value = "";
        return;
    }

    showSshKeysFeedback("Chargement des clés SSH...", "neutral");
    try {
        const response = await apiFetch("/api/auth/ssh-keys");
        if (!response.ok) {
            const payloadText = await response.text();
            let detail = payloadText || `HTTP ${response.status}`;
            try {
                detail = JSON.parse(payloadText)?.detail || detail;
            } catch (_) {
            }
            throw new Error(detail);
        }
        const payload = await response.json();
        input.value = Array.isArray(payload.keys) ? payload.keys.join("\n") : "";
        showSshKeysFeedback("Clés SSH chargées.", "success");
    } catch (error) {
        showSshKeysFeedback(error?.message || "Chargement des clés SSH impossible.", "error");
    }
}


async function refreshTrackedJobStates({ force = false } = {}) {
    if (!currentUser) {
        return;
    }

    const now = Date.now();
    if (trackedJobRefreshInFlight) {
        return;
    }
    if (!force && now - trackedJobLastRefreshAt < TRACKED_JOB_REFRESH_MS) {
        return;
    }

    trackedJobRefreshInFlight = true;
    trackedJobLastRefreshAt = now;
    let changed = false;

    const reconcileMap = async (type, activityPrefix, stateMap, endpointBuilder, buildOutcomeDetails) => {
        const entries = Array.from(stateMap.entries());
        for (const [labKey, state] of entries) {
            if (!state?.inProgress || !state?.jobId) {
                continue;
            }

            const [vmId, labName] = String(labKey || "").split("::");
            if (!vmId || !labName) {
                continue;
            }

            const startedTs = Date.parse(String(state.startedAt || ""));
            if (Number.isFinite(startedTs) && now - startedTs > TRACKED_JOB_MAX_AGE_MS) {
                const completedAt = new Date().toISOString();
                _setLabActionState(type, labKey, {
                    inProgress: false,
                    completedAt,
                    jobId: null,
                    startedAt: state.startedAt || null,
                });
                _saveCompletedAt(type, labKey, completedAt);
                endUserActivity(`${activityPrefix}:${labKey}`, "error", "Action arrêtée automatiquement (timeout de suivi)");
                changed = true;
                continue;
            }

            try {
                const resp = await apiFetch(endpointBuilder(vmId, labName, state.jobId));
                if (!resp.ok) {
                    if (resp.status === 404 || resp.status === 410) {
                        const completedAt = new Date().toISOString();
                        _setLabActionState(type, labKey, {
                            inProgress: false,
                            completedAt,
                            jobId: null,
                            startedAt: state.startedAt || null,
                        });
                        _saveCompletedAt(type, labKey, completedAt);
                        endUserActivity(`${activityPrefix}:${labKey}`, "error", "Action arrêtée (job introuvable)");
                        changed = true;
                    } else {
                        const isGatewayUnavailable = resp.status === 502 || resp.status === 503 || resp.status === 504;
                        if (isGatewayUnavailable) {
                            const completedAt = new Date().toISOString();
                            _setLabActionState(type, labKey, {
                                inProgress: false,
                                completedAt,
                                jobId: null,
                                startedAt: state.startedAt || null,
                            });
                            _saveCompletedAt(type, labKey, completedAt);
                            endUserActivity(`${activityPrefix}:${labKey}`, "error", "Action arrêtée (agent indisponible)");
                            changed = true;
                            continue;
                        }

                        // If backend/agent stays unreachable for too long, avoid infinite "in progress" lock.
                        const fallbackStartTs = Date.parse(String(state.startedAt || ""));
                        const tooOldWithErrors = Number.isFinite(fallbackStartTs)
                            ? (now - fallbackStartTs > TRACKED_JOB_ERROR_STALE_MS)
                            : true;
                        if (tooOldWithErrors) {
                            const completedAt = new Date().toISOString();
                            _setLabActionState(type, labKey, {
                                inProgress: false,
                                completedAt,
                                jobId: null,
                                startedAt: state.startedAt || null,
                            });
                            _saveCompletedAt(type, labKey, completedAt);
                            endUserActivity(`${activityPrefix}:${labKey}`, "error", "Action arrêtée (agent injoignable)");
                            changed = true;
                        }
                    }
                    continue;
                }
                const job = await resp.json();
                const status = String(job?.status || "").toLowerCase();
                if (status !== "completed" && status !== "error") {
                    continue;
                }

                const completedAt = new Date().toISOString();
                _setLabActionState(type, labKey, {
                    inProgress: false,
                    completedAt,
                    jobId: null,
                    startedAt: state.startedAt || null,
                });
                _saveCompletedAt(type, labKey, completedAt);

                const isOk = job?.ok === true;
                const outcome = status === "error" || !isOk ? "error" : "ok";
                endUserActivity(`${activityPrefix}:${labKey}`, outcome, buildOutcomeDetails(job));
                changed = true;
            } catch (_) {
                // Ignore transient polling errors; next refresh cycle will retry.
            }
        }
    };

    try {
        await reconcileMap(
            "reconfig",
            "reconfigure",
            labReconfigureState,
            (vmId, labName, jobId) =>
                `/api/vms/${encodeURIComponent(vmId)}/labs/${encodeURIComponent(labName)}/reconfigure-job/${encodeURIComponent(jobId)}`,
            (job) => {
                if (job?.error) return String(job.error);
                const successCount = Number(job?.success_count || 0);
                const total = Number(job?.total || 0);
                return `${successCount}/${total} équipement(s) reconfiguré(s)`;
            },
        );

        await reconcileMap(
            "setdefault",
            "set-default",
            labSetDefaultState,
            (vmId, labName, jobId) =>
                `/api/vms/${encodeURIComponent(vmId)}/labs/${encodeURIComponent(labName)}/set-default-job/${encodeURIComponent(jobId)}`,
            (job) => {
                if (job?.error) return String(job.error);
                const savedCount = Number(job?.saved_count || 0);
                const total = Number(job?.total || 0);
                return `${savedCount}/${total} équipement(s) sauvegardé(s)`;
            },
        );
    } finally {
        trackedJobRefreshInFlight = false;
    }

    if (changed) {
        renderState(latestStateItems);
        renderActivityRail();
    }
}











document.addEventListener("toggle", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLDetailsElement)) {
        return;
    }
    if (!target.matches("details[data-lab-key]")) {
        return;
    }

    if (!hasOpenRouterPanel() && latestStateItems.length) {
        renderState(latestStateItems);
    }
});

document.addEventListener("submit", async (event) => {
    const target = event.target;
    if (!(target instanceof HTMLFormElement)) {
        return;
    }
    if (target.id !== "lab-request-form") {
        return;
    }

    event.preventDefault();
    if (!currentUser) {
        return;
    }

    const submitButton = document.getElementById("lab-request-submit");
    if (submitButton instanceof HTMLButtonElement) {
        submitButton.disabled = true;
    }

    const formData = new FormData(target);
    const payload = {
        suggestion_title: String(formData.get("suggestion_title") || "").trim(),
        need_details: String(formData.get("need_details") || "").trim(),
        resources_requirements: String(formData.get("resources_requirements") || "").trim(),
        requested_router_images: String(formData.get("requested_router_images") || "").trim(),
    };

    try {
        const response = await apiFetch("/api/lab-requests", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        if (!response.ok) {
            const payloadText = await response.text();
            let detail = payloadText || `HTTP ${response.status}`;
            try {
                detail = JSON.parse(payloadText)?.detail || detail;
            } catch (_) {
            }
            throw new Error(_extractApiErrorDetail(detail));
        }

        target.reset();
        await refreshLabRequests({ force: true, silent: true });
        showToastMessage("Demande envoyee", "Votre demande LAB a ete transmise aux administrateurs.");
    } catch (error) {
        showToastMessage("Envoi impossible", error?.message || "Erreur inconnue", true);
    } finally {
        if (submitButton instanceof HTMLButtonElement) {
            submitButton.disabled = false;
        }
    }
});

document.addEventListener("click", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLElement)) {
        return;
    }

    const toggleVmMetricsButton = target.closest("button[data-action='toggle-vm-metrics']");
    if (toggleVmMetricsButton instanceof HTMLButtonElement) {
        const vmId = String(toggleVmMetricsButton.dataset.vmId || "");
        if (!vmId) {
            return;
        }
        _toggleVmMetrics(vmId);
        return;
    }

    const claimLabRequestButton = target.closest("button[data-action='lab-request-claim']");
    if (claimLabRequestButton instanceof HTMLButtonElement) {
        const requestId = claimLabRequestButton.getAttribute("data-request-id") || "";
        if (!requestId) {
            return;
        }

        claimLabRequestButton.disabled = true;
        apiFetch(`/api/admin/lab-requests/${encodeURIComponent(requestId)}/claim`, {
            method: "POST",
        })
            .then(async (response) => {
                if (!response.ok) {
                    const payloadText = await response.text();
                    let detail = payloadText || `HTTP ${response.status}`;
                    try {
                        detail = JSON.parse(payloadText)?.detail || detail;
                    } catch (_) {
                    }
                    throw new Error(_extractApiErrorDetail(detail));
                }
                return response.json();
            })
            .then(async () => {
                await refreshLabRequests({ force: true, silent: true });
                showToastMessage("Demande prise en charge", "La demande est maintenant assignee.");
            })
            .catch((error) => {
                showToastMessage("Action impossible", error?.message || "Erreur inconnue", true);
            })
            .finally(() => {
                claimLabRequestButton.disabled = false;
            });
        return;
    }

    const saveLabRequestButton = target.closest("button[data-action='lab-request-save']");
    if (saveLabRequestButton instanceof HTMLButtonElement) {
        const requestId = saveLabRequestButton.getAttribute("data-request-id") || "";
        const card = saveLabRequestButton.closest("article[data-lab-request-id]");
        if (!requestId || !(card instanceof HTMLElement)) {
            return;
        }

        const statusNode = card.querySelector("select[data-field='lab-request-status']");
        const responseNode = card.querySelector("textarea[data-field='lab-request-response']");
        const status = statusNode instanceof HTMLSelectElement ? statusNode.value : "";
        const adminResponse = responseNode instanceof HTMLTextAreaElement ? responseNode.value : "";

        saveLabRequestButton.disabled = true;
        apiFetch(`/api/admin/lab-requests/${encodeURIComponent(requestId)}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ status, admin_response: adminResponse }),
        })
            .then(async (response) => {
                if (!response.ok) {
                    const payloadText = await response.text();
                    let detail = payloadText || `HTTP ${response.status}`;
                    try {
                        detail = JSON.parse(payloadText)?.detail || detail;
                    } catch (_) {
                    }
                    throw new Error(_extractApiErrorDetail(detail));
                }
                return response.json();
            })
            .then(async () => {
                await refreshLabRequests({ force: true, silent: true });
                showToastMessage("Demande mise a jour", "Le statut de la demande a ete enregistre.");
            })
            .catch((error) => {
                showToastMessage("Mise a jour impossible", error?.message || "Erreur inconnue", true);
            })
            .finally(() => {
                saveLabRequestButton.disabled = false;
            });
        return;
    }

    const deleteLabRequestButton = target.closest("button[data-action='lab-request-delete']");
    if (deleteLabRequestButton instanceof HTMLButtonElement) {
        const requestId = deleteLabRequestButton.getAttribute("data-request-id") || "";
        if (!requestId) {
            return;
        }

        const confirmed = window.confirm("Supprimer cette demande utilisateur ?");
        if (!confirmed) {
            return;
        }

        deleteLabRequestButton.disabled = true;
        apiFetch(`/api/admin/lab-requests/${encodeURIComponent(requestId)}`, {
            method: "DELETE",
        })
            .then(async (response) => {
                if (!response.ok) {
                    const payloadText = await response.text();
                    let detail = payloadText || `HTTP ${response.status}`;
                    try {
                        detail = JSON.parse(payloadText)?.detail || detail;
                    } catch (_) {
                    }
                    throw new Error(_extractApiErrorDetail(detail));
                }
                return response.json();
            })
            .then(async () => {
                await refreshLabRequests({ force: true, silent: true });
                showToastMessage("Demande supprimee", "La demande utilisateur a ete retiree.");
            })
            .catch((error) => {
                showToastMessage("Suppression impossible", error?.message || "Erreur inconnue", true);
            })
            .finally(() => {
                deleteLabRequestButton.disabled = false;
            });
        return;
    }

    const openUserAuditButton = target.closest("button[data-action='open-user-audit-modal']");
    if (openUserAuditButton instanceof HTMLButtonElement) {
        const username = openUserAuditButton.getAttribute("data-username") || "";
        openUserAuditModal(username);
        return;
    }

    const openSystemAuditButton = target.closest("button[data-action='open-system-audit-modal']");
    if (openSystemAuditButton instanceof HTMLButtonElement) {
        openSystemAuditModal();
        return;
    }

    const openVmAuditButton = target.closest("button[data-action='open-vm-audit-modal']");
    if (openVmAuditButton instanceof HTMLButtonElement) {
        const vmId = openVmAuditButton.getAttribute("data-vm-id") || "";
        openVmAuditModal(vmId);
        return;
    }

    const toggleDocFullscreenButton = target.closest("button[data-action='toggle-doc-fullscreen']");
    if (toggleDocFullscreenButton instanceof HTMLButtonElement) {
        const card = toggleDocFullscreenButton.closest("article[data-doc-key]");
        if (card instanceof HTMLElement) {
            toggleDocumentationFullscreen(card);
        }
        return;
    }

    const adminEditButton = target.closest("button[data-action='admin-edit-user']");
    if (adminEditButton instanceof HTMLButtonElement) {
        const user = {
            username: adminEditButton.getAttribute("data-username") || "",
            full_name: adminEditButton.getAttribute("data-full-name") || "",
            email: adminEditButton.getAttribute("data-email") || "",
            role: adminEditButton.getAttribute("data-role") || "user",
            active: (adminEditButton.getAttribute("data-active") || "false") === "true",
        };
        showEditUserModal(user);
        return;
    }

    const adminResetPasswordButton = target.closest("button[data-action='admin-reset-password']");
    if (adminResetPasswordButton instanceof HTMLButtonElement) {
        const username = adminResetPasswordButton.getAttribute("data-username") || "";
        if (!username) {
            return;
        }
        showResetPasswordModal(username);
        return;
    }

    const adminToggleUserButton = target.closest("button[data-action='admin-toggle-user']");
    if (adminToggleUserButton instanceof HTMLButtonElement) {
        const username = adminToggleUserButton.getAttribute("data-username") || "";
        const active = (adminToggleUserButton.getAttribute("data-active") || "false") === "true";
        if (!username) {
            return;
        }
        if (isCurrentUser(username)) {
            showToastMessage("Action admin bloquée", "Votre propre compte admin ne peut pas être désactivé depuis l’interface.", true);
            return;
        }

        const confirmed = window.confirm(`${active ? "Désactiver" : "Activer"} le compte ${username} ?`);
        if (!confirmed) {
            return;
        }

        submitAdminUserUpdate(username, { active: !active })
            .then(() => refreshAdminPanel())
            .catch((error) => {
                showToastMessage("Action admin impossible", error?.message || "Erreur inconnue", true);
            });
        return;
    }

    const adminDeleteUserButton = target.closest("button[data-action='admin-delete-user']");
    if (adminDeleteUserButton instanceof HTMLButtonElement) {
        const username = adminDeleteUserButton.getAttribute("data-username") || "";
        if (!username) {
            return;
        }
        if (isCurrentUser(username)) {
            showToastMessage("Suppression impossible", "Votre propre compte admin ne peut pas être supprimé depuis l’interface.", true);
            return;
        }

        const confirmed = window.confirm(`Supprimer définitivement le compte ${username} ?`);
        if (!confirmed) {
            return;
        }

        apiFetch(`/api/admin/users/${encodeURIComponent(username)}`, {
            method: "DELETE",
        })
            .then(async (response) => {
                if (!response.ok) {
                    const payloadText = await response.text();
                    let detail = payloadText || `HTTP ${response.status}`;
                    try {
                        detail = JSON.parse(payloadText)?.detail || detail;
                    } catch (_) {
                    }
                    throw new Error(detail);
                }
                return response.json();
            })
            .then(() => {
                refreshAdminPanel();
                showToastMessage("Utilisateur supprimé", `${username} a été supprimé.`);
            })
            .catch((error) => {
                showToastMessage("Suppression impossible", error?.message || "Erreur inconnue", true);
            });
        return;
    }

    const adminEditGroupButton = target.closest("button[data-action='admin-edit-group']");
    if (adminEditGroupButton instanceof HTMLButtonElement) {
        const groupName = adminEditGroupButton.getAttribute("data-group-name") || "";
        const group = latestAdminGroups.find((item) => item.name === groupName);
        if (group) {
            showGroupFormModal(group);
        }
        return;
    }

    const adminDeleteGroupButton = target.closest("button[data-action='admin-delete-group']");
    if (adminDeleteGroupButton instanceof HTMLButtonElement) {
        const groupName = adminDeleteGroupButton.getAttribute("data-group-name") || "";
        if (!groupName) {
            return;
        }

        const confirmed = window.confirm(`Supprimer définitivement le groupe ${groupName} ?`);
        if (!confirmed) {
            return;
        }

        apiFetch(`/api/admin/groups/${encodeURIComponent(groupName)}`, {
            method: "DELETE",
        })
            .then(async (response) => {
                if (!response.ok) {
                    const payloadText = await response.text();
                    let detail = payloadText || `HTTP ${response.status}`;
                    try {
                        detail = JSON.parse(payloadText)?.detail || detail;
                    } catch (_) {
                    }
                    throw new Error(detail);
                }
                return response.json();
            })
            .then(() => {
                refreshAdminPanel();
                showToastMessage("Groupe supprimé", `${groupName} a été supprimé.`);
            })
            .catch((error) => {
                showToastMessage("Suppression impossible", error?.message || "Erreur inconnue", true);
            });
        return;
    }

    const adminProvisionVMButton = target.closest("button[data-action='admin-provision-vm']");
    if (adminProvisionVMButton instanceof HTMLButtonElement) {
        const vmId = adminProvisionVMButton.getAttribute("data-vm-id") || "";
        const vm = latestAdminVMs.find((item) => String(item.id || "") === vmId);
        if (vm) {
            showProvisionModal(vm);
        }
        return;
    }

    const adminEditVMButton = target.closest("button[data-action='admin-edit-vm']");
    if (adminEditVMButton instanceof HTMLButtonElement) {
        const vmId = adminEditVMButton.getAttribute("data-vm-id") || "";
        const vm = latestAdminVMs.find((item) => String(item.id || "") === vmId);
        if (vm) {
            showVMFormModal(vm);
        }
        return;
    }

    const adminDeleteVMButton = target.closest("button[data-action='admin-delete-vm']");
    if (adminDeleteVMButton instanceof HTMLButtonElement) {
        const vmId = adminDeleteVMButton.getAttribute("data-vm-id") || "";
        if (!vmId) {
            return;
        }

        const confirmed = window.confirm(`Supprimer définitivement la VM ${vmId} ?`);
        if (!confirmed) {
            return;
        }

        apiFetch(`/api/admin/vms/${encodeURIComponent(vmId)}`, {
            method: "DELETE",
        })
            .then(async (response) => {
                if (!response.ok) {
                    const payloadText = await response.text();
                    let detail = payloadText || `HTTP ${response.status}`;
                    try {
                        detail = JSON.parse(payloadText)?.detail || detail;
                    } catch (_) {
                    }
                    throw new Error(detail);
                }
                return response.json();
            })
            .then(() => {
                refreshAdminPanel();
                showToastMessage("VM supprimée", `${vmId} a été supprimée.`);
            })
            .catch((error) => {
                showToastMessage("Suppression impossible", error?.message || "Erreur inconnue", true);
            });
        return;
    }

    const toggleSandboxButton = target.closest("button[data-action='toggle-sandbox-lab']");
    if (toggleSandboxButton instanceof HTMLButtonElement) {
        const vmId = toggleSandboxButton.getAttribute("data-vm-id") || "";
        const labName = toggleSandboxButton.getAttribute("data-lab-name") || "";
        const enabledNow = String(toggleSandboxButton.getAttribute("data-enabled") || "") === "true";
        if (!vmId || !labName) {
            return;
        }

        toggleSandboxButton.disabled = true;
        apiFetch(`/api/admin/vms/${encodeURIComponent(vmId)}/labs/${encodeURIComponent(labName)}/sandbox`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ enabled: !enabledNow }),
        })
            .then(async (response) => {
                if (!response.ok) {
                    const txt = await response.text();
                    let detail = txt || `HTTP ${response.status}`;
                    try { detail = JSON.parse(txt)?.detail || detail; } catch (_) {}
                    throw new Error(_extractApiErrorDetail(detail));
                }
                return response.json();
            })
            .then(async () => {
                await refreshState();
                showToastMessage("Mode sandbox", `${labName}: ${enabledNow ? "désactivé" : "activé"}.`);
            })
            .catch((error) => {
                showToastMessage("Mise à jour impossible", error?.message || "Erreur inconnue", true);
            })
            .finally(() => {
                toggleSandboxButton.disabled = false;
            });
        return;
    }

    const toggleLabLockButton = target.closest("button[data-action='toggle-lab-lock']");
    if (toggleLabLockButton instanceof HTMLButtonElement) {
        const vmId = toggleLabLockButton.getAttribute("data-vm-id") || "";
        const labName = toggleLabLockButton.getAttribute("data-lab-name") || "";
        const lockedNow = String(toggleLabLockButton.getAttribute("data-locked") || "") === "true";
        if (!vmId || !labName) {
            return;
        }

        toggleLabLockButton.disabled = true;
        apiFetch(`/api/admin/vms/${encodeURIComponent(vmId)}/labs/${encodeURIComponent(labName)}/lock`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ locked: !lockedNow }),
        })
            .then(async (response) => {
                if (!response.ok) {
                    const txt = await response.text();
                    let detail = txt || `HTTP ${response.status}`;
                    try { detail = JSON.parse(txt)?.detail || detail; } catch (_) {}
                    throw new Error(_extractApiErrorDetail(detail));
                }
                return response.json();
            })
            .then(async () => {
                await refreshState();
                showToastMessage("Lab lock", `${labName}: ${lockedNow ? "déverrouillé" : "verrouillé"}.`);
            })
            .catch((error) => {
                showToastMessage("Mise à jour impossible", error?.message || "Erreur inconnue", true);
            })
            .finally(() => {
                toggleLabLockButton.disabled = false;
            });
        return;
    }

    const editSandboxButton = target.closest("button[data-action='edit-sandbox-yaml']");
    if (editSandboxButton instanceof HTMLButtonElement) {
        const vmId = editSandboxButton.getAttribute("data-vm-id") || "";
        const labName = editSandboxButton.getAttribute("data-lab-name") || "";
        if (!vmId || !labName) {
            return;
        }
        showSandboxYamlModal(vmId, labName);
        return;
    }

    const button = target.closest("button[data-action='show-graph']");
    if (button instanceof HTMLButtonElement) {
        const labKey = button.getAttribute("data-lab-key");
        if (!labKey) {
            return;
        }

        const found = findLabByKey(labKey);
        if (!found) {
            return;
        }

        selectedGraph = { labKey, labName: found.lab.name, signature: graphSignature(found.lab) };
        renderGraphPanel(found.lab);
        return;
    }

    const openDocButton = target.closest("button[data-action='open-lab-doc']");
    if (openDocButton instanceof HTMLButtonElement) {
        const vmId = openDocButton.getAttribute("data-vm-id");
        const labName = openDocButton.getAttribute("data-lab-name");
        if (!vmId || !labName) {
            return;
        }
        openDocumentationForLab(vmId, labName);
        return;
    }

    const saveGeneralDocButton = target.closest("button[data-action='save-general-doc']");
    if (saveGeneralDocButton instanceof HTMLButtonElement) {
        const titleInput = document.getElementById("general-section-title-input");
        const title = titleInput instanceof HTMLInputElement ? titleInput.value.trim() : "";
        const quillEditor = documentationEditors.get("__general__");
        const text = quillEditor ? quillEditor.root.innerHTML : "";

        saveGeneralDocButton.disabled = true;
        apiFetch("/api/docs/general", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ title, text }),
        })
            .then(async (response) => {
                if (!response.ok) {
                    const payloadText = await response.text();
                    let detail = payloadText || `HTTP ${response.status}`;
                    try { detail = JSON.parse(payloadText)?.detail || detail; } catch (_) {}
                    throw new Error(detail);
                }
                return response.json();
            })
            .then((payload) => {
                latestGeneralDocSection = payload.section || null;
                showToastMessage("Section enregistrée", title ? `« ${title} » mis à jour.` : "Section générale mise à jour.");
            })
            .catch((error) => {
                showToastMessage("Enregistrement impossible", error?.message || "Erreur inconnue", true);
            })
            .finally(() => {
                saveGeneralDocButton.disabled = false;
            });
        return;
    }

    const saveDocButton = target.closest("button[data-action='save-lab-doc']");
    if (saveDocButton instanceof HTMLButtonElement) {
        const vmId = saveDocButton.getAttribute("data-vm-id");
        const labName = saveDocButton.getAttribute("data-lab-name");
        if (!vmId || !labName) {
            return;
        }

        const card = saveDocButton.closest("article[data-doc-key]");
        if (!(card instanceof HTMLElement)) {
            return;
        }
        const docKey = makeDocumentationKey(vmId, labName);
        const quillEditor = documentationEditors.get(docKey);
        const diagramField = card.querySelector("textarea[data-doc-field='diagram']");
        const text = quillEditor ? quillEditor.root.innerHTML : "";
        const diagram = diagramField instanceof HTMLTextAreaElement ? diagramField.value : "";

        saveDocButton.disabled = true;
        apiFetch(`/api/docs/${encodeURIComponent(vmId)}/labs/${encodeURIComponent(labName)}`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ text, diagram }),
        })
            .then(async (response) => {
                if (!response.ok) {
                    const payloadText = await response.text();
                    let detail = payloadText || `HTTP ${response.status}`;
                    try {
                        detail = JSON.parse(payloadText)?.detail || detail;
                    } catch (_) {
                    }
                    throw new Error(detail);
                }
                return response.json();
            })
            .then(() => {
                showToastMessage("Documentation enregistrée", `${labName} mis à jour.`);
                return refreshDocumentationPanel();
            })
            .catch((error) => {
                showToastMessage("Enregistrement impossible", error?.message || "Erreur inconnue", true);
            })
            .finally(() => {
                saveDocButton.disabled = false;
            });
        return;
    }

    const uploadPdfButton = target.closest("button[data-action='upload-lab-doc-pdf']");
    if (uploadPdfButton instanceof HTMLButtonElement) {
        const vmId = uploadPdfButton.getAttribute("data-vm-id");
        const labName = uploadPdfButton.getAttribute("data-lab-name");
        if (!vmId || !labName) {
            return;
        }

        const card = uploadPdfButton.closest("article[data-doc-key]");
        if (!(card instanceof HTMLElement)) {
            return;
        }
        const fileInput = card.querySelector("input[data-doc-field='pdf']");
        if (!(fileInput instanceof HTMLInputElement) || !fileInput.files || fileInput.files.length === 0) {
            showToastMessage("Upload impossible", "Sélectionnez un PDF avant upload.", true);
            return;
        }

        const file = fileInput.files[0];
        const formData = new FormData();
        formData.append("file", file);

        uploadPdfButton.disabled = true;
        apiFetch(`/api/docs/${encodeURIComponent(vmId)}/labs/${encodeURIComponent(labName)}/pdf`, {
            method: "POST",
            body: formData,
        })
            .then(async (response) => {
                if (!response.ok) {
                    const payloadText = await response.text();
                    let detail = payloadText || `HTTP ${response.status}`;
                    try {
                        detail = JSON.parse(payloadText)?.detail || detail;
                    } catch (_) {
                    }
                    throw new Error(detail);
                }
                return response.json();
            })
            .then(() => {
                showToastMessage("PDF uploadé", `${labName}: document mis à jour.`);
                return refreshDocumentationPanel();
            })
            .catch((error) => {
                showToastMessage("Upload impossible", error?.message || "Erreur inconnue", true);
            })
            .finally(() => {
                uploadPdfButton.disabled = false;
            });
        return;
    }

    const deletePdfButton = target.closest("button[data-action='delete-lab-doc-pdf']");
    if (deletePdfButton instanceof HTMLButtonElement) {
        const vmId = deletePdfButton.getAttribute("data-vm-id");
        const labName = deletePdfButton.getAttribute("data-lab-name");
        if (!vmId || !labName) {
            return;
        }
        const confirmed = window.confirm(`Supprimer le PDF de documentation pour ${labName} ?`);
        if (!confirmed) {
            return;
        }

        deletePdfButton.disabled = true;
        apiFetch(`/api/docs/${encodeURIComponent(vmId)}/labs/${encodeURIComponent(labName)}/pdf`, {
            method: "DELETE",
        })
            .then(async (response) => {
                if (!response.ok) {
                    const payloadText = await response.text();
                    let detail = payloadText || `HTTP ${response.status}`;
                    try {
                        detail = JSON.parse(payloadText)?.detail || detail;
                    } catch (_) {
                    }
                    throw new Error(detail);
                }
                return response.json();
            })
            .then(() => {
                showToastMessage("PDF supprimé", `${labName}: document retiré.`);
                return refreshDocumentationPanel();
            })
            .catch((error) => {
                showToastMessage("Suppression impossible", error?.message || "Erreur inconnue", true);
            })
            .finally(() => {
                deletePdfButton.disabled = false;
            });
        return;
    }


    const configDiffButton = target.closest("button[data-action='config-diff']");
    if (configDiffButton instanceof HTMLButtonElement) {
        const vmId = configDiffButton.getAttribute("data-vm-id");
        const labName = configDiffButton.getAttribute("data-lab-name");
        const routersJson = configDiffButton.getAttribute("data-routers") || "[]";
        if (!vmId || !labName) {
            return;
        }
        let routers = [];
        try { routers = JSON.parse(routersJson); } catch (_) {}
        showConfigDiffSelectModal(vmId, labName, routers);
        return;
    }

    const reconfigureButton = target.closest("button[data-action='reconfigure-lab']");
    if (reconfigureButton instanceof HTMLButtonElement) {
        const vmId = reconfigureButton.getAttribute("data-vm-id");
        const labName = reconfigureButton.getAttribute("data-lab-name");
        if (!vmId || !labName) {
            return;
        }

        const labKey = findLabKey(vmId, labName);
        const currentState = labReconfigureState.get(labKey) || {};
        if (currentState.inProgress) {
            if (currentState.jobId) {
                showReconfigureProgressModal(vmId, labName, currentState.jobId, labKey);
            }
            return;
        }

        // Find the lab in latestStateItems to get routers list
        let lab = null;
        for (const vm of latestStateItems) {
            if (vm.id === vmId) {
                lab = vm.labs.find((l) => l.name === labName);
                break;
            }
        }

        if (!lab || !Array.isArray(lab.routers) || lab.routers.length === 0) {
            showToastMessage("Erreur", "Impossible de trouver les équipements du LAB.", true);
            return;
        }

        showReconfigureSelectModal(vmId, labName, labKey, lab.routers);
        return;
    }

    const exportButton = target.closest("button[data-action='export-config']");
    if (exportButton instanceof HTMLButtonElement) {
        const vmId = exportButton.getAttribute("data-vm-id");
        const labName = exportButton.getAttribute("data-lab-name");
        if (!vmId || !labName) {
            return;
        }

        const labKey = findLabKey(vmId, labName);
        const currentState = labReconfigureState.get(labKey) || {};
        if (currentState.inProgress) {
            showToastMessage("Export indisponible", "Reconfiguration en cours, veuillez patienter.", true);
            return;
        }

        const activityId = `export-config:${labKey}:${Date.now()}`;
        beginUserActivity({
            id: activityId,
            title: "Export conf",
            target: `${labName} @ ${vmId}`,
            details: "Preparation du ZIP...",
        });
        setActivityRailOpen(true);
        renderActivityRail();

        exportButton.disabled = true;
        exportButton.textContent = "Export...";

        downloadFile(
            `/api/vms/${encodeURIComponent(vmId)}/labs/${encodeURIComponent(labName)}/export-config`,
            `${labName}-configs.zip`,
        )
            .then(() => {
                endUserActivity(activityId, "ok", "Archive telechargee");
            })
            .catch((error) => {
                endUserActivity(activityId, "error", error?.message || "Erreur inconnue");
                showToastMessage("Export impossible", error?.message || "Erreur inconnue", true);
            })
            .finally(() => {
                exportButton.disabled = false;
                exportButton.textContent = "Export conf";
                renderActivityRail();
            });
        return;
    }

    const exportYamlButton = target.closest("button[data-action='export-yaml']");
    if (exportYamlButton instanceof HTMLButtonElement) {
        const vmId = exportYamlButton.getAttribute("data-vm-id");
        const labName = exportYamlButton.getAttribute("data-lab-name");
        if (!vmId || !labName) {
            return;
        }

        const labKey = findLabKey(vmId, labName);
        const currentState = labReconfigureState.get(labKey) || {};
        if (currentState.inProgress) {
            showToastMessage("Action indisponible", "Reconfiguration en cours: Export YAML temporairement indisponible.", true);
            return;
        }

        downloadFile(
            `/api/vms/${encodeURIComponent(vmId)}/labs/${encodeURIComponent(labName)}/export-yaml`,
            `${labName}.yaml`,
        ).catch((error) => {
            showToastMessage("Export impossible", error?.message || "Erreur inconnue", true);
        });
        return;
    }

    const reserveButton = target.closest("button[data-action='reserve-lab']");
    if (reserveButton instanceof HTMLButtonElement) {
        const vmId = reserveButton.getAttribute("data-vm-id");
        const labName = reserveButton.getAttribute("data-lab-name");
        if (!vmId || !labName) {
            return;
        }

        showReservationModal(vmId, labName);
        return;
    }

    const releaseButton = target.closest("button[data-action='release-reservation']");
    if (releaseButton instanceof HTMLButtonElement) {
        const vmId = releaseButton.getAttribute("data-vm-id");
        const labName = releaseButton.getAttribute("data-lab-name");
        if (!vmId || !labName) {
            return;
        }

        const confirmed = window.confirm(`Libérer la réservation du LAB ${labName} ?`);
        if (!confirmed) {
            return;
        }

        releaseButton.disabled = true;
        const originalReleaseLabel = releaseButton.textContent;
        releaseButton.textContent = "Libération...";
        const releaseTrace = showSandboxTraceModal(
            `Journal libération sandbox · ${labName}`,
            [{ name: "request", ok: true, message: "Libération en cours..." }],
            "Opération en cours sur le vm-agent."
        );

        apiFetch(`/api/vms/${encodeURIComponent(vmId)}/labs/${encodeURIComponent(labName)}/reservation`, {
            method: "DELETE",
        })
            .then(async (response) => {
                if (!response.ok) {
                    const payloadText = await response.text();
                    let detail = payloadText || `HTTP ${response.status}`;
                    let parsedDetail = null;
                    try {
                        const parsed = JSON.parse(payloadText);
                        parsedDetail = parsed?.detail ?? parsed;
                        detail = _extractApiErrorDetail(parsedDetail);
                    } catch (_) {
                    }
                    const error = new Error(detail);
                    error.sandboxSteps = parsedDetail?.steps || [];
                    throw error;
                }
                return response.json();
            })
            .then((payload) => {
                const hasReset = Boolean(payload?.sandbox_reset);
                const resetSteps = payload?.sandbox_reset?.steps || [];
                if (hasReset) {
                    showToastMessage("LAB libéré", "Réservation libérée et remise à zéro sandbox terminée.");
                    if (Array.isArray(resetSteps) && resetSteps.length) {
                        releaseTrace?.setSubtitle("Terminé. Étapes exécutées sur le vm-agent.");
                        releaseTrace?.setSteps(resetSteps);
                    }
                } else {
                    releaseTrace?.setSubtitle("Terminé. Aucun reset sandbox requis.");
                    releaseTrace?.setSteps([{ name: "release", ok: true, message: "Libération traitée côté central (sans action vm-agent)." }]);
                }
                return refreshState();
            })
            .catch((error) => {
                showToastMessage("Libération impossible", error.message || "Erreur inconnue", true);
                if (Array.isArray(error?.sandboxSteps) && error.sandboxSteps.length) {
                    releaseTrace?.setSubtitle("Échec. Le détail ci-dessous montre les étapes exécutées avant l'erreur.");
                    releaseTrace?.setSteps(error.sandboxSteps);
                } else {
                    releaseTrace?.setSubtitle("Échec sans détail d'étapes renvoyé par l'API.");
                    releaseTrace?.setSteps([{ name: "release", ok: false, message: error.message || "Erreur inconnue" }]);
                }
            })
            .finally(() => {
                releaseButton.disabled = false;
                releaseButton.textContent = originalReleaseLabel;
            });
        return;
    }

    const extendButton = target.closest("button[data-action='extend-reservation']");
    if (extendButton instanceof HTMLButtonElement) {
        const vmId = extendButton.getAttribute("data-vm-id");
        const labName = extendButton.getAttribute("data-lab-name");
        if (!vmId || !labName) {
            return;
        }

        showReservationExtensionModal(vmId, labName);
        return;
    }

    const startFromInventoryButton = target.closest("button[data-action='start-lab-from-inventory']");
    const viewInventoryYamlButton = target.closest("button[data-action='view-inventory-yaml']");
    if (viewInventoryYamlButton instanceof HTMLButtonElement) {
        const vmId = viewInventoryYamlButton.getAttribute("data-vm-id");
        const labName = viewInventoryYamlButton.getAttribute("data-lab-name");
        const topologyFile = viewInventoryYamlButton.getAttribute("data-topology-file") || "";
        if (!vmId || !labName) {
            return;
        }
        showInventoryYamlModal(vmId, labName, topologyFile);
        return;
    }

    if (startFromInventoryButton instanceof HTMLButtonElement) {
        const vmId = startFromInventoryButton.getAttribute("data-vm-id");
        const labName = startFromInventoryButton.getAttribute("data-lab-name");
        const topologyFile = startFromInventoryButton.getAttribute("data-topology-file") || "";
        if (!vmId || !labName || !topologyFile) {
            return;
        }
        void performLabActionRequest({
            vmId,
            labName,
            topologyFile,
            action: "start",
            button: startFromInventoryButton,
            busyLabel: "Start...",
            successTitle: "LAB démarré",
            errorTitle: "Start échoué",
        });
        return;
    }

    const startButton = target.closest("button[data-action='start-lab']");
    if (startButton instanceof HTMLButtonElement) {
        const vmId = startButton.getAttribute("data-vm-id");
        const labName = startButton.getAttribute("data-lab-name");
        const topologyFile = startButton.getAttribute("data-topology-file") || "";
        if (!vmId || !labName) {
            return;
        }
        if ((startButton.getAttribute("data-lab-locked") || "") === "true" && !isAdmin()) {
            showToastMessage("Action bloquée", "Ce LAB est verrouillé. Seul un admin peut lancer cette action.", true);
            return;
        }

        const labKey = findLabKey(vmId, labName);
        const currentState = labReconfigureState.get(labKey) || {};
        if (currentState.inProgress) {
            showToastMessage("Start indisponible", "Reconfiguration en cours, veuillez patienter.", true);
            return;
        }

        void performLabActionRequest({
            vmId,
            labName,
            topologyFile,
            action: "start",
            button: startButton,
            busyLabel: "Start...",
            successTitle: "LAB démarré",
            errorTitle: "Start échoué",
        });
        return;
    }

    const stopButton = target.closest("button[data-action='stop-lab']");
    if (stopButton instanceof HTMLButtonElement) {
        const vmId = stopButton.getAttribute("data-vm-id");
        const labName = stopButton.getAttribute("data-lab-name");
        if (!vmId || !labName) {
            return;
        }
        if ((stopButton.getAttribute("data-lab-locked") || "") === "true" && !isAdmin()) {
            showToastMessage("Action bloquée", "Ce LAB est verrouillé. Seul un admin peut lancer cette action.", true);
            return;
        }

        const labKey = findLabKey(vmId, labName);
        const currentState = labReconfigureState.get(labKey) || {};
        if (currentState.inProgress) {
            showToastMessage("Stop indisponible", "Reconfiguration en cours, veuillez patienter.", true);
            return;
        }

        const confirmed = confirmDangerActionTwice("STOP", labName);
        if (!confirmed) {
            showToastMessage("Action annulée", "Stop non confirmé (NON par défaut).", false);
            return;
        }

        void performLabActionRequest({
            vmId,
            labName,
            action: "stop",
            button: stopButton,
            busyLabel: "Stop...",
            successTitle: "LAB stoppé",
            errorTitle: "Stop échoué",
        });
        return;
    }

    const redeployButton = target.closest("button[data-action='redeploy-lab']");
    if (!(redeployButton instanceof HTMLButtonElement)) {
        const setDefaultButton = target.closest("button[data-action='set-default-config']");
        if (setDefaultButton instanceof HTMLButtonElement) {
            const vmId = setDefaultButton.getAttribute("data-vm-id");
            const labName = setDefaultButton.getAttribute("data-lab-name");
            if (!vmId || !labName) {
                return;
            }

            const labKey = findLabKey(vmId, labName);
            const currentState = labSetDefaultState.get(labKey) || {};
            if (currentState.inProgress) {
                if (currentState.jobId) {
                    showSetDefaultProgressModal(vmId, labName, currentState.jobId, labKey);
                }
                return;
            }

            const confirmed = window.confirm(
                `Définir la configuration actuelle du LAB ${labName} comme configuration par défaut de reconfiguration ?\n\n` +
                `Si une config par défaut existe, elle sera sauvegardée en .tgz avant remplacement.`,
            );
            if (!confirmed) {
                return;
            }

            _setLabActionState("setdefault", labKey, { inProgress: true, completedAt: null, startedAt: new Date().toISOString(), jobId: null });
            beginUserActivity({
                id: `set-default:${labKey}`,
                title: "Set as default",
                target: `${labName} @ ${vmId}`,
                details: "Capture de la configuration en cours",
            });
            renderState(latestStateItems);
            renderActivityRail();

            setDefaultButton.disabled = true;
            setDefaultButton.textContent = "Saving...";

            apiFetch(`/api/vms/${encodeURIComponent(vmId)}/labs/${encodeURIComponent(labName)}/set-default-config`, {
                method: "POST",
            })
                .then(async (response) => {
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
                    return response.json();
                })
                .then((payload) => {
                    const jobId = payload?.job_id;
                    if (!jobId) {
                        throw new Error("Réponse invalide: job_id manquant");
                    }
                    _setLabActionState("setdefault", labKey, {
                        inProgress: true,
                        completedAt: null,
                        startedAt: new Date().toISOString(),
                        jobId,
                    });
                    showSetDefaultProgressModal(vmId, labName, jobId, labKey);
                })
                .catch((error) => {
                    _setLabActionState("setdefault", labKey, { inProgress: false, completedAt: null, jobId: null });
                    endUserActivity(`set-default:${labKey}`, "error", error?.message || "Erreur inconnue");
                    showToastMessage("Action impossible", error?.message || "Erreur inconnue", true);
                    renderState(latestStateItems);
                    renderActivityRail();
                })
                .finally(() => {
                    const latest = labSetDefaultState.get(labKey) || {};
                    if (!latest.inProgress) {
                        setDefaultButton.disabled = false;
                        setDefaultButton.textContent = "Set current as default";
                    }
                });
            return;
        }
        return;
    }

    const vmId = redeployButton.getAttribute("data-vm-id");
    const labName = redeployButton.getAttribute("data-lab-name");
    const topologyFile = redeployButton.getAttribute("data-topology-file") || "";
    if (!vmId || !labName) {
        return;
    }
    if ((redeployButton.getAttribute("data-lab-locked") || "") === "true" && !isAdmin()) {
        showToastMessage("Action bloquée", "Ce LAB est verrouillé. Seul un admin peut lancer cette action.", true);
        return;
    }

    const labKey = findLabKey(vmId, labName);
    const currentState = labReconfigureState.get(labKey) || {};
    if (currentState.inProgress) {
        showToastMessage("Redeploy indisponible", "Reconfiguration en cours, veuillez patienter.", true);
        return;
    }

    const confirmed = confirmDangerActionTwice("REDEPLOY", labName);
    if (!confirmed) {
        showToastMessage("Action annulée", "Redeploy non confirmé (NON par défaut).", false);
        return;
    }

    void performLabActionRequest({
        vmId,
        labName,
        topologyFile,
        action: "redeploy",
        button: redeployButton,
        busyLabel: "Redeploy...",
        successTitle: "Redeploy terminé",
        errorTitle: "Redeploy échoué",
    });
});

document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && documentationFullscreenCard) {
        exitDocumentationFullscreen();
    }
});

document.addEventListener("input", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLInputElement)) {
        return;
    }

    if (target.dataset.action === "admin-user-audit-search") {
        const username = String(target.dataset.username || "");
        adminAuditUserSearchQueries[username] = target.value;
        applyAdminAuditFilter();
        return;
    }

    if (target.dataset.action === "admin-system-audit-search") {
        adminSystemAuditSearchQuery = target.value;
        applyAdminAuditFilter();
    }
});

document.addEventListener("change", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLSelectElement)) {
        return;
    }

    if (target.dataset.action === "admin-user-audit-severity") {
        const username = String(target.dataset.username || "");
        adminAuditUserSeverityFilters[username] = target.value;
        applyAdminAuditFilter();
        return;
    }

    if (target.dataset.action === "admin-system-audit-severity") {
        adminSystemAuditSeverityFilter = target.value;
        applyAdminAuditFilter();
        return;
    }

    if (target.id === "admin-central-hours") {
        refreshCentralResourcesOnly();
        return;
    }

    if (target.id === "admin-vm-hours") {
        const nextHours = Number(target.value || 24);
        adminVmGraphHours = Number.isFinite(nextHours) ? nextHours : 24;
        refreshAdminVmCardsResources({ force: true });
    }
});

