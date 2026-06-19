function resetAdminPanelCache() {
    latestAdminUsers = [];
    latestAdminGroups = [];
    latestAdminVMs = [];
    latestAdminAuditItems = [];
    latestAdminStats = null;
    latestAdminCentralResources = null;
    latestAdminVmResources = null;
    latestAdminVmResourcesById = {};
    latestAdminVmResourcesMetaById = {};
    latestAdminTopologyInventory = [];
    adminVmCardFetchSeq = 0;
    adminVmListSignature = "";
    adminVmCardsRefreshInFlight = false;
    adminVmCardsRefreshQueued = false;
    adminVmGraphHours = 24;
    adminVmCardsLastRefreshAt = 0;
    adminLastDataSignature = "";
    adminLastStatsSignature = "";
    adminLastRefreshAt = 0;

    const statsBody = document.getElementById("admin-stats-body");
    if (statsBody) {
        statsBody.innerHTML = '<p class="text-xs text-slate-500">Chargement des statistiques...</p>';
    }
}

function buildGroupLabPermissionsFromForm(form) {
    const selectedLabInputs = form.querySelectorAll("input[data-lab-vm-id][data-lab-name]:checked");
    const permissionsMap = new Map();
    for (const input of selectedLabInputs) {
        if (!(input instanceof HTMLInputElement)) {
            continue;
        }
        const vmId = String(input.dataset.labVmId || "").trim();
        const labName = String(input.dataset.labName || "").trim();
        if (!vmId || !labName) {
            continue;
        }
        if (!permissionsMap.has(vmId)) {
            permissionsMap.set(vmId, new Set());
        }
        permissionsMap.get(vmId).add(labName);
    }

    return Array.from(permissionsMap.entries()).map(([vmId, labs]) => ({
        vm_id: vmId,
        labs: Array.from(labs).sort((left, right) => left.localeCompare(right)),
    }));
}

function renderAdminGroups(items) {
    const container = document.getElementById("admin-groups");
    const countBadge = document.getElementById("admin-groups-count");
    if (!container) {
        return;
    }

    if (!Array.isArray(items) || !items.length) {
        container.innerHTML = '<p class="text-xs text-slate-500">Aucun groupe.</p>';
        if (countBadge) countBadge.textContent = "0";
        return;
    }
    if (countBadge) countBadge.textContent = String(items.length);

    container.innerHTML = items.map((group) => {
        const members = Array.isArray(group.member_details) ? group.member_details : [];
        const memberLabel = members.length
            ? members.map((member) => member.full_name || member.username).join(", ")
            : "Aucun membre";
        const vmLabel = Array.isArray(group.vm_ids) && group.vm_ids.length
            ? group.vm_ids.join(", ")
            : "Aucune VM complète";
        const labScopeLabel = Array.isArray(group.lab_permissions) && group.lab_permissions.length
            ? group.lab_permissions.map((item) => `${item.vm_id}: ${Array.isArray(item.labs) ? item.labs.join(", ") : ""}`).join(" | ")
            : "Aucune portée LAB spécifique";

        return `
            <div class="rounded-lg border border-slate-800 bg-slate-900/70 px-3 py-2">
                <div class="flex items-start justify-between gap-3">
                    <div>
                        <p class="text-sm font-semibold text-slate-100">${escapeHtml(group.name)}</p>
                        <p class="text-xs text-slate-400 mt-1">${escapeHtml(group.description || "Sans description")}</p>
                        <p class="text-[11px] mt-2 text-cyan-300">Membres: ${escapeHtml(memberLabel)}</p>
                        <p class="text-[11px] mt-1 text-emerald-300">VMs: ${escapeHtml(vmLabel)}</p>
                        <p class="text-[11px] mt-1 text-amber-300">LABs: ${escapeHtml(labScopeLabel)}</p>
                    </div>
                    <div class="flex items-center gap-2">
                        <button type="button" data-action="admin-edit-group" data-group-name="${escapeHtml(group.name)}" class="text-xs px-2 py-1 rounded border border-slate-600 text-slate-200 hover:bg-slate-800">Éditer</button>
                        <button type="button" data-action="admin-delete-group" data-group-name="${escapeHtml(group.name)}" class="text-xs px-2 py-1 rounded border border-rose-700 text-rose-200 hover:bg-rose-900/30">Supprimer</button>
                    </div>
                </div>
            </div>
        `;
    }).join("");
}

function renderAdminVMs(items) {
    const container = document.getElementById("admin-vms");
    const countBadge = document.getElementById("admin-vms-count");
    if (!container) {
        return;
    }

    if (!Array.isArray(items) || !items.length) {
        container.innerHTML = '<p class="text-xs text-slate-500">Aucune VM configurée.</p>';
        if (countBadge) countBadge.textContent = "0";
        adminVmListSignature = "";
        return;
    }
    if (countBadge) countBadge.textContent = String(items.length);
    items = [...items].sort((a, b) => String(a.name || a.id || "").localeCompare(String(b.name || b.id || ""), undefined, { numeric: true }));
    const fullAdmin = isAdmin();

    const inventoryByVmId = new Map();
    for (const vmEntry of latestAdminTopologyInventory || []) {
        if (!vmEntry || typeof vmEntry !== "object") {
            continue;
        }
        const vmId = String(vmEntry.vm_id || "");
        if (!vmId) {
            continue;
        }
        inventoryByVmId.set(vmId, {
            error: String(vmEntry.error || "").trim(),
            topologies: Array.isArray(vmEntry.topologies) ? vmEntry.topologies : [],
        });
    }

    const inventorySig = Array.from(inventoryByVmId.entries())
        .map(([vmId, entry]) => {
            const topoSig = (entry.topologies || [])
                .map((topology) => `${String(topology?.lab_name || "")}:${String(topology?.topology_file || "")}:${topology?.running ? "1" : "0"}`)
                .sort()
                .join(";");
            return `${vmId}:${entry.error}:${topoSig}`;
        })
        .sort()
        .join("|");

    const nextSig = items
        .map((vm) => `${String(vm?.id || "")}::${String(vm?.name || "")}::${String(vm?.ip || "")}::${String(vm?.port || "")}::${String(vm?.management_subnet || "")}`)
        .sort()
        .join("|") + `::${inventorySig}`;
    if (nextSig === adminVmListSignature) {
        // Keep cards synced with cached payloads even when list metadata did not change.
        const expectedHours = _currentVmGraphHours();
        for (const vm of items) {
            if (!_isClabVm(vm)) {
                continue;
            }
            const vmId = String(vm?.id || "");
            if (!vmId) {
                continue;
            }
            const cachedPayload = latestAdminVmResourcesById[vmId];
            if (cachedPayload && _hasFreshVmGraphCache(vmId, expectedHours)) {
                renderAdminVmCardResources(vm, cachedPayload);
            }
        }
        return;
    }
    adminVmListSignature = nextSig;

    const vmMetricsElementId = (vmId) => `admin-vm-card-metrics-${encodeURIComponent(String(vmId || ""))}`;
    const isClabVm = (vm) => {
        const vmId = String(vm?.id || "").toLowerCase();
        const vmName = String(vm?.name || "").toLowerCase();
        return vmId.includes("clab") || vmName.includes("clab");
    };

    const detailsState = captureDetailsState(container);

    container.innerHTML = items.map((vm) => {
        const vmId = String(vm.id || "");
        const vmName = String(vm.name || vmId);
        const vmIp = String(vm.ip || "");
        const vmPort = Number(vm.port || 8081);
        const vmToken = String(vm.token || "");
        const vmBaseUrl = String(vm.base_url || `http://${vmIp}:${vmPort}`);
        const vmManagementSubnet = String(vm.management_subnet || "").trim();
        const usesClabApi = Boolean(vm.use_clab_api_server);
        const clabApiUrl = String(vm.clab_api_base_url || "");
        const isClab = isClabVm(vm);
        const metricsId = vmMetricsElementId(vmId);
        const vmInventory = inventoryByVmId.get(vmId) || { error: "", topologies: [] };
        const vmInventoryError = String(vmInventory.error || "");
        const vmTopologies = Array.isArray(vmInventory.topologies) ? vmInventory.topologies : [];

        const collapsed = adminVmCardCollapsedState[vmId] || false;
        const toggleLabel = collapsed ? "+" : "−";

        const inventoryContent = vmInventoryError
            ? `<p class="text-xs text-rose-300">${escapeHtml(vmInventoryError)}</p>`
            : vmTopologies.length
                ? vmTopologies.map((topology) => {
                    const labName = String(topology?.lab_name || "");
                    const topologyFile = String(topology?.topology_file || "");
                    const fileName = String(topology?.file_name || topologyFile || "");
                    const running = Boolean(topology?.running);
                    const statusClass = running
                        ? "border-emerald-700/50 bg-emerald-900/20 text-emerald-200"
                        : "border-slate-700 bg-slate-900 text-slate-300";
                    const startDisabledAttr = running ? "disabled" : "";
                    const startDisabledClass = running ? "opacity-50 cursor-not-allowed" : "";
                    return `
                        <div class="rounded border border-slate-800 bg-slate-950/60 px-2 py-1.5">
                            <div class="flex items-center justify-between gap-2 flex-wrap">
                                <div class="min-w-0">
                                    <p class="text-xs text-slate-200 truncate">${escapeHtml(labName || fileName)}</p>
                                    <p class="text-[11px] text-slate-500 truncate">${escapeHtml(topologyFile || fileName)}</p>
                                </div>
                                <div class="flex items-center gap-1.5">
                                    <span class="text-[10px] px-2 py-0.5 rounded-full border ${statusClass}">${running ? "Running" : "Stopped"}</span>
                                    <button type="button" data-action="view-inventory-yaml" data-vm-id="${escapeHtml(vmId)}" data-lab-name="${escapeHtml(labName)}" data-topology-file="${escapeHtml(topologyFile)}" class="text-[11px] px-2 py-0.5 rounded border border-cyan-700 text-cyan-200 hover:bg-cyan-900/30">View</button>
                                    <button type="button" ${startDisabledAttr} data-action="start-lab-from-inventory" data-vm-id="${escapeHtml(vmId)}" data-lab-name="${escapeHtml(labName)}" data-topology-file="${escapeHtml(topologyFile)}" class="text-[11px] px-2 py-0.5 rounded border border-emerald-700 text-emerald-200 hover:bg-emerald-900/30 ${startDisabledClass}">Start</button>
                                </div>
                            </div>
                        </div>
                    `;
                }).join("")
                : '<p class="text-xs text-slate-500">Aucune topologie détectée pour cette VM.</p>';

        const vmActionButtonsHtml = fullAdmin
            ? `
                        <button type="button" data-action="admin-provision-vm" data-vm-id="${escapeHtml(vmId)}" class="text-xs px-2 py-1 rounded border border-emerald-700 text-emerald-200 hover:bg-emerald-900/30">Provisionner</button>
                        <button type="button" data-action="open-vm-audit-modal" data-vm-id="${escapeHtml(vmId)}" class="text-xs px-2 py-1 rounded border border-fuchsia-700 text-fuchsia-200 hover:bg-fuchsia-900/30">Logs</button>
                        <button type="button" data-action="admin-edit-vm" data-vm-id="${escapeHtml(vmId)}" class="text-xs px-2 py-1 rounded border border-slate-600 text-slate-200 hover:bg-slate-800">Editer</button>
                        <button type="button" data-action="admin-delete-vm" data-vm-id="${escapeHtml(vmId)}" class="text-xs px-2 py-1 rounded border border-rose-700 text-rose-200 hover:bg-rose-900/30">Supprimer</button>
            `
            : `
                        <button type="button" data-action="open-vm-audit-modal" data-vm-id="${escapeHtml(vmId)}" class="text-xs px-2 py-1 rounded border border-fuchsia-700 text-fuchsia-200 hover:bg-fuchsia-900/30">Logs</button>
            `;

        const inventoryHtml = fullAdmin
            ? `
                <details class="mt-3 rounded border border-slate-800 bg-slate-950/50" data-key="admin-vm-topology-${escapeHtml(vmId)}">
                    <summary class="cursor-pointer list-none px-2 py-1.5 flex items-center justify-between gap-2">
                        <span class="text-[11px] uppercase tracking-wide text-amber-300/80">Inventaire topologies (${vmTopologies.length})</span>
                        <span class="text-slate-500">▾</span>
                    </summary>
                    <div class="px-2 pb-2 space-y-1.5">
                        ${inventoryContent}
                    </div>
                </details>
            `
            : "";

        return `
            <div class="rounded-lg border border-slate-800 bg-slate-900/70 px-3 py-2">
                <div class="flex items-start justify-between gap-3">
                    <div>
                        <p class="text-sm font-semibold text-slate-100">${escapeHtml(vmName)}</p>
                        <p class="text-xs text-slate-400 mt-1">${escapeHtml(vmId)} · ${escapeHtml(vmIp)}:${escapeHtml(vmPort)}</p>
                        <p class="text-[11px] mt-1 text-slate-500">${escapeHtml(vmBaseUrl)}</p>
                        ${vmManagementSubnet ? `<p class="text-[11px] mt-1 text-amber-300">Subnet mgmt: ${escapeHtml(vmManagementSubnet)}</p>` : ""}
                        <p class="text-[11px] mt-1.5">
                            ${usesClabApi
                                ? `<span class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full border border-cyan-700/60 bg-cyan-900/20 text-cyan-300">
                                    <span class="w-1.5 h-1.5 rounded-full bg-cyan-400 inline-block"></span>clab-api-server
                                   </span>
                                   ${clabApiUrl ? `<span class="ml-1 text-slate-500">${escapeHtml(clabApiUrl)}</span>` : ""}`
                                : `<span class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full border border-slate-700 bg-slate-800/30 text-slate-500">
                                    <span class="w-1.5 h-1.5 rounded-full bg-slate-600 inline-block"></span>vm-agent only
                                   </span>`
                            }
                        </p>
                        ${fullAdmin ? `<p class="text-[11px] mt-1 text-amber-300">Token: ${escapeHtml(maskToken(vmToken))}</p>` : ""}
                    </div>
                    <div class="flex items-center gap-2">
                        ${vmActionButtonsHtml}
                    </div>
                </div>
                ${isClab ? `
                    <div class="mt-3 rounded border border-slate-800 bg-slate-950/50 p-2">
                        <div class="flex items-center justify-between mb-2">
                            <p class="text-[11px] text-cyan-300">Ressources C-Lab · ${escapeHtml(vmName)}</p>
                            <button type="button" data-action="toggle-vm-metrics" data-vm-id="${escapeHtml(vmId)}" class="text-[11px] px-2 py-0.5 rounded border border-slate-700 text-slate-300 hover:bg-slate-800 transition-all">${toggleLabel}</button>
                        </div>
                        <div id="${escapeHtml(metricsId)}" class="space-y-2 transition-all ${collapsed ? "hidden" : ""}">
                            <p class="text-xs text-slate-500">Chargement des métriques VM...</p>
                        </div>
                    </div>
                ` : ""}
                ${inventoryHtml}
            </div>
        `;
    }).join("");

    restoreDetailsState(container, detailsState);

    // Rehydrate card metrics from cache immediately after DOM rebuild.
    const expectedHours = _currentVmGraphHours();
    for (const vm of items) {
        if (!_isClabVm(vm)) {
            continue;
        }
        const vmId = String(vm?.id || "");
        if (!vmId) {
            continue;
        }
        const cachedPayload = latestAdminVmResourcesById[vmId];
        if (cachedPayload && _hasFreshVmGraphCache(vmId, expectedHours)) {
            renderAdminVmCardResources(vm, cachedPayload);
        } else {
            const container = document.getElementById(_vmMetricsElementId(vmId));
            if (container) {
                container.classList.toggle("hidden", adminVmCardCollapsedState[vmId] || false);
            }
        }
    }
}

function renderCentralResourceCards(current) {
    const cpu = current?.cpu_percent;
    const memoryPercent = current?.memory?.percent;
    const memoryUsed = current?.memory?.used;
    const memoryTotal = current?.memory?.total;
    const diskPercent = current?.disk?.percent;
    const diskUsed = current?.disk?.used;
    const diskTotal = current?.disk?.total;

    const load1 = current?.load_avg?.one;
    const load5 = current?.load_avg?.five;
    const load15 = current?.load_avg?.fifteen;

    return `
        <div class="grid grid-cols-1 md:grid-cols-3 gap-3">
            ${resourceBlock("CPU", cpu, null, null)}
            ${resourceBlock("RAM", memoryPercent, memoryUsed, memoryTotal)}
            ${resourceBlock("Disque", diskPercent, diskUsed, diskTotal)}
        </div>
        <p class="text-[11px] text-slate-500">
            Charge moyenne: 1m ${load1 !== undefined && load1 !== null ? Number(load1).toFixed(2) : "-"}
            · 5m ${load5 !== undefined && load5 !== null ? Number(load5).toFixed(2) : "-"}
            · 15m ${load15 !== undefined && load15 !== null ? Number(load15).toFixed(2) : "-"}
        </p>
    `;
}

function renderCentralResourceSummaryBar(current) {
    const cpu = current?.cpu_percent;
    const memoryPercent = current?.memory?.percent;
    const diskPercent = current?.disk?.percent;

    const summaryItem = (label, value, toneClass) => `
        <div class="rounded border border-slate-800 bg-slate-950/70 px-2 py-1.5 min-w-[110px]">
            <p class="text-[10px] uppercase tracking-wide text-slate-500">${label}</p>
            <p class="text-sm font-semibold ${toneClass}">${percentage(value)}</p>
        </div>
    `;

    return `
        <div class="flex flex-wrap items-center gap-2">
            ${summaryItem("CPU", cpu, "text-cyan-300")}
            ${summaryItem("RAM", memoryPercent, "text-amber-300")}
            ${summaryItem("Disque", diskPercent, "text-emerald-300")}
        </div>
    `;
}

function updateAdminCentralPanelVisibility() {
    const body = document.getElementById("admin-central-resources-body");
    const summary = document.getElementById("admin-central-summary");
    const toggle = document.getElementById("admin-central-toggle");

    if (body) {
        body.classList.toggle("hidden", adminCentralCollapsed);
    }
    if (summary) {
        summary.classList.toggle("hidden", !adminCentralCollapsed);
    }
    if (toggle) {
        toggle.textContent = adminCentralCollapsed ? "Afficher" : "Réduire";
    }
}

function renderCentralResourceGraph(history, windowHours, aggregation = null) {
    const validHistory = Array.isArray(history) ? history : [];
    const cpuPoints = validHistory
        .map((item) => item?.cpu_percent)
        .filter((value) => value !== null && value !== undefined && Number.isFinite(Number(value)));
    const memoryPoints = validHistory
        .map((item) => item?.memory?.percent)
        .filter((value) => value !== null && value !== undefined && Number.isFinite(Number(value)));
    const diskPoints = validHistory
        .map((item) => item?.disk?.percent)
        .filter((value) => value !== null && value !== undefined && Number.isFinite(Number(value)));

    if (!cpuPoints.length && !memoryPoints.length && !diskPoints.length) {
        return '<p class="text-xs text-slate-500">Pas encore assez de données pour tracer le graphe.</p>';
    }

    const isLight = document.body.classList.contains("theme-light");
    const svgBg = isLight ? "#f1f5f9" : "#03070f";
    const svgBorder = isLight ? "#cbd5e1" : "#1e293b";
    const gridBase = isLight ? "#cbd5e1" : "#334155";
    const gridSub = isLight ? "#e2e8f0" : "#1e293b";
    const labelColor = isLight ? "#475569" : "#94a3b8";
    const axisColor = isLight ? "#94a3b8" : "#475569";
    const wrapBg = isLight ? "#f8fafc" : "rgba(15,23,42,0.7)";
    const wrapBorder = isLight ? "#cbd5e1" : "#1e293b";

    // Extract timestamps for x-axis labels
    const timestamps = validHistory.map((item) => item?.timestamp).filter(Boolean);
    const firstTs = timestamps.length ? new Date(timestamps[0]) : null;
    const lastTs = timestamps.length ? new Date(timestamps[timestamps.length - 1]) : null;
    const fmtTime = (d) => d instanceof Date && !isNaN(d) ? d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" }) : "";
    const fmtDateTime = (d) => d instanceof Date && !isNaN(d) ? d.toLocaleDateString("fr-FR", { day: "2-digit", month: "2-digit" }) + " " + fmtTime(d) : "";
    const startLabel = firstTs ? fmtDateTime(firstTs) : "";
    const midLabel = (firstTs && lastTs) ? fmtTime(new Date((firstTs.getTime() + lastTs.getTime()) / 2)) : "";
    const endLabel = lastTs ? fmtTime(lastTs) : "";
    const pointCount = validHistory.length;
    const actualSpanMin = (firstTs && lastTs) ? Math.round((lastTs - firstTs) / 60000) : 0;
    const spanLabel = actualSpanMin >= 60 ? `${(actualSpanMin / 60).toFixed(1)}h de données (${pointCount} pts)` : `${actualSpanMin}min de données (${pointCount} pts)`;

    const width = 920;
    const height = 200;
    const cpuPath = buildSeriesPath(cpuPoints.length ? cpuPoints : [0], width, height);
    const memoryPath = buildSeriesPath(memoryPoints.length ? memoryPoints : [0], width, height);
    const diskPath = buildSeriesPath(diskPoints.length ? diskPoints : [0], width, height);

    // Y-axis labels rendered as HTML (outside SVG) to avoid distortion from preserveAspectRatio=none
    const yAxisHtml = `
        <div style="display:flex;flex-direction:column;justify-content:space-between;padding-right:5px;font-size:10px;color:${axisColor};text-align:right;min-width:30px;line-height:1">
            <span>100%</span><span>75%</span><span>50%</span><span>25%</span><span>0%</span>
        </div>`;

    const aggregationLabel = aggregation === "day"
        ? "moyennes par jour"
        : aggregation === "hour"
            ? "moyennes par heure"
            : "";

    return `
        <div style="background:${wrapBg};border:1px solid ${wrapBorder};border-radius:0.5rem;padding:0.75rem">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
                <p style="font-size:12px;color:${labelColor};font-weight:500">Tendance CPU/RAM/Disque — fenêtre ${windowHours}h${aggregationLabel ? ` (${aggregationLabel})` : ""}</p>
                <p style="font-size:11px;color:${axisColor}">${spanLabel}</p>
            </div>
            <div style="display:flex;align-items:stretch">
                ${yAxisHtml}
                <div style="flex:1;min-width:0">
                    <svg viewBox="0 0 ${width} ${height}" style="width:100%;height:12rem;display:block;border-radius:0.375rem;border:1px solid ${svgBorder};background:${svgBg}" preserveAspectRatio="none">
                        <line x1="0" y1="${height}" x2="${width}" y2="${height}" stroke="${gridBase}" stroke-width="1" />
                        <line x1="0" y1="${height * 0.75}" x2="${width}" y2="${height * 0.75}" stroke="${gridSub}" stroke-width="1" stroke-dasharray="4 3" />
                        <line x1="0" y1="${height * 0.5}" x2="${width}" y2="${height * 0.5}" stroke="${gridSub}" stroke-width="1" stroke-dasharray="4 3" />
                        <line x1="0" y1="${height * 0.25}" x2="${width}" y2="${height * 0.25}" stroke="${gridSub}" stroke-width="1" stroke-dasharray="4 3" />
                        <path d="${cpuPath}" fill="none" stroke="#22d3ee" stroke-width="1.5" />
                        <path d="${memoryPath}" fill="none" stroke="#f59e0b" stroke-width="1.5" />
                        <path d="${diskPath}" fill="none" stroke="#10b981" stroke-width="1.5" />
                    </svg>
                    <div style="display:flex;justify-content:space-between;margin-top:2px;font-size:10px;color:${axisColor}">
                        <span>${escapeHtml(startLabel)}</span>
                        <span>${escapeHtml(midLabel)}</span>
                        <span>${escapeHtml(endLabel)}</span>
                    </div>
                </div>
            </div>
            <div style="display:flex;align-items:center;gap:16px;margin-top:8px;font-size:11px;color:${labelColor}">
                <span style="display:inline-flex;align-items:center;gap:5px"><span style="display:inline-block;width:14px;height:2px;background:#22d3ee;border-radius:1px"></span>CPU</span>
                <span style="display:inline-flex;align-items:center;gap:5px"><span style="display:inline-block;width:14px;height:2px;background:#f59e0b;border-radius:1px"></span>RAM</span>
                <span style="display:inline-flex;align-items:center;gap:5px"><span style="display:inline-block;width:14px;height:2px;background:#10b981;border-radius:1px"></span>Disque</span>
            </div>
        </div>
    `;
}

function renderAdminCentralResources(payload) {
    const container = document.getElementById("admin-central-resources");
    const summary = document.getElementById("admin-central-summary");
    if (!container) {
        return;
    }

    if (!payload || !payload.current) {
        container.innerHTML = '<p class="text-xs text-slate-500">Métriques central indisponibles.</p>';
        if (summary) {
            summary.innerHTML = '<p class="text-xs text-slate-500">Résumé ressources indisponible.</p>';
        }
        updateAdminCentralPanelVisibility();
        return;
    }

    const cards = renderCentralResourceCards(payload.current);
    const summaryBar = renderCentralResourceSummaryBar(payload.current);
    const graph = renderCentralResourceGraph(payload.history || [], payload.window_hours || 24);
    const stamp = payload.current.timestamp ? new Date(payload.current.timestamp).toLocaleString("fr-FR") : "-";

    if (summary) {
        summary.innerHTML = summaryBar;
    }

    container.innerHTML = `
        ${cards}
        ${graph}
        <p class="text-[11px] text-slate-500">Dernier échantillon: ${escapeHtml(stamp)}</p>
    `;
    updateAdminCentralPanelVisibility();
}

function syncVmMetricsSelectOptions() {
    const select = document.getElementById("admin-vm-metrics-id");
    if (!(select instanceof HTMLSelectElement)) {
        return;
    }

    const currentValue = String(select.value || "");
    const options = ['<option value="">-- VM --</option>'];
    const sortedVMsForSelect = [...(latestAdminVMs || [])].sort((a, b) => String(a.name || a.id || "").localeCompare(String(b.name || b.id || ""), undefined, { numeric: true }));
    for (const vm of sortedVMsForSelect) {
        const vmId = String(vm?.id || "").trim();
        if (!vmId) {
            continue;
        }
        const vmName = String(vm?.name || vmId);
        options.push(`<option value="${escapeHtml(vmId)}">${escapeHtml(vmName)} (${escapeHtml(vmId)})</option>`);
    }
    select.innerHTML = options.join("");

    if (currentValue && Array.from(select.options).some((option) => option.value === currentValue)) {
        select.value = currentValue;
        return;
    }
    if (sortedVMsForSelect.length > 0) {
        select.value = String(sortedVMsForSelect[0]?.id || "");
    }
}

function renderAdminVmResources(payload) {
    const container = document.getElementById("admin-vm-resources");
    if (!container) {
        return;
    }

    if (!payload || !Array.isArray(payload.history)) {
        container.innerHTML = '<p class="text-xs text-slate-500">Métriques VM indisponibles.</p>';
        return;
    }

    const vmName = String(payload.current?.vm_name || payload.history[payload.history.length - 1]?.vm_name || payload.vm_id || "VM");
    const vmId = String(payload.vm_id || payload.current?.vm_id || "");
    const graph = renderCentralResourceGraph(payload.history || [], payload.window_hours || 24, payload.aggregation || null);
    const stamp = payload.current?.timestamp ? new Date(payload.current.timestamp).toLocaleString("fr-FR") : "-";

    container.innerHTML = `
        <div class="flex items-center justify-between">
            <h4 class="text-xs uppercase tracking-wide text-slate-400">Ressources VM · ${escapeHtml(vmName)}${vmId ? ` (${escapeHtml(vmId)})` : ""}</h4>
        </div>
        ${payload.current ? renderCentralResourceCards(payload.current) : '<p class="text-xs text-slate-500">Pas encore de mesures pour cette VM.</p>'}
        ${graph}
        <p class="text-[11px] text-slate-500">Dernier échantillon VM: ${escapeHtml(stamp)}</p>
    `;
}

function _vmMetricsElementId(vmId) {
    return `admin-vm-card-metrics-${encodeURIComponent(String(vmId || ""))}`;  
}

function _currentVmGraphHours() {
    const hoursSelect = document.getElementById("admin-vm-hours");
    if (hoursSelect instanceof HTMLSelectElement) {
        const picked = Number(hoursSelect.value || 24);
        if (Number.isFinite(picked)) {
            adminVmGraphHours = picked;
        }
    }
    return Number.isFinite(adminVmGraphHours) ? adminVmGraphHours : 24;
}

function _isVmGraphExpanded(vmId) {
    return !(adminVmCardCollapsedState[String(vmId || "")] || false);
}

function _hasFreshVmGraphCache(vmId, hours) {
    const id = String(vmId || "");
    if (!id || !latestAdminVmResourcesById[id]) {
        return false;
    }
    const meta = latestAdminVmResourcesMetaById[id];
    return Boolean(meta && Number(meta.hours || 0) === Number(hours || 24));
}

function _toggleVmMetrics(vmId) {
    const key = `vlm:admin:vm-metrics-collapsed:${vmId}`;
    adminVmCardCollapsedState[vmId] = !adminVmCardCollapsedState[vmId];
    try {
        localStorage.setItem(key, adminVmCardCollapsedState[vmId] ? "1" : "0");
    } catch (_) {}
    
    const container = document.getElementById(_vmMetricsElementId(vmId));
    const button = document.querySelector(`[data-action="toggle-vm-metrics"][data-vm-id="${vmId.replace(/"/g, '\\"')}"]`);
    if (container) {
        container.classList.toggle("hidden", adminVmCardCollapsedState[vmId]);
    }
    if (button) {
        button.textContent = adminVmCardCollapsedState[vmId] ? "+" : "−";
    }

    if (_isVmGraphExpanded(vmId)) {
        const hours = _currentVmGraphHours();
        if (_hasFreshVmGraphCache(vmId, hours)) {
            const vm = (latestAdminVMs || []).find((item) => String(item?.id || "") === String(vmId || ""));
            if (vm) {
                renderAdminVmCardResources(vm, latestAdminVmResourcesById[String(vmId || "")]);
            }
        } else {
            void refreshAdminVmCardsResources({ force: true, vmIds: [String(vmId || "")] });
        }
    }
}

function _loadVmMetricsCollapsedStates() {
    Object.keys(adminVmCardCollapsedState).forEach((key) => {
        delete adminVmCardCollapsedState[key];
    });
    for (const vm of latestAdminVMs || []) {
        const vmId = String(vm?.id || "");
        if (!vmId) continue;
        const key = `vlm:admin:vm-metrics-collapsed:${vmId}`;
        try {
            const stored = localStorage.getItem(key);
            adminVmCardCollapsedState[vmId] = stored === null ? true : stored === "1";
        } catch (_) {
            adminVmCardCollapsedState[vmId] = true;
        }
    }
}

function _isClabVm(vm) {
    const vmId = String(vm?.id || "").toLowerCase();
    const vmName = String(vm?.name || "").toLowerCase();
    return vmId.includes("clab") || vmName.includes("clab");
}

function renderAdminVmCardResources(vm, payload) {
    const vmId = String(vm?.id || "");
    const vmName = String(vm?.name || vmId || "VM");
    const container = document.getElementById(_vmMetricsElementId(vmId));
    if (!container) {
        return;
    }

    if (!payload || !Array.isArray(payload.history)) {
        container.innerHTML = '<p class="text-xs text-slate-500">Métriques VM indisponibles.</p>';
        container.classList.toggle("hidden", adminVmCardCollapsedState[vmId] || false);
        return;
    }

    const graph = renderCentralResourceGraph(payload.history || [], payload.window_hours || 24, payload.aggregation || null);
    const stamp = payload.current?.timestamp ? new Date(payload.current.timestamp).toLocaleString("fr-FR") : "-";

    container.innerHTML = `
        ${payload.current ? renderCentralResourceCards(payload.current) : '<p class="text-xs text-slate-500">Pas encore de mesures pour cette VM.</p>'}
        ${graph}
        <p class="text-[11px] text-slate-500">Dernier échantillon: ${escapeHtml(stamp)} · ${escapeHtml(vmName)}</p>
    `;
    container.classList.toggle("hidden", adminVmCardCollapsedState[vmId] || false);
}

async function refreshAdminVmCardsResources(options = {}) {
    const force = Boolean(options.force);
    const vmIdsFilter = Array.isArray(options.vmIds)
        ? new Set(options.vmIds.map((value) => String(value || "")).filter(Boolean))
        : null;

    const hoursValue = _currentVmGraphHours();

    if (!force && Date.now() - adminVmCardsLastRefreshAt < ADMIN_VM_GRAPH_REFRESH_MS) {
        return;
    }

    if (adminVmCardsRefreshInFlight) {
        adminVmCardsRefreshQueued = true;
        return;
    }

    adminVmCardsRefreshInFlight = true;
    adminVmCardFetchSeq += 1;

    const clabVms = (latestAdminVMs || []).filter((vm) => _isClabVm(vm));
    const targetVms = clabVms.filter((vm) => {
        const vmId = String(vm?.id || "");
        if (!vmId) {
            return false;
        }
        if (vmIdsFilter && !vmIdsFilter.has(vmId)) {
            return false;
        }
        if (!vmIdsFilter && !_isVmGraphExpanded(vmId)) {
            return false;
        }
        if (!force && _hasFreshVmGraphCache(vmId, hoursValue)) {
            return false;
        }
        return true;
    });

    if (!targetVms.length) {
        adminVmCardsLastRefreshAt = Date.now();
        return;
    }

    try {
        await Promise.all(targetVms.map(async (vm) => {
            const vmId = String(vm?.id || "").trim();
            if (!vmId) {
                return;
            }

            const endpoint = `/api/admin/vms/${encodeURIComponent(vmId)}/resources/aggregated?hours=${encodeURIComponent(hoursValue)}&bucket=auto`;

            try {
                const response = await apiFetch(endpoint);
                if (!response.ok) {
                    const container = document.getElementById(_vmMetricsElementId(vmId));
                    if (container) {
                        container.innerHTML = `<p class="text-xs text-rose-400">Erreur HTTP ${response.status}</p>`;
                    }
                    return;
                }
                const payload = await response.json();
                latestAdminVmResourcesById[vmId] = payload || null;
                latestAdminVmResourcesMetaById[vmId] = {
                    hours: hoursValue,
                    fetchedAt: Date.now(),
                };
                renderAdminVmCardResources(vm, payload || null);
            } catch (err) {
                const container = document.getElementById(_vmMetricsElementId(vmId));
                if (container) {
                    container.innerHTML = `<p class="text-xs text-rose-400">Erreur: ${escapeHtml(String(err?.message || "inconnue"))}</p>`;
                }
            }
        }));
    } finally {
        adminVmCardsLastRefreshAt = Date.now();
        adminVmCardsRefreshInFlight = false;
        if (adminVmCardsRefreshQueued) {
            adminVmCardsRefreshQueued = false;
            void refreshAdminVmCardsResources();
        }
    }
}

function showGroupFormModal(group = null) {
    const isEdit = Boolean(group);
    const inventory = collectGroupInventory(group?.name || "");
    const selectedMembers = new Set((group?.members || []).map((member) => String(member || "").toLowerCase()));
    const selectedVmIds = new Set((group?.vm_ids || []).map((vmId) => String(vmId || "")));
    const selectedLabsByVm = new Map();
    for (const permission of group?.lab_permissions || []) {
        const vmId = String(permission?.vm_id || "").trim();
        if (!vmId) {
            continue;
        }
        selectedLabsByVm.set(vmId, new Set((permission?.labs || []).map((lab) => String(lab || "").trim()).filter(Boolean)));
    }

    const usersHtml = inventory.users.length
        ? inventory.users.map((user) => {
            const checked = selectedMembers.has(user.username);
            const disabled = Boolean(user.assignedGroup) && !checked;
            return `
                <label class="flex items-start gap-2 rounded border ${disabled ? "border-slate-800 opacity-60" : "border-slate-700"} bg-slate-950 px-2 py-2 text-xs">
                    <input type="checkbox" name="members" value="${escapeHtml(user.username)}" ${checked ? "checked" : ""} ${disabled ? "disabled" : ""} class="mt-0.5" />
                    <span>
                        <span class="text-slate-100">${escapeHtml(user.label)}</span>
                        ${user.assignedGroup ? `<span class="block text-[11px] text-amber-300">Déjà dans ${escapeHtml(user.assignedGroup)}</span>` : ""}
                    </span>
                </label>
            `;
        }).join("")
        : '<p class="text-xs text-slate-500">Aucun utilisateur disponible.</p>';

    const vmsHtml = inventory.vms.length
        ? inventory.vms.map((vm) => {
            const vmSelected = selectedVmIds.has(vm.id);
            const labsHtml = vm.labs.length
                ? vm.labs.map((labName) => {
                    const checked = selectedLabsByVm.get(vm.id)?.has(labName) || false;
                    return `
                        <label class="inline-flex items-center gap-2 rounded border border-slate-800 bg-slate-950 px-2 py-1 text-xs">
                            <input type="checkbox" data-lab-vm-id="${escapeHtml(vm.id)}" data-lab-name="${escapeHtml(labName)}" ${checked ? "checked" : ""} />
                            <span>${escapeHtml(labName)}</span>
                        </label>
                    `;
                }).join("")
                : '<p class="text-[11px] text-slate-500">Aucun LAB détecté sur cette VM.</p>';

            return `
                <div class="rounded-lg border border-slate-700 bg-slate-900/60 p-2">
                    <label class="inline-flex items-center gap-2 text-xs text-emerald-200 font-semibold">
                        <input type="checkbox" name="vm_ids" value="${escapeHtml(vm.id)}" ${vmSelected ? "checked" : ""} />
                        <span>${escapeHtml(vm.label)}</span>
                    </label>
                    <p class="mt-1 text-[11px] text-slate-500">Coche la VM pour accès complet, ou coche seulement des LABs ci-dessous.</p>
                    <div class="mt-2 flex flex-wrap gap-2">${labsHtml}</div>
                </div>
            `;
        }).join("")
        : '<p class="text-xs text-slate-500">Aucune VM/LAB disponible. Lance une collecte d\'état puis réessaie.</p>';

    const overlay = showOverlayModal({
        title: isEdit ? `Éditer le groupe ${group.name}` : "Créer un groupe",
        tone: "slate",
        widthClass: "max-w-3xl",
        bodyHtml: `
            <form id="admin-group-form" class="grid gap-4 md:grid-cols-2">
                <div>
                    <label class="block text-xs uppercase tracking-wide text-slate-400 mb-1">Nom du groupe</label>
                    <input name="name" value="${escapeHtml(group?.name || "")}" required class="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-cyan-500" />
                </div>
                <div>
                    <label class="block text-xs uppercase tracking-wide text-slate-400 mb-1">Description</label>
                    <input name="description" value="${escapeHtml(group?.description || "")}" class="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-cyan-500" />
                </div>
                <div class="md:col-span-2">
                    <label class="block text-xs uppercase tracking-wide text-slate-400 mb-1">Utilisateurs du groupe</label>
                    <div class="grid gap-2 sm:grid-cols-2">${usersHtml}</div>
                </div>
                <div class="md:col-span-2">
                    <label class="block text-xs uppercase tracking-wide text-slate-400 mb-1">Périmètre VM / LAB</label>
                    <div class="space-y-2">${vmsHtml}</div>
                </div>
                <p id="admin-group-error" class="hidden text-sm text-rose-300 md:col-span-2"></p>
                <div class="md:col-span-2 flex items-center justify-end gap-2">
                    <button type="button" data-modal-close="true" class="text-xs px-3 py-2 rounded border border-slate-600 hover:bg-slate-800">Annuler</button>
                    <button type="submit" id="admin-group-submit" class="text-xs px-3 py-2 rounded border border-cyan-700 text-cyan-200 hover:bg-cyan-900/30">${isEdit ? "Enregistrer" : "Créer"}</button>
                </div>
            </form>
        `,
    });

    const form = overlay?.querySelector("#admin-group-form");
    const errorNode = overlay?.querySelector("#admin-group-error");
    const submitButton = overlay?.querySelector("#admin-group-submit");
    if (!(form instanceof HTMLFormElement) || !(errorNode instanceof HTMLElement) || !(submitButton instanceof HTMLButtonElement)) {
        return;
    }

    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        errorNode.classList.add("hidden");
        submitButton.disabled = true;

        try {
            const formData = new FormData(form);
            const payload = {
                name: String(formData.get("name") || "").trim(),
                description: String(formData.get("description") || "").trim(),
                members: Array.from(form.querySelectorAll("input[name='members']:checked")).map((input) => String(input.value || "").toLowerCase()),
                vm_ids: Array.from(form.querySelectorAll("input[name='vm_ids']:checked")).map((input) => String(input.value || "")),
                lab_permissions: buildGroupLabPermissionsFromForm(form),
            };

            const response = await apiFetch(isEdit ? `/api/admin/groups/${encodeURIComponent(group.name)}` : "/api/admin/groups", {
                method: isEdit ? "PATCH" : "POST",
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
                throw new Error(detail);
            }

            closeOverlayModal();
            await refreshAdminPanel();
            showToastMessage(isEdit ? "Groupe mis à jour" : "Groupe créé", `Le groupe ${payload.name} a été enregistré.`);
        } catch (error) {
            errorNode.textContent = error?.message || "Enregistrement impossible.";
            errorNode.classList.remove("hidden");
        } finally {
            submitButton.disabled = false;
        }
    });
}

// ─────────────────────────────────────────────────────────────────────────────
// Stats panel
// ─────────────────────────────────────────────────────────────────────────────

function _statsBarChart(series, color) {
    // series: [{bucket, count}]  — render a tiny inline bar chart as HTML
    if (!series || !series.length) return '<p class="text-[11px] text-slate-500">Aucune donnée</p>';
    const max = Math.max(...series.map((s) => s.count), 1);
    const isLight = document.body.classList.contains("theme-light");
    const barBg = isLight ? "rgba(0,0,0,0.06)" : "rgba(255,255,255,0.04)";
    const labelColor = isLight ? "#64748b" : "#64748b";
    const bars = series.map((s) => {
        const pct = Math.round((s.count / max) * 100);
        const label = s.bucket.length === 10 ? s.bucket.slice(5) : s.bucket.slice(5); // MM-DD or MM
        return `<div style="display:flex;flex-direction:column;align-items:center;flex:1;min-width:0;gap:2px" title="${s.bucket}: ${s.count}">
            <span style="font-size:9px;color:${labelColor};line-height:1">${s.count > 0 ? s.count : ""}</span>
            <div style="width:100%;background:${barBg};border-radius:2px;height:48px;display:flex;align-items:flex-end;overflow:hidden">
                <div style="width:100%;height:${pct}%;background:${color};border-radius:2px 2px 0 0;min-height:${s.count > 0 ? 2 : 0}px;transition:height .3s"></div>
            </div>
            <span style="font-size:9px;color:${labelColor};line-height:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:100%">${label}</span>
        </div>`;
    }).join("");
    return `<div style="display:flex;gap:2px;align-items:flex-end;height:72px">${bars}</div>`;
}

function renderAdminStats(stats) {
    const container = document.getElementById("admin-stats-body");
    if (!container) return;
    if (!stats) {
        container.innerHTML = '<p class="text-xs text-slate-500">Données indisponibles.</p>';
        return;
    }

    const gran = document.getElementById("stats-granularity")?.value || "week";
    const isLight = document.body.classList.contains("theme-light");
    const cardBg = isLight ? "#f8fafc" : "rgba(15,23,42,0.7)";
    const cardBorder = isLight ? "#cbd5e1" : "#1e293b";
    const labelColor = isLight ? "#475569" : "#94a3b8";
    const subColor = isLight ? "#64748b" : "#64748b";
    const textColor = isLight ? "#0f172a" : "#f1f5f9";

    const s = stats.summary || {};
    const logins = stats.logins || {};
    const reservations = stats.reservations || {};

    const loginSeries = gran === "day" ? logins.by_day : gran === "week" ? logins.by_week : logins.by_month;
    const resvSeries = gran === "day" ? reservations.by_day : gran === "week" ? reservations.by_week : reservations.by_month;

    // Summary cards
    const summaryCards = [
        { label: "Connexions totales", value: s.total_logins ?? 0, color: "#22d3ee" },
        { label: "Réservations totales", value: s.total_reservations ?? 0, color: "#a78bfa" },
        { label: "Utilisateurs actifs", value: s.distinct_users_logged_in ?? 0, color: "#f59e0b" },
        { label: "Labs réservés (distincts)", value: s.distinct_labs_reserved ?? 0, color: "#10b981" },
    ].map((c) => `
        <div style="background:${cardBg};border:1px solid ${cardBorder};border-left:3px solid ${c.color};border-radius:0.5rem;padding:10px 14px">
            <p style="font-size:11px;color:${labelColor};text-transform:uppercase;letter-spacing:0.05em">${c.label}</p>
            <p style="font-size:22px;font-weight:700;color:${c.color};margin-top:2px">${c.value}</p>
        </div>`).join("");

    // Top users by logins table
    const topLoginUsers = (logins.top_users || []).slice(0, 10).map((u, i) => `
        <tr>
            <td style="padding:4px 8px;font-size:11px;color:${subColor}">${i + 1}</td>
            <td style="padding:4px 8px;font-size:12px;color:${textColor};font-weight:500">${escapeHtml(u.full_name)}</td>
            <td style="padding:4px 8px;font-size:11px;color:${labelColor}">${escapeHtml(u.username)}</td>
            <td style="padding:4px 8px;font-size:12px;color:#22d3ee;font-weight:600;text-align:right">${u.login_count}</td>
            <td style="padding:4px 8px;font-size:11px;color:${subColor};text-align:right">${u.active_days}j</td>
        </tr>`).join("");

    // Top users by reservations
    const topResvUsers = (reservations.top_users || []).slice(0, 10).map((u, i) => `
        <tr>
            <td style="padding:4px 8px;font-size:11px;color:${subColor}">${i + 1}</td>
            <td style="padding:4px 8px;font-size:12px;color:${textColor};font-weight:500">${escapeHtml(u.full_name)}</td>
            <td style="padding:4px 8px;font-size:11px;color:${labelColor}">${escapeHtml(u.username)}</td>
            <td style="padding:4px 8px;font-size:12px;color:#a78bfa;font-weight:600;text-align:right">${u.reservation_count}</td>
        </tr>`).join("");

    // Top labs
    const topLabs = (reservations.top_labs || []).slice(0, 10).map((l, i) => {
        const parts = l.lab_key.split("/");
        const labName = parts.length > 1 ? parts[1] : l.lab_key;
        const vmId = parts.length > 1 ? parts[0] : "";
        return `<tr>
            <td style="padding:4px 8px;font-size:11px;color:${subColor}">${i + 1}</td>
            <td style="padding:4px 8px;font-size:12px;color:${textColor};font-weight:500">${escapeHtml(labName)}</td>
            <td style="padding:4px 8px;font-size:11px;color:${labelColor}">${escapeHtml(vmId)}</td>
            <td style="padding:4px 8px;font-size:12px;color:#10b981;font-weight:600;text-align:right">${l.count}</td>
        </tr>`;
    }).join("");

    const tableStyle = `width:100%;border-collapse:collapse`;
    const thStyle = `padding:4px 8px;font-size:10px;color:${labelColor};text-transform:uppercase;letter-spacing:0.05em;text-align:left;border-bottom:1px solid ${cardBorder}`;

    const genAt = stats.generated_at ? new Date(stats.generated_at).toLocaleString("fr-FR") : "";

    container.innerHTML = `
        <!-- Summary KPIs -->
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin-bottom:16px">
            ${summaryCards}
        </div>

        <!-- Charts row -->
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:16px">
            <div style="background:${cardBg};border:1px solid ${cardBorder};border-radius:0.5rem;padding:12px">
                <p style="font-size:11px;color:${labelColor};text-transform:uppercase;letter-spacing:0.05em;margin-bottom:8px">Connexions</p>
                ${_statsBarChart(loginSeries, "#22d3ee")}
            </div>
            <div style="background:${cardBg};border:1px solid ${cardBorder};border-radius:0.5rem;padding:12px">
                <p style="font-size:11px;color:${labelColor};text-transform:uppercase;letter-spacing:0.05em;margin-bottom:8px">Réservations</p>
                ${_statsBarChart(resvSeries, "#a78bfa")}
            </div>
        </div>

        <!-- Tables row -->
        <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:10px">
            <div style="background:${cardBg};border:1px solid ${cardBorder};border-radius:0.5rem;overflow:hidden">
                <p style="font-size:11px;color:#22d3ee;text-transform:uppercase;letter-spacing:0.05em;padding:8px 10px;border-bottom:1px solid ${cardBorder}">Top utilisateurs · connexions</p>
                <table style="${tableStyle}">
                    <thead><tr>
                        <th style="${thStyle}">#</th>
                        <th style="${thStyle}">Nom</th>
                        <th style="${thStyle}">Login</th>
                        <th style="${thStyle};text-align:right">Nb</th>
                        <th style="${thStyle};text-align:right">Jours</th>
                    </tr></thead>
                    <tbody>${topLoginUsers || `<tr><td colspan="5" style="padding:8px;font-size:11px;color:${subColor}">Aucune donnée</td></tr>`}</tbody>
                </table>
            </div>
            <div style="background:${cardBg};border:1px solid ${cardBorder};border-radius:0.5rem;overflow:hidden">
                <p style="font-size:11px;color:#a78bfa;text-transform:uppercase;letter-spacing:0.05em;padding:8px 10px;border-bottom:1px solid ${cardBorder}">Top utilisateurs · réservations</p>
                <table style="${tableStyle}">
                    <thead><tr>
                        <th style="${thStyle}">#</th>
                        <th style="${thStyle}">Nom</th>
                        <th style="${thStyle}">Login</th>
                        <th style="${thStyle};text-align:right">Nb</th>
                    </tr></thead>
                    <tbody>${topResvUsers || `<tr><td colspan="4" style="padding:8px;font-size:11px;color:${subColor}">Aucune donnée</td></tr>`}</tbody>
                </table>
            </div>
            <div style="background:${cardBg};border:1px solid ${cardBorder};border-radius:0.5rem;overflow:hidden">
                <p style="font-size:11px;color:#10b981;text-transform:uppercase;letter-spacing:0.05em;padding:8px 10px;border-bottom:1px solid ${cardBorder}">Top labs · réservations</p>
                <table style="${tableStyle}">
                    <thead><tr>
                        <th style="${thStyle}">#</th>
                        <th style="${thStyle}">Lab</th>
                        <th style="${thStyle}">VM</th>
                        <th style="${thStyle};text-align:right">Nb</th>
                    </tr></thead>
                    <tbody>${topLabs || `<tr><td colspan="4" style="padding:8px;font-size:11px;color:${subColor}">Aucune donnée</td></tr>`}</tbody>
                </table>
            </div>
        </div>
        <p style="font-size:10px;color:${subColor};text-align:right">Calculé le ${escapeHtml(genAt)}</p>
    `;
}

async function refreshAdminStats(options = {}) {
    const force = Boolean(options.force);
    const silent = options.silent !== false;
    const container = document.getElementById("admin-stats-body");
    if (!container || !canAccessAdminView()) return;
    if (!silent && !latestAdminStats) {
        container.innerHTML = '<p class="text-xs text-slate-500">Chargement...</p>';
    }
    try {
        const response = await apiFetch("/api/admin/stats");
        if (!response.ok) {
            if (!silent || !latestAdminStats) {
                container.innerHTML = '<p class="text-xs text-rose-400">Impossible de charger les statistiques.</p>';
            }
            return;
        }
        const payload = await response.json();
        const summary = payload?.summary || {};
        const signature = [
            payload?.generated_at || "",
            summary.total_logins || 0,
            summary.total_reservations || 0,
            summary.distinct_users_logged_in || 0,
            summary.distinct_labs_reserved || 0,
        ].join("|");
        if (!force && signature === adminLastStatsSignature) {
            return;
        }
        latestAdminStats = payload;
        adminLastStatsSignature = signature;
        renderAdminStats(latestAdminStats);
    } catch (_) {
        if (!silent) {
            container.innerHTML = '<p class="text-xs text-rose-400">Impossible de charger les statistiques.</p>';
        }
    }
}

async function refreshAdminPanel(options = {}) {
    const force = options.force !== false;
    const silent = options.silent !== false;

    if (!canAccessAdminView()) {
        return;
    }

    // ── Affichage immédiat depuis le cache ──────────────────────────────────
    // Si des données sont déjà en mémoire, on les affiche tout de suite sans
    // attendre les réponses réseau — la vue est instantanée pour l'utilisateur.
    if (latestAdminUsers.length > 0 || latestAdminVMs.length > 0) {
        applyAdminViewScope();
        if (isAdmin()) {
            _loadVmMetricsCollapsedStates();
            renderAdminVMs(latestAdminVMs);
            renderAdminCentralResources(latestAdminCentralResources);
            void refreshAdminVmCardsResources();
            applyAdminAuditFilter();
        } else if (isGroupAdmin()) {
            renderAdminUsers(latestAdminUsers, new Map());
            _loadVmMetricsCollapsedStates();
            renderAdminVMs(latestAdminVMs);
            renderAdminCentralResources(latestAdminCentralResources);
            void refreshAdminVmCardsResources();
        }
    }

    // Throttle : si les données ont été récupérées dans la fenêtre de 30 s,
    // on ne refait pas de requête (sauf si force = true depuis une action mutante).
    if (!force && Date.now() - adminLastRefreshAt < ADMIN_AUTO_REFRESH_MS) {
        return;
    }
    if (adminRefreshInFlight) {
        return;
    }

    adminRefreshInFlight = true;
    const shouldPreserveScroll = currentView === "admin";
    const scrollX = shouldPreserveScroll ? window.scrollX : 0;
    const scrollY = shouldPreserveScroll ? window.scrollY : 0;

    applyAdminViewScope();

    try {
        if (isGroupAdmin()) {
            const hoursSelect = document.getElementById("admin-central-hours");
            const hoursValue = hoursSelect instanceof HTMLSelectElement ? Number(hoursSelect.value || 24) : 24;

            const usersPromise = apiFetch("/api/admin/users").then(r => { if (!r.ok) throw new Error(`admin/users HTTP ${r.status}`); return r.json(); });
            const vmsPromise = apiFetch("/api/admin/vms").then(r => { if (!r.ok) throw new Error(`admin/vms HTTP ${r.status}`); return r.json(); });
            const centralPromise = apiFetch(`/api/admin/central/resources?hours=${encodeURIComponent(hoursValue)}`).then(r => { if (!r.ok) throw new Error(`central/resources HTTP ${r.status}`); return r.json(); });

            const [usersPayload, vmsPayload, centralPayload] = await Promise.all([usersPromise, vmsPromise, centralPromise]);

            const nextUsers = usersPayload.items || [];
            const nextVMs = vmsPayload.items || [];
            const nextCentral = centralPayload || null;
            const nextSignature = [
                "group-admin",
                nextUsers.length,
                nextVMs.length,
                nextCentral?.generated_at || "",
                ...nextUsers.map((item) => `${item.username || ""}:${item.role || ""}:${item.active ? 1 : 0}`),
                ...nextVMs.map((item) => `${item.id || ""}:${item.name || ""}`),
            ].join("|");
            if (force || nextSignature !== adminLastDataSignature) {
                latestAdminUsers = nextUsers;
                latestAdminGroups = [];
                latestAdminVMs = nextVMs;
                latestAdminAuditItems = [];
                latestAdminCentralResources = nextCentral;
                latestAdminTopologyInventory = [];
                renderAdminUsers(latestAdminUsers, new Map());
                _loadVmMetricsCollapsedStates();
                renderAdminVMs(latestAdminVMs);
                renderAdminCentralResources(latestAdminCentralResources);
                void refreshAdminVmCardsResources();
                adminLastDataSignature = nextSignature;
            }
            await refreshAdminStats({ force, silent });
            adminLastRefreshAt = Date.now();
            return;
        }

        const hoursSelect = document.getElementById("admin-central-hours");
        const hoursValue = hoursSelect instanceof HTMLSelectElement ? Number(hoursSelect.value || 24) : 24;

        // ── Rendu progressif ────────────────────────────────────────────────
        // Toutes les requêtes partent simultanément, mais chaque section est
        // rendue dès que SA réponse arrive, sans attendre les autres.
        const usersPromise    = apiFetch("/api/admin/users").then(r => { if (!r.ok) throw new Error(`admin/users HTTP ${r.status}`); return r.json(); });
        const groupsPromise   = apiFetch("/api/admin/groups").then(r => { if (!r.ok) throw new Error(`admin/groups HTTP ${r.status}`); return r.json(); });
        const vmsPromise      = apiFetch("/api/admin/vms").then(r => { if (!r.ok) throw new Error(`admin/vms HTTP ${r.status}`); return r.json(); });
        const auditPromise    = apiFetch("/api/admin/audit?limit=200").then(r => { if (!r.ok) throw new Error(`admin/audit HTTP ${r.status}`); return r.json(); });
        const centralPromise  = apiFetch(`/api/admin/central/resources?hours=${encodeURIComponent(hoursValue)}`).then(r => { if (!r.ok) throw new Error(`central/resources HTTP ${r.status}`); return r.json(); });
        const inventoryPromise = apiFetch("/api/admin/topologies/inventory").then(r => { if (!r.ok) throw new Error(`admin/topologies/inventory HTTP ${r.status}`); return r.json(); });

        // VMs : section rapide, s'affiche dès réception
        vmsPromise.then(p => {
            latestAdminVMs = p.items || [];
            _loadVmMetricsCollapsedStates();
            renderAdminVMs(latestAdminVMs);
            void refreshAdminVmCardsResources();
        }).catch(() => {});

        // Ressources centrales : indépendant, s'affiche dès réception
        centralPromise.then(p => {
            latestAdminCentralResources = p || null;
            renderAdminCentralResources(latestAdminCentralResources);
        }).catch(() => {});

        inventoryPromise.then(p => {
            latestAdminTopologyInventory = p.items || [];
            renderAdminVMs(latestAdminVMs);
        }).catch(() => {});

        // Utilisateurs + audit : interdépendants (renderAdminUsers mêle les deux)
        Promise.all([usersPromise, auditPromise]).then(([uP, aP]) => {
            latestAdminUsers = uP.items || [];
            latestAdminAuditItems = aP.items || [];
            applyAdminAuditFilter();
            renderActivityRail();
        }).catch(() => {});

        // Attendre tout pour la vérification de signature et la mise à jour finale
        const [usersPayload, groupsPayload, vmsPayload, auditPayload, centralResourcesPayload, inventoryPayload] = await Promise.all([
            usersPromise, groupsPromise, vmsPromise, auditPromise, centralPromise, inventoryPromise,
        ]);

        const nextUsers = usersPayload.items || [];
        const nextGroups = groupsPayload.items || [];
        const nextVMs = vmsPayload.items || [];
        const nextAudit = auditPayload.items || [];
        const nextCentral = centralResourcesPayload || null;
        const nextInventory = inventoryPayload.items || [];
        const nextSignature = [
            "admin",
            nextUsers.length,
            nextGroups.length,
            nextVMs.length,
            nextAudit.length,
            nextAudit[0]?.timestamp || "",
            nextAudit[0]?.action || "",
            nextCentral?.generated_at || "",
            nextInventory.length,
        ].join("|");

        if (force || nextSignature !== adminLastDataSignature) {
            latestAdminUsers = nextUsers;
            latestAdminGroups = nextGroups;
            latestAdminVMs = nextVMs;
            latestAdminAuditItems = nextAudit;
            latestAdminCentralResources = nextCentral;
            latestAdminTopologyInventory = nextInventory;
            _loadVmMetricsCollapsedStates();
            renderAdminVMs(latestAdminVMs);
            renderAdminCentralResources(latestAdminCentralResources);
            applyAdminAuditFilter();
            void refreshAdminVmCardsResources();
            adminLastDataSignature = nextSignature;
            renderActivityRail();
        }

        await refreshVmResourcesOnly();
        void refreshActivityAuditHistory(false);

        await refreshAdminStats({ force, silent });
        adminLastRefreshAt = Date.now();
    } catch (error) {
        console.error("Failed to refresh admin panel", error);
    } finally {
        adminRefreshInFlight = false;
        if (shouldPreserveScroll) {
            window.requestAnimationFrame(() => {
                window.scrollTo(scrollX, scrollY);
            });
        }
    }
}

async function refreshCentralResourcesOnly() {
    const hoursSelect = document.getElementById("admin-central-hours");
    const hoursValue = hoursSelect instanceof HTMLSelectElement ? Number(hoursSelect.value || 24) : 24;

    // Auto-expand the panel so the user sees the updated graph
    if (adminCentralCollapsed) {
        adminCentralCollapsed = false;
        updateAdminCentralPanelVisibility();
    }

    const container = document.getElementById("admin-central-resources");
    if (container) {
        container.innerHTML = `<p style="font-size:12px;color:#94a3b8">Chargement...</p>`;
    }

    try {
        const response = await apiFetch(`/api/admin/central/resources?hours=${encodeURIComponent(hoursValue)}`);
        if (!response.ok) {
            if (container) container.innerHTML = `<p style="font-size:12px;color:#f43f5e">Erreur HTTP ${response.status}</p>`;
            return;
        }
        const payload = await response.json();
        latestAdminCentralResources = payload || null;
        renderAdminCentralResources(latestAdminCentralResources);
    } catch (err) {
        if (container) container.innerHTML = `<p style="font-size:12px;color:#f43f5e">Erreur: ${escapeHtml(String(err?.message || "inconnue"))}</p>`;
    }
}

async function refreshVmResourcesOnly() {
    const vmSelect = document.getElementById("admin-vm-metrics-id");
    const hoursSelect = document.getElementById("admin-central-hours");
    const container = document.getElementById("admin-vm-resources");

    const vmId = vmSelect instanceof HTMLSelectElement ? String(vmSelect.value || "").trim() : "";
    const hoursValue = hoursSelect instanceof HTMLSelectElement ? Number(hoursSelect.value || 24) : 24;

    if (!vmId) {
        if (container) {
            container.innerHTML = '<p class="text-xs text-slate-500">Sélectionne une VM pour afficher son historique de ressources.</p>';
        }
        latestAdminVmResources = null;
        return;
    }

    if (container) {
        container.innerHTML = `<p style="font-size:12px;color:#94a3b8">Chargement des métriques VM...</p>`;
    }

    try {
        const useAggregated = hoursValue > 24;
        const endpoint = useAggregated
            ? `/api/admin/vms/${encodeURIComponent(vmId)}/resources/aggregated?hours=${encodeURIComponent(hoursValue)}&bucket=auto`
            : `/api/admin/vms/${encodeURIComponent(vmId)}/resources?hours=${encodeURIComponent(hoursValue)}`;
        const response = await apiFetch(endpoint);
        if (!response.ok) {
            if (container) container.innerHTML = `<p style="font-size:12px;color:#f43f5e">Erreur HTTP ${response.status}</p>`;
            return;
        }
        const payload = await response.json();
        latestAdminVmResources = payload || null;
        renderAdminVmResources(latestAdminVmResources);
    } catch (err) {
        if (container) container.innerHTML = `<p style="font-size:12px;color:#f43f5e">Erreur: ${escapeHtml(String(err?.message || "inconnue"))}</p>`;
    }
}
