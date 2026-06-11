// ── Gestion des colonnes interactives ────────────────────────────────────────
const TABLE_COLUMN_CONFIG_KEY = "vlm:table-columns";
const DEFAULT_TABLE_COLUMNS = {
    order: ["hostname", "ip", "model", "state_uptime", "ssh"],
    widths: {
        hostname: 250,
        ip: 180,
        model: 150,
        state_uptime: 200,
        ssh: 80
    },
    sortBy: "hostname",
    sortDir: "asc"
};

// Stockage des données des routeurs par tableId
const routerTableDataMap = new Map();

function _getTableColumnConfig() {
    try {
        const saved = localStorage.getItem(TABLE_COLUMN_CONFIG_KEY);
        if (saved) {
            const config = JSON.parse(saved);
            // Merge with defaults and ensure all default columns are included
            const merged = { ...DEFAULT_TABLE_COLUMNS, ...config };
            // Add any new columns that are in defaults but not in saved config
            if (config.order) {
                const newColumns = DEFAULT_TABLE_COLUMNS.order.filter(col => !config.order.includes(col));
                if (newColumns.length > 0) {
                    merged.order = [...config.order, ...newColumns];
                    merged.widths = { ...DEFAULT_TABLE_COLUMNS.widths, ...(config.widths || {}) };
                }
            }
            return merged;
        }
    } catch (_) {}
    return { ...DEFAULT_TABLE_COLUMNS };
}

function _saveTableColumnConfig(config) {
    try {
        localStorage.setItem(TABLE_COLUMN_CONFIG_KEY, JSON.stringify(config));
    } catch (_) {}
}

function _generateTableHeaderHtml(tableId, columns) {
    const config = _getTableColumnConfig();
    const colOrder = config.order || DEFAULT_TABLE_COLUMNS.order;
    const widths = config.widths || DEFAULT_TABLE_COLUMNS.widths;
    const sortBy = config.sortBy || "hostname";
    const sortDir = config.sortDir || "asc";

    const columnLabels = {
        hostname: "HOSTNAME",
        ip: "IP",
        model: "MODÈLE",
        state_uptime: "ÉTAT / UPTIME",
        ssh: "SSH"
    };

    let html = `<div class="table-header-row" data-table-id="${tableId}">`;
    
    for (const colId of colOrder) {
        const label = columnLabels[colId] || colId;
        const width = widths[colId] || 150;
        const sortIcon = sortBy === colId ? (sortDir === "asc" ? " ▲" : " ▼") : "";
        
        html += `
            <div class="table-header-cell" 
                 data-column-id="${colId}"
                 data-table-id="${tableId}"
                 style="width:${width}px">
                <span>${label}${sortIcon}</span>
                <div class="resize-handle"></div>
            </div>
        `;
    }
    
    html += `</div>`;
    return html;
}

function _generateRouterTableHtml(tableId, routersData) {
    const config = _getTableColumnConfig();
    const colOrder = config.order || DEFAULT_TABLE_COLUMNS.order;
    const widths = config.widths || DEFAULT_TABLE_COLUMNS.widths;
    let sortBy = config.sortBy || "hostname";
    let sortDir = config.sortDir || "asc";

    // Tri des routeurs selon la configuration
    let sorted = [...routersData];
    sorted.sort((a, b) => {
        let aVal = "", bVal = "";
        
        if (sortBy === "hostname") {
            aVal = (a.name || "").toLowerCase();
            bVal = (b.name || "").toLowerCase();
        } else if (sortBy === "ip") {
            aVal = a.mgmt_ipv4 || "";
            bVal = b.mgmt_ipv4 || "";
        } else if (sortBy === "model") {
            aVal = (a.kind || "").toLowerCase();
            bVal = (b.kind || "").toLowerCase();
        } else if (sortBy === "state_uptime") {
            aVal = a.state || "";
            bVal = b.state || "";
        }

        if (sortDir === "asc") {
            return aVal.localeCompare(bVal);
        } else {
            return bVal.localeCompare(aVal);
        }
    });

    let html = `<div class="router-table" data-table-id="${tableId}" data-router-scroll="${tableId}">`;

    if (sorted.length === 0) {
        html += `<div class="px-2 py-3 text-xs text-slate-500">Aucun routeur détecté</div>`;
    } else {
        for (const router of sorted) {
            const routerName = router.name || "router";
            const mgmtIp = router.mgmt_ipv4 || "N/A";
            const routerKind = router.kind || "inconnu";
            const status = statusBadge(router.state);
            const uptime = router.uptime || "-";

            html += `<div class="router-row">`;

            for (const colId of colOrder) {
                const width = widths[colId] || 150;
                let cellContent = "";

                if (colId === "hostname") {
                    cellContent = `<div title="${escapeHtml(routerName)}">${escapeHtml(routerName)}</div>`;
                } else if (colId === "ip") {
                    cellContent = `<div class="font-mono" title="${mgmtIp}">${mgmtIp}</div>`;
                } else if (colId === "model") {
                    cellContent = `<div title="${escapeHtml(routerKind)}">${escapeHtml(routerKind)}</div>`;
                } else if (colId === "state_uptime") {
                    cellContent = `
                        <div class="flex items-center justify-between gap-2 w-full">
                            <span class="${status.className}"><span class="font-semibold">${status.icon}</span> ${status.label}</span>
                            <span class="text-slate-300 whitespace-nowrap" title="${uptime}">${uptime}</span>
                        </div>
                    `;
                } else if (colId === "ssh") {
                    const sshCommand = _generateSshCommand(mgmtIp, routerKind);
                    cellContent = `<button type="button" class="ssh-copy-btn text-xs px-2 py-1 rounded border border-cyan-700 text-cyan-300 hover:bg-cyan-900/30" data-ssh-command="${escapeHtml(sshCommand)}" title="Copier la commande SSH">SSH</button>`;
                }

                html += `<div class="router-cell" style="width:${width}px">${cellContent}</div>`;
            }

            html += `</div>`;
        }
    }

    html += `</div>`;
    return html;
}

function _getNodeSshUsername(nodeKind) {
    if (!nodeKind) return "clab";
    const kindLower = String(nodeKind).toLowerCase();
    // cisco-xrd uses 'clab' user
    if (kindLower.includes("cisco-xrd") || kindLower.includes("xrd")) return "clab";
    // cisco-iol uses 'admin' user
    if (kindLower.includes("cisco-iol") || kindLower.includes("iol")) return "admin";
    // juniper uses 'admin' user
    if (kindLower.includes("juniper") || kindLower.includes("vmx") || kindLower.includes("vqfx")) return "admin";
    // Default fallback
    return "clab";
}

function _normalizeNodeIp(nodeIp) {
    const raw = String(nodeIp || "").trim();
    if (!raw || raw === "N/A") return "";
    return raw.split("/")[0].trim();
}

function _generateSshCommand(nodeIp, nodeKind) {
    const cleanIp = _normalizeNodeIp(nodeIp);
    if (!cleanIp) {
        return "";
    }
    const username = currentUser?.username || "user";
    const nodeUsername = _getNodeSshUsername(nodeKind);
    return `ssh -J ${username}@<jump-host-ip> ${nodeUsername}@${cleanIp}`;
}

function _copyTextFallback(text) {
    try {
        const textarea = document.createElement("textarea");
        textarea.value = text;
        textarea.setAttribute("readonly", "");
        textarea.style.position = "fixed";
        textarea.style.top = "-9999px";
        textarea.style.left = "-9999px";
        document.body.appendChild(textarea);
        textarea.focus();
        textarea.select();
        const ok = document.execCommand("copy");
        document.body.removeChild(textarea);
        return ok;
    } catch (_) {
        return false;
    }
}

function _copyTextToClipboard(text) {
    if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
        return navigator.clipboard.writeText(text).then(() => true).catch(() => _copyTextFallback(text));
    }
    return Promise.resolve(_copyTextFallback(text));
}

function _setupTableColumnInteractions(tableId) {
    const wrapper = document.querySelector(`.table-wrapper[data-table-id="${tableId}"]`);
    if (!wrapper) return;

    // Setup SSH button click handlers
    wrapper.querySelectorAll(".ssh-copy-btn").forEach(btn => {
        btn.addEventListener("click", (e) => {
            e.preventDefault();
            const sshCommand = btn.dataset.sshCommand;
            if (!sshCommand) return;

            _copyTextToClipboard(sshCommand).then((copied) => {
                if (!copied) {
                    alert("Impossible de copier automatiquement. Commande SSH:\n" + sshCommand);
                    return;
                }
                const originalText = btn.textContent;
                btn.textContent = "✓ Copié";
                btn.classList.add("bg-emerald-900/30", "border-emerald-700", "text-emerald-300");
                btn.classList.remove("border-cyan-700", "text-cyan-300", "hover:bg-cyan-900/30");
                setTimeout(() => {
                    btn.textContent = originalText;
                    btn.classList.remove("bg-emerald-900/30", "border-emerald-700", "text-emerald-300");
                    btn.classList.add("border-cyan-700", "text-cyan-300", "hover:bg-cyan-900/30");
                }, 2000);
            });
        });
    });

    const config = _getTableColumnConfig();
    let resizingColumn = null;
    let resizeStartX = 0;
    let resizeStartWidth = 0;

    // Gestion du redimensionnement des colonnes
    function setupResizeHandles() {
        const handles = wrapper.querySelectorAll(".resize-handle");
        for (const handle of handles) {
            // Retirer les anciens listeners en clonant
            const newHandle = handle.cloneNode(true);
            handle.parentElement.replaceChild(newHandle, handle);
        }

        const newHandles = wrapper.querySelectorAll(".resize-handle");
        for (const handle of newHandles) {
            handle.addEventListener("mousedown", onResizeStart, { once: false });
        }
    }

    function onResizeStart(e) {
        e.preventDefault();
        e.stopPropagation();
        const cell = e.target.closest(".table-header-cell");
        if (!cell) return;

        const colId = cell.dataset.columnId;
        resizingColumn = colId;
        resizeStartX = e.clientX;
        resizeStartWidth = parseInt(cell.style.width || cell.offsetWidth);

        document.addEventListener("mousemove", onResizeMove);
        document.addEventListener("mouseup", onResizeEnd);
    }

    function onResizeMove(e) {
        if (!resizingColumn) return;
        const delta = e.clientX - resizeStartX;
        const newWidth = Math.max(80, resizeStartWidth + delta);

        config.widths[resizingColumn] = newWidth;
        _saveTableColumnConfig(config);

        // Mettre à jour la largeur de la cellule d'en-tête
        const headerCell = wrapper.querySelector(`[data-column-id="${resizingColumn}"]`);
        if (headerCell) {
            headerCell.style.width = `${newWidth}px`;
        }

        // Mettre à jour les largeurs de tous les cells de cette colonne
        const colIndex = config.order.indexOf(resizingColumn);
        if (colIndex !== -1) {
            wrapper.querySelectorAll(".router-row").forEach(row => {
                const cells = row.querySelectorAll(".router-cell");
                if (cells[colIndex]) {
                    cells[colIndex].style.width = `${newWidth}px`;
                }
            });
        }
    }

    function onResizeEnd() {
        document.removeEventListener("mousemove", onResizeMove);
        document.removeEventListener("mouseup", onResizeEnd);
        resizingColumn = null;
    }

    // Gestion du tri au clic sur les en-têtes
    function setupSortHandlers() {
        const headerCells = wrapper.querySelectorAll(".table-header-cell");
        for (const cell of headerCells) {
            cell.removeEventListener("click", onHeaderClick);
            cell.addEventListener("click", onHeaderClick);
        }
    }

    function onHeaderClick(e) {
        if (e.target.closest(".resize-handle")) return;

        const cell = e.currentTarget;
        const colId = cell.dataset.columnId;

        if (config.sortBy === colId) {
            config.sortDir = config.sortDir === "asc" ? "desc" : "asc";
        } else {
            config.sortBy = colId;
            config.sortDir = "asc";
        }

        _saveTableColumnConfig(config);

        // Redessiner la table avec le nouveau tri
        const routerTable = wrapper.querySelector(".router-table");
        const routersData = routerTableDataMap.get(tableId);

        if (routerTable && routersData && routersData.length > 0) {
            const sortedHtml = _generateRouterTableHtml(tableId, routersData);
            const temp = document.createElement("div");
            temp.innerHTML = sortedHtml;
            const newTable = temp.querySelector(".router-table");
            routerTable.replaceWith(newTable);
        }

        // Redessiner les en-têtes
        const header = wrapper.querySelector(".table-header-row");
        if (header) {
            const newHeaderHtml = _generateTableHeaderHtml(tableId, config.order);
            const tempHeader = document.createElement("div");
            tempHeader.innerHTML = newHeaderHtml;
            header.replaceWith(tempHeader.firstElementChild);
        }

        // Réinitialiser les interactions
        setupResizeHandles();
        setupSortHandlers();
    }

    setupResizeHandles();
    setupSortHandlers();
}
