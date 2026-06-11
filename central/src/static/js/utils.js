function percentage(value) {
    if (value === null || value === undefined) {
        return "-";
    }
    return `${Number(value).toFixed(1)}%`;
}

function formatMoGo(bytes) {
    if (bytes === null || bytes === undefined || Number.isNaN(Number(bytes))) {
        return "-";
    }

    const megaBytes = Number(bytes) / (1024 * 1024);
    if (megaBytes >= 1024) {
        return `${(megaBytes / 1024).toFixed(1)} Go`;
    }
    return `${megaBytes.toFixed(0)} Mo`;
}

function progressBar(percent) {
    const safe = Number.isFinite(Number(percent)) ? Math.max(0, Math.min(100, Number(percent))) : 0;
    const isLight = document.body.classList.contains("theme-light");
    const trackBg = isLight ? "#e2e8f0" : "#1e293b";
    const fillBg = safe >= 85 ? "#f43f5e" : safe >= 65 ? "#f59e0b" : "#10b981";
    return `
        <div style="width:100%;height:6px;border-radius:4px;background:${trackBg};overflow:hidden;margin-top:6px">
            <div style="height:6px;width:${safe}%;background:${fillBg};transition:width 0.3s"></div>
        </div>
    `;
}

function resourceBlock(label, percent, usedBytes, totalBytes) {
    const safe = Number.isFinite(Number(percent)) ? Math.max(0, Math.min(100, Number(percent))) : 0;
    const isLight = document.body.classList.contains("theme-light");
    const cardBg = isLight ? "#f8fafc" : "rgba(15,23,42,0.8)";
    const cardBorder = isLight ? "#cbd5e1" : "#1e293b";
    const labelColor = isLight ? "#475569" : "#94a3b8";
    const subColor = isLight ? "#64748b" : "#64748b";
    const valueColor = safe >= 85 ? "#f43f5e" : safe >= 65 ? "#f59e0b" : "#10b981";
    // Fixed per-metric accent color — matches the graph line colors
    const accentColor = label === "CPU" ? "#22d3ee" : label === "RAM" ? "#f59e0b" : "#10b981";
    return `
        <div style="background:${cardBg};border:1px solid ${cardBorder};border-left:3px solid ${accentColor};border-radius:0.5rem;padding:10px 12px">
            <div style="display:flex;align-items:center;justify-content:space-between">
                <p style="font-size:11px;color:${labelColor};text-transform:uppercase;letter-spacing:0.05em">${label}</p>
                <p style="font-size:14px;font-weight:600;color:${valueColor}">${percentage(percent)}</p>
            </div>
            ${progressBar(percent)}
            <p style="font-size:11px;color:${subColor};margin-top:4px">${formatMoGo(usedBytes)} / ${formatMoGo(totalBytes)}</p>
        </div>
    `;
}

// ── UI helpers : preserve <details> open/closed state across re-renders ──
function captureDetailsState(container) {
    if (!container) return {};
    const state = {};
    for (const el of container.querySelectorAll("details[data-key]")) {
        state[el.dataset.key] = el.open;
    }
    return state;
}

function restoreDetailsState(container, state) {
    if (!container) return;
    for (const el of container.querySelectorAll("details[data-key]")) {
        const key = el.dataset.key;
        if (Object.prototype.hasOwnProperty.call(state, key)) {
            el.open = state[key];
        }
    }
}

function sessionMetaLabel(user) {
    if (!user) {
        return "";
    }
    const roleLabel = user.role === "admin"
        ? "Admin"
        : user.role === "group-admin"
            ? "Group Admin"
            : "Utilisateur";
    return `${user.email} · ${roleLabel}`;
}

async function downloadFile(url, fallbackFilename) {
    const response = await apiFetch(url);
    if (!response.ok) {
        const payloadText = await response.text();
        let detail = payloadText || `HTTP ${response.status}`;
        try {
            detail = JSON.parse(payloadText)?.detail || detail;
        } catch (_) {
        }
        throw new Error(detail);
    }

    const blob = await response.blob();
    const disposition = response.headers.get("content-disposition") || "";
    const match = disposition.match(/filename="?([^\"]+)"?/i);
    const filename = match?.[1] || fallbackFilename;
    const objectUrl = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = objectUrl;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(objectUrl);
}

function maskToken(value) {
    const token = String(value || "");
    if (!token) {
        return "(vide)";
    }
    if (token.length <= 6) {
        return `${token.slice(0, 1)}***`;
    }
    return `${token.slice(0, 3)}***${token.slice(-2)}`;
}

function buildSeriesPath(points, width, height) {
    if (!points.length) {
        return "";
    }
    return points.map((point, index) => {
        const x = (index / Math.max(1, points.length - 1)) * width;
        const y = height - ((Math.max(0, Math.min(100, Number(point))) / 100) * height);
        return `${index === 0 ? "M" : "L"}${x.toFixed(2)},${y.toFixed(2)}`;
    }).join(" ");
}

function formatCompletionDate(isoDate) {
    if (!isoDate) {
        return "";
    }
    const date = new Date(isoDate);
    if (Number.isNaN(date.getTime())) {
        return "";
    }
    return date.toLocaleString("fr-FR");
}

function escapeHtml(value) {
    return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");
}

function escapeJsonAttr(jsonStr) {
    return escapeHtml(jsonStr).replace(/"/g, "&quot;");
}

function labRequestStatusLabel(status) {
    const normalized = String(status || "").trim().toLowerCase();
    if (normalized === "pris_en_compte") {
        return "Pris en compte";
    }
    if (normalized === "en_cours_etude") {
        return "En cours d'etude";
    }
    if (normalized === "reponse") {
        return "Reponse";
    }
    return "Inconnu";
}

function labRequestStatusTone(status) {
    const normalized = String(status || "").trim().toLowerCase();
    if (normalized === "reponse") {
        return "border-emerald-700/60 bg-emerald-900/20 text-emerald-200";
    }
    if (normalized === "en_cours_etude") {
        return "border-amber-700/60 bg-amber-900/20 text-amber-200";
    }
    return "border-cyan-700/60 bg-cyan-900/20 text-cyan-200";
}

function formatRemainingReservation(expiresAt) {
    if (!expiresAt) {
        return "";
    }

    const end = new Date(expiresAt);
    const remainingMs = end.getTime() - Date.now();
    if (Number.isNaN(end.getTime()) || remainingMs <= 0) {
        return "Expirée";
    }

    const totalSeconds = Math.floor(remainingMs / 1000);
    const hours = Math.floor(totalSeconds / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    const seconds = totalSeconds % 60;

    if (hours > 0) {
        return `${hours}h ${String(minutes).padStart(2, "0")}m ${String(seconds).padStart(2, "0")}s`;
    }
    return `${minutes}m ${String(seconds).padStart(2, "0")}s`;
}

function updateReservationCountdowns() {
    document.querySelectorAll("[data-reservation-expiry]").forEach((node) => {
        const expiresAt = node.getAttribute("data-reservation-expiry") || "";
        node.textContent = formatRemainingReservation(expiresAt);
    });
}

function formatDurationHours(value) {
    const totalMinutes = Math.round(Number(value || 0) * 60);
    const hours = Math.floor(totalMinutes / 60);
    const minutes = totalMinutes % 60;
    if (hours > 0 && minutes > 0) {
        return `${hours}h${String(minutes).padStart(2, "0")}`;
    }
    if (hours > 0) {
        return `${hours}h`;
    }
    return `${minutes}m`;
}

function statusBadge(stateValue) {
    const rawState = (stateValue || "unknown").toLowerCase();
    if (rawState.includes("running") || rawState.includes("up")) {
        return { icon: "●", className: "text-emerald-400", label: "RUN" };
    }
    if (rawState.includes("starting") || rawState.includes("restarting") || rawState.includes("pending")) {
        return { icon: "●", className: "text-amber-400", label: "INIT" };
    }
    return { icon: "●", className: "text-rose-400", label: "DOWN" };
}

function vmEndpointLabel(vm) {
    const ip = vm.ip || "";
    const port = vm.port || "";
    if (ip && port) {
        return `${ip}:${port}`;
    }
    return vm.base_url || "";
}
