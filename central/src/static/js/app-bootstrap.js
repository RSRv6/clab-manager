function _setTabActive(tab, active) {
    if (!tab) {
        return;
    }
    if (active) {
        tab.classList.add("bg-slate-700", "text-slate-100");
        tab.classList.remove("text-slate-400");
    } else {
        tab.classList.remove("bg-slate-700", "text-slate-100");
        tab.classList.add("text-slate-400");
    }
}

function showView(view) {
    currentView = view;
    document.getElementById("view-clab")?.classList.toggle("hidden", view !== "clab");
    document.getElementById("view-doc")?.classList.toggle("hidden", view !== "doc");
    document.getElementById("view-admin")?.classList.toggle("hidden", view !== "admin");
    _setTabActive(document.getElementById("tab-clab"), view === "clab");
    _setTabActive(document.getElementById("tab-doc"), view === "doc");
    _setTabActive(document.getElementById("tab-admin"), view === "admin");
    if (view === "admin") {
        // force:false → respecte le throttle 30 s si les données sont fraîches
        refreshAdminPanel({ force: false });
    }
    if (view === "doc") {
        refreshDocumentationPanel();
    }
    if (view === "clab") {
        refreshLabRequests({ silent: true });
    }
}

function findLabByKey(labKey) {
    for (const vm of latestStateItems) {
        const labs = vm.labs || [];
        for (const lab of labs) {
            if (makeLabKey(vm, lab) === labKey) {
                return { vm, lab };
            }
        }
    }
    return null;
}

function refreshSelectedGraph() {
    if (!selectedGraph?.labKey) {
        return;
    }
    const found = findLabByKey(selectedGraph.labKey);
    if (found) {
        const nextSignature = graphSignature(found.lab);
        if (!graphInstance) {
            selectedGraph = { labKey: selectedGraph.labKey, labName: found.lab.name, signature: nextSignature };
            renderGraphPanel(found.lab);
            return;
        }

        if (selectedGraph.signature !== nextSignature) {
            selectedGraph = { labKey: selectedGraph.labKey, labName: found.lab.name, signature: nextSignature };
            renderGraphPanel(found.lab, { preserveViewport: true });
            return;
        }

        selectedGraph = { labKey: selectedGraph.labKey, labName: found.lab.name, signature: nextSignature };
    }
}

function bindGraphControls() {
    if (graphControlsBound) {
        return;
    }

    const zoomIn = document.getElementById("graph-zoom-in");
    const zoomOut = document.getElementById("graph-zoom-out");
    const zoomFit = document.getElementById("graph-zoom-fit");
    const fullscreen = document.getElementById("graph-fullscreen");
    const graphPanel = document.getElementById("graph-panel");

    if (zoomIn) {
        zoomIn.addEventListener("click", () => {
            if (!graphInstance) {
                return;
            }
            graphInstance.zoom({
                level: graphInstance.zoom() * 1.2,
                renderedPosition: {
                    x: graphInstance.width() / 2,
                    y: graphInstance.height() / 2,
                },
            });
        });
    }

    if (zoomOut) {
        zoomOut.addEventListener("click", () => {
            if (!graphInstance) {
                return;
            }
            graphInstance.zoom({
                level: graphInstance.zoom() / 1.2,
                renderedPosition: {
                    x: graphInstance.width() / 2,
                    y: graphInstance.height() / 2,
                },
            });
        });
    }

    if (zoomFit) {
        zoomFit.addEventListener("click", () => {
            if (!graphInstance) {
                return;
            }
            graphInstance.fit(undefined, 24);
        });
    }

    if (fullscreen && graphPanel) {
        fullscreen.addEventListener("click", async () => {
            if (document.fullscreenElement) {
                await document.exitFullscreen();
                return;
            }
            await graphPanel.requestFullscreen();
            if (graphInstance) {
                graphInstance.resize();
                graphInstance.fit(undefined, 24);
            }
        });
    }

    document.addEventListener("fullscreenchange", () => {
        // Wait for the browser to complete the fullscreen transition before resizing
        setTimeout(() => {
            if (graphInstance) {
                graphInstance.resize();
                graphInstance.fit(undefined, 24);
            }
        }, 50);
    });

    window.addEventListener("resize", () => {
        if (!graphInstance) {
            return;
        }
        graphInstance.resize();
    });

    graphControlsBound = true;
}

async function refreshState() {
    if (!currentUser) {
        return;
    }

    try {
        const response = await apiFetch("/api/state");
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }
        const payload = await response.json();
        latestStateItems = payload.items || [];
        _refreshFailCount = 0;
        _hideConnectionLostBanner();
        refreshSelectedGraph();
        renderState(latestStateItems, { preserveViewport: hasOpenRouterPanel() });
        refreshLabRequests({ force: false, silent: true });

        if (isAdmin() && currentView === "admin") {
            refreshAdminPanel({ force: false, silent: true });
        }
    } catch (error) {
        _refreshFailCount++;
        if (_refreshFailCount >= 3) _showConnectionLostBanner();
        console.error("Failed to refresh state", error);
    }
}

async function bootstrapApp() {
    bindGraphControls();
    bindAuthControls();
    applyTheme(_loadThemePreference(null), { persist: false });
    setActivityRailOpen(false);
    renderActivityRail();

    // Listen for topology builder commit notifications
    window.addEventListener('message', (event) => {
        const msg = event.data;
        if (!msg || !msg.type || !currentUser) {
            return;
        }

        const vmId = msg.vmId;
        const labName = msg.labName;
        if (!vmId || !labName) {
            return;
        }

        const labKey = findLabKey(vmId, labName);
        if (!labKey) {
            return;
        }

        if (msg.type === 'topology-commit-start') {
            beginUserActivity({
                id: `yaml-topo-commit:${labKey}`,
                title: "Deployment Topology Builder",
                target: `${labName} @ ${vmId}`,
                details: "Commit en cours",
            });
        } else if (msg.type === 'topology-commit-success') {
            endUserActivity(`yaml-topo-commit:${labKey}`, "ok", "Topologie committée via Topology Builder");
            renderActivityRail();
        } else if (msg.type === 'topology-commit-error') {
            endUserActivity(`yaml-topo-commit:${labKey}`, "error", msg.error || "Erreur de commit");
            renderActivityRail();
        }
    });

    // ── Handle ?reset_token=XXX in URL ──────────────────────────────────────
    const urlParams = new URLSearchParams(window.location.search);
    const resetToken = urlParams.get("reset_token");
    if (resetToken) {
        // Show reset screen, hide login
        document.getElementById("login-screen")?.classList.add("hidden");
        const resetScreen = document.getElementById("reset-token-screen");
        resetScreen?.classList.remove("hidden");

        const form = document.getElementById("reset-token-form");
        const errorNode = document.getElementById("reset-token-error");
        const successNode = document.getElementById("reset-token-success");
        const submitBtn = document.getElementById("reset-token-submit");

        if (form instanceof HTMLFormElement) {
            form.addEventListener("submit", async (event) => {
                event.preventDefault();
                if (!(errorNode instanceof HTMLElement) || !(submitBtn instanceof HTMLButtonElement)) return;
                errorNode.classList.add("hidden");
                submitBtn.disabled = true;

                const pwd = document.getElementById("reset-token-password")?.value || "";
                const confirm = document.getElementById("reset-token-password-confirm")?.value || "";
                if (pwd !== confirm) {
                    errorNode.textContent = "Les mots de passe ne correspondent pas.";
                    errorNode.classList.remove("hidden");
                    submitBtn.disabled = false;
                    return;
                }
                try {
                    const response = await apiFetch("/api/auth/reset-with-token", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ token: resetToken, new_password: pwd }),
                    });
                    if (!response.ok) {
                        const txt = await response.text();
                        let detail = txt || `HTTP ${response.status}`;
                        try { detail = JSON.parse(txt)?.detail || detail; } catch (_) {}
                        throw new Error(detail);
                    }
                    form.classList.add("hidden");
                    successNode?.classList.remove("hidden");
                    // Clean the token from the URL without reloading
                    window.history.replaceState({}, "", "/");
                } catch (err) {
                    errorNode.textContent = err?.message || "Erreur lors de la réinitialisation.";
                    errorNode.classList.remove("hidden");
                    submitBtn.disabled = false;
                }
            });
        }
        return; // Don't bootstrap the full app — user is on the reset page
    }

    const sessionReady = await loadSession();
    if (sessionReady) {
        await refreshState();
    }

    // Fetch server-side config to keep reservation constants in sync with backend
    try {
        const cfgResp = await apiFetch("/api/config/client");
        if (cfgResp.ok) {
            const cfg = await cfgResp.json();
            MAX_RESERVATION_HOURS = cfg.MAX_RESERVATION_HOURS ?? MAX_RESERVATION_HOURS;
            MAX_RESERVATION_TOTAL_HOURS = cfg.MAX_RESERVATION_TOTAL_HOURS ?? MAX_RESERVATION_TOTAL_HOURS;
            RESERVATION_EXTENSION_WINDOW_HOURS = cfg.RESERVATION_EXTENSION_WINDOW_HOURS ?? RESERVATION_EXTENSION_WINDOW_HOURS;
        }
    } catch (_) {}

    _startPolling();
}

function _startPolling() {
    const refreshMillis = (window.APP_REFRESH_SECONDS || 3) * 1000;
    if (refreshIntervalId === null) {
        refreshIntervalId = window.setInterval(refreshState, refreshMillis);
    }
    if (countdownIntervalId === null) {
        countdownIntervalId = window.setInterval(updateReservationCountdowns, 1000);
    }
}

function _stopPolling() {
    if (refreshIntervalId !== null) { clearInterval(refreshIntervalId); refreshIntervalId = null; }
    if (countdownIntervalId !== null) { clearInterval(countdownIntervalId); countdownIntervalId = null; }
}

function _showConnectionLostBanner() {
    if (document.getElementById("vlm-connection-lost-banner")) return;
    const banner = document.createElement("div");
    banner.id = "vlm-connection-lost-banner";
    banner.className = "fixed top-4 left-1/2 -translate-x-1/2 z-50 bg-rose-900 border border-rose-600 text-rose-100 text-sm font-medium px-4 py-2 rounded-lg shadow-lg pointer-events-none";
    banner.textContent = "⚠ Connexion au serveur perdue — nouvelle tentative en cours…";
    document.body.appendChild(banner);
}

function _hideConnectionLostBanner() {
    document.getElementById("vlm-connection-lost-banner")?.remove();
}

bootstrapApp();
