// state-render.js
// Extracted from app.js - key helpers + state rendering

function graphSignature(lab) {
    if (!lab || !lab.graph) {
        return "no-graph";
    }
    const nodes = Array.isArray(lab.graph.nodes) ? lab.graph.nodes : [];
    const links = Array.isArray(lab.graph.links) ? lab.graph.links : [];
    const nodeNames = nodes.map((node) => node?.name || "").sort().join("|");
    const linkPairs = links
        .map((link) => `${link?.source || ""}->${link?.target || ""}:${link?.source_if || ""}:${link?.target_if || ""}`)
        .sort()
        .join("|");
    return `${nodes.length}:${links.length}:${nodeNames}:${linkPairs}`;
}

function makeLabKey(vm, lab) {
    const vmKey = vm.id || vm.name || "vm";
    const labKey = lab.name || "lab";
    return `${vmKey}::${labKey}`;
}

function findLabKey(vmId, labName) {
    for (const vm of latestStateItems) {
        if ((vm.id || "") !== vmId) {
            continue;
        }
        for (const lab of vm.labs || []) {
            if ((lab.name || "") === labName) {
                return makeLabKey(vm, lab);
            }
        }
    }
    return `${vmId || "vm"}::${labName || "lab"}`;
}


function captureExpandedLabs() {
    expandedLabs.clear();
    const opened = document.querySelectorAll("details[data-lab-key][open]");
    opened.forEach((item) => {
        const key = item.getAttribute("data-lab-key");
        if (key) {
            expandedLabs.add(key);
        }
    });
}

function hasOpenRouterPanel() {
    return document.querySelector("details[data-lab-key][open]") !== null;
}

function captureRouterPanelScrollState(container) {
    if (!(container instanceof HTMLElement)) {
        return {};
    }
    const state = {};
    for (const el of container.querySelectorAll("[data-router-scroll]")) {
        const key = el.getAttribute("data-router-scroll");
        if (!key) {
            continue;
        }
        state[key] = {
            top: Number(el.scrollTop) || 0,
            left: Number(el.scrollLeft) || 0,
        };
    }
    return state;
}

function restoreRouterPanelScrollState(container, state) {
    if (!(container instanceof HTMLElement)) {
        return;
    }
    for (const el of container.querySelectorAll("[data-router-scroll]")) {
        const key = el.getAttribute("data-router-scroll");
        if (!key) {
            continue;
        }
        if (Object.prototype.hasOwnProperty.call(state, key)) {
            const saved = state[key];
            if (saved && typeof saved === "object") {
                el.scrollTop = Number(saved.top) || 0;
                el.scrollLeft = Number(saved.left) || 0;
            } else {
                // Backward compatibility with legacy numeric value.
                el.scrollTop = Number(saved) || 0;
            }
        }
    }
}

function captureRouterTableCache(container) {
    if (!(container instanceof HTMLElement)) {
        return {};
    }
    const cache = {};
    for (const details of container.querySelectorAll("details[data-lab-key]")) {
        const labKey = details.getAttribute("data-lab-key");
        if (!labKey) {
            continue;
        }
        const wrapper = details.querySelector(".table-wrapper[data-router-signature]");
        if (!(wrapper instanceof HTMLElement)) {
            continue;
        }
        cache[labKey] = {
            signature: String(wrapper.dataset.routerSignature || ""),
            html: wrapper.innerHTML,
        };
    }
    return cache;
}

function _tableConfigSignature() {
    const config = _getTableColumnConfig();
    const order = Array.isArray(config.order) ? config.order.join("|") : "";
    const sortBy = String(config.sortBy || "hostname");
    const sortDir = String(config.sortDir || "asc");
    const widths = config.widths && typeof config.widths === "object" ? config.widths : {};
    const widthsSig = Object.keys(widths)
        .sort()
        .map((key) => `${key}:${Number(widths[key] || 0)}`)
        .join("|");
    return `${order}::${sortBy}::${sortDir}::${widthsSig}`;
}

function _makeRoutersSignature(routers) {
    const rowsSig = (Array.isArray(routers) ? routers : [])
        .map((router) => [
            String(router?.name || ""),
            String(router?.mgmt_ipv4 || ""),
            String(router?.kind || ""),
            String(router?.state || ""),
            String(router?.uptime || ""),
        ].join("~"))
        .join("||");
    return `${_tableConfigSignature()}::${rowsSig}`;
}

async function renderGraphPanel(lab, options = { preserveViewport: false }) {
    const labNameNode = document.getElementById("graph-lab-name");
    const graphPanel = document.getElementById("graph-panel");
    if (!labNameNode || !graphPanel) {
        return;
    }

    labNameNode.textContent = lab?.name || "Aucun LAB sélectionné";

    if (!lab || !lab.graph) {
        graphPanel.innerHTML = "Aucun diagramme disponible pour ce LAB.";
        return;
    }

    const graphNodes = Array.isArray(lab.graph.nodes) ? lab.graph.nodes : [];
    const graphLinks = Array.isArray(lab.graph.links) ? lab.graph.links : [];

    if (!graphNodes.length && !graphLinks.length) {
        graphPanel.innerHTML = "Diagramme indisponible (topologie non trouvée ou non parseable).";
        return;
    }

    const elements = [];

    graphNodes.forEach((node) => {
        if (!node?.name) {
            return;
        }
        elements.push({
            data: {
                id: node.name,
                label: node.name,
                kind: node.kind || "node",
            },
            position: {
                x: Number.isFinite(Number(node.x)) ? Number(node.x) : undefined,
                y: Number.isFinite(Number(node.y)) ? Number(node.y) : undefined,
            },
        });
    });

    graphLinks.forEach((link, index) => {
        if (!link?.source || !link?.target) {
            return;
        }
        elements.push({
            data: {
                id: `e_${link.source}_${link.target}_${index}`,
                source: link.source,
                target: link.target,
                source_if: link.source_if || "",
                target_if: link.target_if || "",
            },
        });
    });

    graphPanel.innerHTML = '<div id="cy-graph" class="w-full h-full min-h-[420px]"></div>';

    let previousViewport = null;
    if (options.preserveViewport && graphInstance) {
        previousViewport = {
            zoom: graphInstance.zoom(),
            pan: graphInstance.pan(),
        };
    }

    if (graphInstance) {
        graphInstance.destroy();
        graphInstance = null;
    }

    if (window.cytoscape) {
        graphInstance = window.cytoscape({
            container: document.getElementById("cy-graph"),
            elements,
            layout: {
                name: "preset",
                fit: true,
                padding: 20,
            },
            style: [
                {
                    selector: "node",
                    style: {
                        "background-color": "#0ea5e9",
                        label: "data(label)",
                        color: "#e2e8f0",
                        "font-size": "8px",
                        "text-wrap": "wrap",
                        "text-max-width": "95px",
                        "text-valign": "center",
                        "text-halign": "center",
                        width: 34,
                        height: 34,
                        "border-width": 1,
                        "border-color": "#0f172a",
                    },
                },
                {
                    selector: "node[kind *= 'juniper']",
                    style: { "background-color": "#22c55e" },
                },
                {
                    selector: "node[kind *= 'cisco']",
                    style: { "background-color": "#3b82f6" },
                },
                {
                    selector: "edge",
                    style: {
                        width: 1.2,
                        "line-color": "#64748b",
                        "target-arrow-shape": "none",
                        "curve-style": "bezier",
                        "source-label": "data(source_if)",
                        "target-label": "data(target_if)",
                        "source-text-offset": 12,
                        "target-text-offset": 12,
                        "font-size": "7px",
                        color: "#cbd5e1",
                        "text-background-color": "#0f172a",
                        "text-background-opacity": 0.85,
                        "text-background-padding": "1px",
                        "text-rotation": "autorotate",
                    },
                },
            ],
            wheelSensitivity: 0.18,
        });

        const allNodesHavePosition = graphNodes.every(
            (node) => Number.isFinite(Number(node.x)) && Number.isFinite(Number(node.y)),
        );

        if (!allNodesHavePosition) {
            graphInstance.layout({
                name: "cose",
                fit: true,
                padding: 24,
                animate: false,
            }).run();
        } else if (previousViewport) {
            graphInstance.zoom(previousViewport.zoom);
            graphInstance.pan(previousViewport.pan);
        } else {
            graphInstance.fit(undefined, 24);
        }
    }
}


function renderState(items, options = {}) {
    const { preserveViewport = false } = options;
    captureExpandedLabs();

    const cards = document.getElementById("cards");
    const detailsState = captureDetailsState(cards);
    const routerPanelScrollState = captureRouterPanelScrollState(cards);
    const routerTableCache = captureRouterTableCache(cards);
    const viewportState = preserveViewport
        ? {
            x: window.scrollX,
            y: window.scrollY,
        }
        : null;
    cards.innerHTML = "";

    if (!items.length) {
        cards.innerHTML = `
            <div class="rounded-xl border border-slate-800 bg-slate-900 p-6 text-slate-400">
                Aucune VM configurée ou accessible pour le moment.
            </div>
        `;
        return;
    }

    items = [...items].sort((a, b) => String(a.name || a.id || "").localeCompare(String(b.name || b.id || ""), undefined, { numeric: true }));
    items.forEach((vm) => {
        const cpu = vm.resources?.cpu_percent;
        const memory = vm.resources?.memory?.percent;
        const memoryUsed = vm.resources?.memory?.used;
        const memoryTotal = vm.resources?.memory?.total;
        const disk = vm.resources?.disk?.percent;
        const diskUsed = vm.resources?.disk?.used;
        const diskTotal = vm.resources?.disk?.total;
        const labs = vm.labs || [];

        const statusClass = vm.online ? "text-emerald-400" : "text-rose-400";
        const statusLabel = vm.online ? "ONLINE" : "OFFLINE";

        const labsHtml = labs.length
            ? labs.map((lab) => {
                const routers = Array.isArray(lab.routers) ? lab.routers : [];
                const sortedRouters = [...routers].sort((first, second) => {
                    const firstName = (first.name || "").toLowerCase();
                    const secondName = (second.name || "").toLowerCase();
                    return firstName.localeCompare(secondName);
                });

                const labKey = makeLabKey(vm, lab);
                const isOpen = expandedLabs.has(labKey);
                const reconfigState = labReconfigureState.get(labKey) || {};
                const isReconfiguring = Boolean(reconfigState.inProgress);
                const hasReconfigureJobId = Boolean(reconfigState.jobId);
                const completedAt = reconfigState.completedAt || _loadCompletedAt('reconfig', labKey);
                const completionMessage = completedAt
                    ? `Reconfiguration terminée le ${formatCompletionDate(completedAt)}`
                    : "";
                const setDefaultState = labSetDefaultState.get(labKey) || {};
                const isSavingDefault = Boolean(setDefaultState.inProgress);
                const configDiffState = labConfigDiffState.get(labKey) || {};
                const isConfigDiffing = Boolean(configDiffState.inProgress);
                const hasConfigDiffJobId = Boolean(configDiffState.jobId);
                const defaultCompletedAt = setDefaultState.completedAt || _loadCompletedAt('setdefault', labKey);
                const defaultCompletionMessage = defaultCompletedAt
                    ? `Base par défaut mise à jour le ${formatCompletionDate(defaultCompletedAt)}`
                    : "";

                const disableClass = isReconfiguring ? "opacity-50 cursor-not-allowed" : "";
                const disabledAttr = isReconfiguring ? "disabled" : "";
                const reservation = lab.reservation;
                const scheduledReservations = Array.isArray(lab.scheduled_reservations) ? lab.scheduled_reservations : [];
                const nextScheduledReservation = scheduledReservations.length ? scheduledReservations[0] : null;
                const labOperable = canOperateLab(lab);
                const sandboxEnabled = Boolean(lab.sandbox_enabled);
                // La réservation appartient à l'utilisateur courant (comparaison stricte sur le username)
                const isMyReservation = Boolean(reservation) &&
                    String(reservation.owner_username || "").toLowerCase() === String(currentUser?.username || "").toLowerCase();
                // L'admin peut libérer n'importe quelle réservation, mais ce n'est pas forcément "sa" session
                const reservationOwnedByCurrentUser = isMyReservation;
                const reconfigureButtonDisabled = !labOperable || (isReconfiguring && !hasReconfigureJobId);
                const actionDisabledAttr = !labOperable ? "disabled" : "";
                const actionDisableClass = !labOperable ? "opacity-50 cursor-not-allowed" : "";
                const reconfigureButtonDisabledAttr = reconfigureButtonDisabled ? "disabled" : "";
                const reconfigureButtonDisableClass = reconfigureButtonDisabled ? "opacity-50 cursor-not-allowed" : "";
                const reconfigureButtonLabel = isReconfiguring
                    ? (hasReconfigureJobId ? "Suivi reconfig" : "Initialisation...")
                    : "Reconfigurer";
                const labLocked = Boolean(lab.lab_locked);
                const lockBlocksDangerOps = labLocked && !isAdmin();
                const redeployDisabledAttr = !canRedeployLab() || isReconfiguring || lockBlocksDangerOps ? "disabled" : "";
                const redeployDisableClass = !canRedeployLab() || isReconfiguring || lockBlocksDangerOps ? "opacity-50 cursor-not-allowed" : "";
                const reservationChip = reservation
                    ? isMyReservation
                        ? `<span class="inline-flex items-center gap-1.5 rounded-full bg-emerald-900/40 border border-emerald-700/50 px-2 py-0.5 text-[11px] text-emerald-300">● Votre session · <span data-reservation-expiry="${escapeHtml(reservation.expires_at || "")}">${escapeHtml(formatRemainingReservation(reservation.expires_at))}</span></span>`
                        : `<span class="inline-flex items-center gap-1.5 rounded-full bg-amber-900/40 border border-amber-700/50 px-2 py-0.5 text-[11px] text-amber-300">● ${escapeHtml(reservation.reserved_by || "Inconnu")} · <span data-reservation-expiry="${escapeHtml(reservation.expires_at || "")}">${escapeHtml(formatRemainingReservation(reservation.expires_at))}</span></span>`
                    : isAdmin()
                        ? `<span class="inline-flex items-center rounded-full bg-cyan-900/20 border border-cyan-800/40 px-2 py-0.5 text-[11px] text-cyan-400/70">Admin</span>`
                        : `<span class="inline-flex items-center rounded-full bg-slate-800/60 border border-slate-700 px-2 py-0.5 text-[11px] text-slate-500">Libre</span>`;
                const scheduledChip = nextScheduledReservation
                    ? (() => {
                        const startsAtValue = String(nextScheduledReservation.starts_at || "");
                        const startsAtDate = new Date(startsAtValue);
                        const startsAtLabel = Number.isFinite(startsAtDate.getTime())
                            ? startsAtDate.toLocaleString("fr-FR")
                            : startsAtValue;
                        return `<span class="inline-flex items-center rounded-full bg-sky-900/30 border border-sky-700/50 px-2 py-0.5 text-[11px] text-sky-300">Planifiée: ${escapeHtml(startsAtLabel)}</span>`;
                    })()
                    : "";
                const extensionAvailableHours = reservation
                    ? Math.max(0, MAX_RESERVATION_TOTAL_HOURS - Number(reservation.duration_hours || 0))
                    : 0;
                const reservationRemainingMs = reservation ? (new Date(String(reservation.expires_at || "")).getTime() - Date.now()) : NaN;
                const canTryExtendReservation = Boolean(reservation)
                    && (isMyReservation || isAdmin())
                    && extensionAvailableHours > 0
                    && Number.isFinite(reservationRemainingMs)
                    && reservationRemainingMs > 0
                    && reservationRemainingMs <= (RESERVATION_EXTENSION_WINDOW_HOURS * 3600 * 1000);

                const extendButtonHtml = canTryExtendReservation
                    ? `<button type="button" data-action="extend-reservation" data-vm-id="${vm.id || ""}" data-lab-name="${lab.name || ""}" class="text-xs px-2 py-1 rounded border border-amber-700 text-amber-300 hover:bg-amber-900/30">Étendre</button>`
                    : "";

                const reserveButtonHtml = reservation
                    ? (isMyReservation || isAdmin() || isGroupAdmin())
                        ? `<button type="button" data-action="release-reservation" data-vm-id="${vm.id || ""}" data-lab-name="${lab.name || ""}" class="text-xs px-2 py-1 rounded border border-rose-700 text-rose-300 hover:bg-rose-900/30">Libérer</button>`
                        : `<span class="text-[11px] px-2 py-1 rounded border border-slate-700 text-slate-400">Réservé</span>`
                    : `<button type="button" ${disabledAttr} data-action="reserve-lab" data-vm-id="${vm.id || ""}" data-lab-name="${lab.name || ""}" class="text-xs px-2 py-1 rounded border border-amber-700 text-amber-300 hover:bg-amber-900/30 ${disableClass}">Réserver</button>`;
                const showReservationActions = labOperable || isAdmin();
                const reconfigureButtonHtml = showReservationActions
                    ? `<button type="button" ${reconfigureButtonDisabledAttr} data-action="reconfigure-lab" data-vm-id="${vm.id || ""}" data-lab-name="${lab.name || ""}" class="text-xs px-2 py-1 rounded border border-fuchsia-700 text-fuchsia-300 hover:bg-fuchsia-900/30 ${reconfigureButtonDisableClass}">${reconfigureButtonLabel}</button>`
                    : "";
                const configDiffButtonDisabled = !labOperable || (isConfigDiffing && !hasConfigDiffJobId);
                const configDiffButtonDisabledAttr = configDiffButtonDisabled ? "disabled" : "";
                const configDiffButtonDisableClass = configDiffButtonDisabled ? "opacity-50 cursor-not-allowed" : "";
                const configDiffButtonLabel = isConfigDiffing
                    ? (hasConfigDiffJobId ? "Suivi diff" : "Initialisation...")
                    : "Diff";
                const configDiffButtonHtml = showReservationActions
                    ? `<button type="button" ${configDiffButtonDisabledAttr} data-action="config-diff" data-vm-id="${vm.id || ""}" data-lab-name="${lab.name || ""}" data-routers="${escapeHtml(JSON.stringify(sortedRouters.map((router) => router?.name || "").filter(Boolean)))}" class="text-xs px-2 py-1 rounded border border-sky-700 text-sky-300 hover:bg-sky-900/30 ${configDiffButtonDisableClass}">${configDiffButtonLabel}</button>`
                    : "";
                const exportYamlButtonHtml = showReservationActions
                    ? `<button type="button" ${actionDisabledAttr} data-action="export-yaml" data-vm-id="${vm.id || ""}" data-lab-name="${lab.name || ""}" class="text-xs px-2 py-1 rounded border border-slate-700 text-slate-400 hover:bg-slate-800 ${actionDisableClass}">YAML</button>`
                    : "";
                const exportConfigButtonHtml = showReservationActions
                    ? `<button type="button" ${actionDisabledAttr} data-action="export-config" data-vm-id="${vm.id || ""}" data-lab-name="${lab.name || ""}" class="text-xs px-2 py-1 rounded border border-slate-700 text-slate-400 hover:bg-slate-800 ${actionDisableClass}">Export conf</button>`
                    : "";
                const startButtonHtml = canRedeployLab()
                    ? `<button type="button" ${redeployDisabledAttr} data-action="start-lab" data-vm-id="${vm.id || ""}" data-lab-name="${lab.name || ""}" data-topology-file="${lab.topology_file || ""}" class="text-xs px-2 py-1 rounded border border-emerald-700 text-emerald-300 hover:bg-emerald-900/30 ${redeployDisableClass}">Start</button>`
                    : "";
                const stopButtonHtml = canRedeployLab()
                    ? `<button type="button" ${redeployDisabledAttr} data-action="stop-lab" data-vm-id="${vm.id || ""}" data-lab-name="${lab.name || ""}" class="text-xs px-2 py-1 rounded border border-rose-700 text-rose-300 hover:bg-rose-900/30 ${redeployDisableClass}">Stop</button>`
                    : "";
                const redeployButtonHtml = canRedeployLab()
                    ? `<button type="button" ${redeployDisabledAttr} data-action="redeploy-lab" data-vm-id="${vm.id || ""}" data-lab-name="${lab.name || ""}" data-topology-file="${lab.topology_file || ""}" class="text-xs px-2 py-1 rounded border border-amber-700 text-amber-300 hover:bg-amber-900/30 ${redeployDisableClass}">Redeploy</button>`
                    : "";
                const setDefaultButtonDisabledAttr = !canSetDefaultConfig() || isReconfiguring ? "disabled" : "";
                const setDefaultButtonDisableClass = !canSetDefaultConfig() || isReconfiguring ? "opacity-50 cursor-not-allowed" : "";
                const setDefaultButtonLabel = isSavingDefault ? "Suivi save default" : "Set current as default";
                const setDefaultButtonHtml = canSetDefaultConfig()
                    ? `<button type="button" ${setDefaultButtonDisabledAttr} data-action="set-default-config" data-vm-id="${vm.id || ""}" data-lab-name="${lab.name || ""}" class="text-xs px-2 py-1 rounded border border-emerald-700 text-emerald-300 hover:bg-emerald-900/30 ${setDefaultButtonDisableClass}">${setDefaultButtonLabel}</button>`
                    : "";
                const sandboxBadge = sandboxEnabled
                    ? '<span class="inline-flex items-center rounded-full bg-violet-900/30 border border-violet-700/50 px-2 py-0.5 text-[11px] text-violet-300">Sandbox</span>'
                    : "";
                const lockBadge = labLocked
                    ? '<span class="inline-flex items-center rounded-full bg-rose-900/30 border border-rose-700/50 px-2 py-0.5 text-[11px] text-rose-300">Lock</span>'
                    : "";
                const canEditSandboxYaml = sandboxEnabled && (isMyReservation || isAdmin());
                const sandboxEditButtonHtml = canEditSandboxYaml
                    ? `<button type="button" data-action="edit-sandbox-yaml" data-vm-id="${vm.id || ""}" data-lab-name="${lab.name || ""}" class="text-xs px-2 py-1 rounded border border-violet-700 text-violet-300 hover:bg-violet-900/30">Éditer YAML</button>`
                    : "";
                const sandboxToggleButtonHtml = isAdmin()
                    ? `<button type="button" data-action="toggle-sandbox-lab" data-vm-id="${vm.id || ""}" data-lab-name="${lab.name || ""}" data-enabled="${sandboxEnabled ? "true" : "false"}" class="text-xs px-2 py-1 rounded border ${sandboxEnabled ? "border-violet-600 text-violet-200" : "border-slate-700 text-slate-400"} hover:bg-slate-800">${sandboxEnabled ? "Sandbox ON" : "Sandbox OFF"}</button>`
                    : "";
                const lockToggleButtonHtml = isAdmin()
                    ? `<button type="button" data-action="toggle-lab-lock" data-vm-id="${vm.id || ""}" data-lab-name="${lab.name || ""}" data-locked="${labLocked ? "true" : "false"}" class="text-xs px-2 py-1 rounded border ${labLocked ? "border-rose-600 text-rose-200" : "border-slate-700 text-slate-300"} hover:bg-slate-800">${labLocked ? "Unlock" : "Lock"}</button>`
                    : "";

                const tableId = `routers-table-${vm.id || "vm"}-${lab.name || ""}`.replace(/[^a-zA-Z0-9_-]/g, "_");
                const routerSignature = _makeRoutersSignature(sortedRouters);
                // Stocker les routeurs dans la map globale pour le tri et le redimensionnement
                routerTableDataMap.set(tableId, sortedRouters);
                const generatedRouterRows = `
                    ${_generateTableHeaderHtml(tableId, DEFAULT_TABLE_COLUMNS.order)}
                    ${_generateRouterTableHtml(tableId, sortedRouters)}
                `;
                const cachedRouter = routerTableCache[labKey];
                const routerRows = cachedRouter && cachedRouter.signature === routerSignature
                    ? cachedRouter.html
                    : generatedRouterRows;

                return `
                    <li class="rounded-lg border border-slate-800 bg-slate-900/60 p-3">
                        <div class="flex items-start justify-between gap-2 flex-wrap">
                            <div class="flex items-center gap-2 flex-wrap min-w-0">
                                <span class="text-sm font-medium text-slate-100">${lab.name || "lab"}</span>
                                <span class="text-[11px] text-slate-500">${lab.node_count || 0} nœuds</span>
                                ${sandboxBadge}
                                ${lockBadge}
                                ${reservationChip}
                                ${scheduledChip}
                            </div>
                            <div class="flex items-center gap-1.5 flex-wrap justify-end">
                                ${reserveButtonHtml}
                                ${extendButtonHtml}
                                ${reconfigureButtonHtml}
                                ${configDiffButtonHtml}
                                <button type="button" data-action="show-graph" data-lab-key="${labKey}" class="text-xs px-2 py-1 rounded border border-cyan-800 text-cyan-400 hover:bg-cyan-900/20">Graph</button>
                                <button type="button" data-action="open-lab-doc" data-vm-id="${vm.id || ""}" data-lab-name="${lab.name || ""}" class="text-xs px-2 py-1 rounded border border-cyan-700 text-cyan-300 hover:bg-cyan-900/30">Documentation</button>
                                ${sandboxEditButtonHtml}
                                ${exportYamlButtonHtml}
                                ${exportConfigButtonHtml}
                                ${sandboxToggleButtonHtml}
                                ${lockToggleButtonHtml}
                                ${setDefaultButtonHtml}
                                ${startButtonHtml.replace('data-action="start-lab"', `data-action="start-lab" data-lab-locked="${labLocked ? "true" : "false"}"`)}
                                ${stopButtonHtml.replace('data-action="stop-lab"', `data-action="stop-lab" data-lab-locked="${labLocked ? "true" : "false"}"`)}
                                ${redeployButtonHtml.replace('data-action="redeploy-lab"', `data-action="redeploy-lab" data-lab-locked="${labLocked ? "true" : "false"}"`)}
                            </div>
                        </div>
                        ${isReconfiguring ? '<p class="text-[11px] text-amber-300 mt-1.5">Reconfiguration en cours...</p>' : ''}
                        ${isConfigDiffing ? '<p class="text-[11px] text-sky-300 mt-1.5">Diff configuration en cours...</p>' : ''}
                        ${isSavingDefault ? '<p class="text-[11px] text-emerald-300 mt-1.5">Mise à jour de la base par défaut en cours...</p>' : ''}
                        ${completionMessage ? `<p class="text-[11px] text-emerald-300 mt-1.5">${completionMessage}</p>` : ''}
                        ${defaultCompletionMessage ? `<p class="text-[11px] text-emerald-300 mt-1.5">${defaultCompletionMessage}</p>` : ''}
                        <details data-lab-key="${labKey}" ${isOpen ? "open" : ""} class="group mt-2.5 rounded-md border border-slate-700 bg-slate-900/80">
                            <summary class="cursor-pointer list-none px-3 py-2 text-xs text-slate-300 flex items-center justify-between">
                                <span>Routeurs (${lab.node_count || 0})</span>
                                <span class="text-slate-500 group-open:rotate-180 transition-transform">▾</span>
                            </summary>
                            <div class="border-t border-slate-700">
                                <div class="overflow-x-auto">
                                    <div class="table-wrapper" data-table-id="${tableId}" data-router-signature="${escapeHtml(routerSignature)}">
                                        ${routerRows}
                                    </div>
                                </div>
                            </div>
                        </details>
                    </li>
                `;
            }).join("")
            : '<li class="text-sm text-slate-500">Aucun lab détecté</li>';

        const card = document.createElement("div");
        card.className = `rounded-xl border border-l-4 bg-slate-950/70 p-5 ${vm.online ? "border-slate-800 border-l-emerald-600/60" : "border-slate-800 border-l-rose-700/60"}`;
        card.innerHTML = `
            <div class="flex items-center justify-between mb-1">
                <div class="flex items-center gap-2.5 min-w-0">
                    <h2 class="font-semibold truncate">${vm.name || vm.id || "vm"}</h2>
                    <span class="text-[11px] text-slate-500 shrink-0">${vmEndpointLabel(vm)}</span>
                </div>
                <span class="text-[11px] font-medium px-2 py-0.5 rounded-full border shrink-0 ml-2 ${vm.online ? "border-emerald-700/60 bg-emerald-900/30 text-emerald-300" : "border-rose-700/60 bg-rose-900/30 text-rose-300"}">${statusLabel}</span>
            </div>

            <div class="grid grid-cols-1 md:grid-cols-3 gap-2 text-sm mb-4">
                ${resourceBlock("CPU", cpu, null, null)}
                ${resourceBlock("RAM", memory, memoryUsed, memoryTotal)}
                ${resourceBlock("Disque", disk, diskUsed, diskTotal)}
            </div>

            <div>
                <p class="text-xs text-slate-500 uppercase tracking-wide mb-2">Labs · ${labs.length} actif${labs.length > 1 ? "s" : ""}</p>
                <ul class="space-y-2">${labsHtml}</ul>
            </div>

            ${vm.error ? `<p class="text-xs text-rose-400 mt-3">${vm.error}</p>` : ""}
        `;

        cards.appendChild(card);
    });

    restoreDetailsState(cards, detailsState);
    restoreRouterPanelScrollState(cards, routerPanelScrollState);

    // Initialiser les interactions des tables de routeurs
    document.querySelectorAll(`.table-wrapper[data-table-id]`).forEach(wrapper => {
        const tableId = wrapper.dataset.tableId;
        _setupTableColumnInteractions(tableId);
    });

    if (viewportState) {
        window.requestAnimationFrame(() => {
            window.scrollTo(viewportState.x, viewportState.y);
        });
    }

    updateReservationCountdowns();
}

