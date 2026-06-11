function isAdmin() {
    return currentUser?.role === "admin";
}

function isGroupAdmin() {
    return currentUser?.role === "group-admin";
}

function canAccessAdminView() {
    return isAdmin() || isGroupAdmin();
}

function canEditDocumentation() {
    return isAdmin() || isGroupAdmin();
}

function canManageUserInUi(targetUser) {
    if (isAdmin()) {
        return true;
    }
    if (!isGroupAdmin()) {
        return false;
    }
    const targetRole = String(targetUser?.role || "").toLowerCase();
    if (targetRole !== "user") {
        return false;
    }
    const actorGroups = new Set((currentUser?.groups || []).map((item) => String(item || "").toLowerCase()));
    const targetGroups = new Set((targetUser?.groups || []).map((item) => String(item || "").toLowerCase()));
    for (const group of targetGroups) {
        if (actorGroups.has(group)) {
            return true;
        }
    }
    return false;
}

function isCurrentUser(username) {
    return String(username || "").toLowerCase() === String(currentUser?.username || "").toLowerCase();
}

function applyAdminViewScope() {
    const centralSection = document.getElementById("admin-central-section");
    const vmsContainer = document.getElementById("admin-vms")?.closest("section");
    const auditSection = document.getElementById("admin-audit")?.closest("section");
    const statsSection = document.getElementById("admin-stats-section");
    const createGroupButton = document.getElementById("admin-create-group-button");
    const createVMButton = document.getElementById("admin-create-vm-button");

    const fullAdmin = isAdmin();
    const canSeeAdminReadOnly = canAccessAdminView();
    const canSeeStats = canAccessAdminView();
    centralSection?.classList.toggle("hidden", !canSeeAdminReadOnly);
    vmsContainer?.classList.toggle("hidden", !canSeeAdminReadOnly);
    auditSection?.classList.toggle("hidden", !fullAdmin);
    statsSection?.classList.toggle("hidden", !canSeeStats);
    createGroupButton?.classList.toggle("hidden", !fullAdmin);
    createVMButton?.classList.toggle("hidden", !fullAdmin);
}

function setSessionUser(user) {
    const previousUsername = String(currentUser?.username || "").toLowerCase();
    currentUser = user || null;
    const nextUsername = String(currentUser?.username || "").toLowerCase();
    const userChanged = previousUsername !== nextUsername;

    const loginScreen = document.getElementById("login-screen");
    const resetTokenScreen = document.getElementById("reset-token-screen");
    const hasResetToken = new URLSearchParams(window.location.search).has("reset_token");
    const topBanner = document.getElementById("top-banner");
    const dashboardShell = document.getElementById("dashboard-shell");
    const sessionPanel = document.getElementById("session-panel");
    const tabAdmin = document.getElementById("tab-admin");
    const tabDoc = document.getElementById("tab-doc");
    const userName = document.getElementById("session-user-name");
    const userMeta = document.getElementById("session-user-meta");
    applyTheme(_loadThemePreference(currentUser?.username || null), { persist: false });

    if (currentUser) {
        topBanner?.classList.remove("max-w-xl", "mx-auto");
        loginScreen?.classList.add("hidden");
        resetTokenScreen?.classList.add("hidden");
        dashboardShell?.classList.remove("hidden");
        sessionPanel?.classList.remove("hidden");

        if (userChanged) {
            latestStateItems = [];
            resetAdminPanelCache();
            resetLabRequestsCache();
            renderState([]);
            showView("clab");
        }

        if (userName) {
            userName.textContent = currentUser.full_name || currentUser.username;
        }
        if (userMeta) {
            userMeta.textContent = sessionMetaLabel(currentUser);
        }
        if (tabAdmin) {
            tabAdmin.classList.toggle("hidden", !canAccessAdminView());
        }
        if (tabDoc) {
            tabDoc.classList.remove("hidden");
        }
        applyAdminViewScope();
        _loadAllInProgressStates();
        rebuildInFlightActivityFromTrackedJobs();
        _restoreTrackedJobWindows();
        _loadActivityHistoryFromStorage();
        renderActivityRail();
        void refreshTrackedJobStates({ force: true });
        void refreshActivityAuditHistory(true);
        void loadMySshKeys();
        void refreshLabRequests({ silent: true });
        return;
    }

    topBanner?.classList.add("max-w-xl", "mx-auto");
    if (hasResetToken) {
        loginScreen?.classList.add("hidden");
        resetTokenScreen?.classList.remove("hidden");
    } else {
        loginScreen?.classList.remove("hidden");
        resetTokenScreen?.classList.add("hidden");
    }
    dashboardShell?.classList.add("hidden");
    sessionPanel?.classList.add("hidden");
    const _sb = document.getElementById("session-body");
    if (_sb) { _sb.style.maxHeight = "0"; _sb.style.opacity = "0"; }
    document.getElementById("session-toggle-icon")?.classList.remove("rotate-180");
    if (tabAdmin) {
        tabAdmin.classList.add("hidden");
    }
    if (tabDoc) {
        tabDoc.classList.add("hidden");
    }
    const sshInput = document.getElementById("ssh-keys-input");
    if (sshInput instanceof HTMLTextAreaElement) {
        sshInput.value = "";
    }
    const sshFeedback = document.getElementById("ssh-keys-feedback");
    sshFeedback?.classList.add("hidden");
    latestStateItems = [];
    resetAdminPanelCache();
    resetLabRequestsCache();
    labReconfigureState.clear();
    labSetDefaultState.clear();
    labConfigDiffState.clear();
    activityInFlightActions.clear();
    activityLocalHistory = [];
    activityAuditUserHistory = [];
    activityAuditLastFetchAt = 0;
    renderActivityRail();
}

async function loadSession() {
    try {
        const response = await apiFetch("/api/auth/me");
        if (!response.ok) {
            setSessionUser(null);
            return false;
        }
        const payload = await response.json();
        setSessionUser(payload.user || null);
        return true;
    } catch (_) {
        setSessionUser(null);
        return false;
    }
}

function bindAuthControls() {
    _stripCredentialQueryParamsFromUrl();

    const loginForm = document.getElementById("login-form");
    const loginError = document.getElementById("login-error");
    const loginSubmit = document.getElementById("login-submit");
    const logoutButton = document.getElementById("logout-button");
    const sessionToggleButton = document.getElementById("session-toggle-button");
    const sessionBody = document.getElementById("session-body");
    const sessionToggleIcon = document.getElementById("session-toggle-icon");
    const themeToggleButton = document.getElementById("theme-toggle");
    const activityRailToggle = document.getElementById("activity-rail-toggle");

    function collapseSessionBody() {
        if (!sessionBody) return;
        sessionBody.style.maxHeight = sessionBody.scrollHeight + "px";
        requestAnimationFrame(() => {
            sessionBody.style.maxHeight = "0";
            sessionBody.style.opacity = "0";
        });
        sessionToggleIcon?.classList.remove("rotate-180");
    }
    function expandSessionBody() {
        if (!sessionBody) return;
        sessionBody.style.maxHeight = sessionBody.scrollHeight + "px";
        sessionBody.style.opacity = "1";
        sessionToggleIcon?.classList.add("rotate-180");
        sessionBody.addEventListener("transitionend", function onEnd() {
            sessionBody.style.maxHeight = "none";
            sessionBody.removeEventListener("transitionend", onEnd);
        }, { once: true });
    }

    sessionToggleButton?.addEventListener("click", () => {
        const collapsed = sessionBody?.style.maxHeight === "0px" || sessionBody?.style.maxHeight === "";
        if (collapsed) expandSessionBody(); else collapseSessionBody();
    });

    themeToggleButton?.addEventListener("click", () => {
        toggleTheme();
    });

    activityRailToggle?.addEventListener("click", () => {
        const rail = document.getElementById("activity-rail");
        const isOpen = rail?.classList.contains("activity-rail-open");
        setActivityRailOpen(!isOpen);
    });

    const changePasswordForm = document.getElementById("change-password-form");
    const changePasswordFeedback = document.getElementById("change-password-feedback");
    const adminCreateUserButton = document.getElementById("admin-create-user-button");
    const adminCreateGroupButton = document.getElementById("admin-create-group-button");
    const adminCreateVMButton = document.getElementById("admin-create-vm-button");
    const sshKeysSaveButton = document.getElementById("ssh-keys-save-button");

    if (loginForm instanceof HTMLFormElement && loginError instanceof HTMLElement && loginSubmit instanceof HTMLButtonElement) {
        loginForm.addEventListener("submit", async (event) => {
            event.preventDefault();
            loginError.classList.add("hidden");
            loginSubmit.disabled = true;

            const formData = new FormData(loginForm);
            try {
                const response = await apiFetch("/api/auth/login", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        username: String(formData.get("username") || "").trim(),
                        password: String(formData.get("password") || ""),
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
                setSessionUser(payload.user || null);
                _stripCredentialQueryParamsFromUrl();
                loginForm.reset();
                _refreshFailCount = 0;
                _startPolling();
                await refreshState();
            } catch (error) {
                loginError.textContent = error?.message || "Connexion impossible.";
                loginError.classList.remove("hidden");
            } finally {
                loginSubmit.disabled = false;
            }
        });
    }

    logoutButton?.addEventListener("click", async () => {
        _stopPolling();
        _hideConnectionLostBanner();
        _refreshFailCount = 0;
        try {
            await apiFetch("/api/auth/logout", { method: "POST" });
        } catch (_) {
        }
        latestStateItems = [];
        setSessionUser(null);
    });

    if (changePasswordForm instanceof HTMLFormElement && changePasswordFeedback instanceof HTMLElement) {
        changePasswordForm.addEventListener("submit", async (event) => {
            event.preventDefault();
            const currentPassword = document.getElementById("current-password");
            const newPassword = document.getElementById("new-password");
            if (!(currentPassword instanceof HTMLInputElement) || !(newPassword instanceof HTMLInputElement)) {
                return;
            }

            changePasswordFeedback.classList.add("hidden");
            try {
                const response = await apiFetch("/api/auth/change-password", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        current_password: currentPassword.value,
                        new_password: newPassword.value,
                    }),
                });
                if (!response.ok) {
                    const payloadText = await response.text();
                    let detail = payloadText || `HTTP ${response.status}`;
                    try {
                        detail = JSON.parse(payloadText)?.detail || detail;
                    } catch (_) {
                    }
                    throw new Error(detail);
                }

                changePasswordFeedback.textContent = "Mot de passe mis à jour.";
                changePasswordFeedback.className = "text-xs mt-2 text-emerald-300";
                changePasswordFeedback.classList.remove("hidden");
                changePasswordForm.reset();
            } catch (error) {
                changePasswordFeedback.textContent = error?.message || "Mise à jour impossible.";
                changePasswordFeedback.className = "text-xs mt-2 text-rose-300";
                changePasswordFeedback.classList.remove("hidden");
            }
        });
    }

    if (sshKeysSaveButton instanceof HTMLButtonElement) {
        sshKeysSaveButton.addEventListener("click", async () => {
            const input = document.getElementById("ssh-keys-input");
            if (!(input instanceof HTMLTextAreaElement)) {
                return;
            }

            if (!currentUser) {
                showSshKeysFeedback("Session expirée.", "error");
                return;
            }

            sshKeysSaveButton.disabled = true;
            const keys = input.value
                .split("\n")
                .map((line) => line.trim())
                .filter(Boolean);
            showSshKeysFeedback("Enregistrement en cours...", "neutral");

            try {
                const response = await apiFetch("/api/auth/ssh-keys", {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ keys }),
                });
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
                const saved = Array.isArray(payload.keys) ? payload.keys : [];
                input.value = saved.join("\n");
                showSshKeysFeedback(`Clés SSH enregistrées (${saved.length}).`, "success");
            } catch (error) {
                showSshKeysFeedback(error?.message || "Enregistrement des clés SSH impossible.", "error");
            } finally {
                sshKeysSaveButton.disabled = false;
            }
        });
    }

    adminCreateUserButton?.addEventListener("click", async () => {
        await refreshAdminPanel();
        showCreateUserModal();
    });

    adminCreateGroupButton?.addEventListener("click", async () => {
        await refreshAdminPanel();
        showCreateGroupModal();
    });

    adminCreateVMButton?.addEventListener("click", async () => {
        await refreshAdminPanel();
        const launchCreateVm = window.showCreateVMModal;
        if (typeof launchCreateVm !== "function") {
            showToastMessage("Action indisponible", "Le module de formulaire VM n'est pas chargé.", true);
            return;
        }
        launchCreateVm();
    });

    document.getElementById("admin-central-toggle")?.addEventListener("click", () => {
        adminCentralCollapsed = !adminCentralCollapsed;
        updateAdminCentralPanelVisibility();
    });

    document.getElementById("stats-granularity")?.addEventListener("change", () => {
        if (latestAdminStats) renderAdminStats(latestAdminStats);
    });
    document.getElementById("stats-refresh-btn")?.addEventListener("click", () => {
        refreshAdminStats();
    });

    document.getElementById("tab-clab")?.addEventListener("click", () => showView("clab"));
    document.getElementById("tab-doc")?.addEventListener("click", () => showView("doc"));
    document.getElementById("tab-admin")?.addEventListener("click", () => showView("admin"));
}
