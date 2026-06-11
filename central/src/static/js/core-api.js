function _extractApiErrorDetail(detail) {
    if (!detail) return "Erreur inconnue";
    if (typeof detail === "string") return detail;
    if (typeof detail === "object") {
        // Unwrap double-wrapped {detail: {...}} — peut arriver en cas de relay HTTPException
        if (detail.detail && !detail.error) {
            return _extractApiErrorDetail(detail.detail);
        }
        if (typeof detail.message === "string" && detail.message) {
            const retryAfterSeconds = Number(detail.retry_after_seconds || 0);
            if (retryAfterSeconds > 0) {
                const retryAfterMinutes = Math.ceil(retryAfterSeconds / 60);
                return `${detail.message}. Réessayez dans ${retryAfterMinutes} min.`;
            }
            return detail.message;
        }
        if (detail.capacity_check) {
            const check = detail.capacity_check;
            const req = check?.required || {};
            const res = check?.resources || {};
            const suggestion = (check?.recommendation?.suggested_by_kind || [])
                .map((entry) => `${entry.kind}: ${entry.suggested_max}/${entry.current}`)
                .join(", ");
            return [
                detail.error || "Capacité insuffisante",
                `Requis: ${req.memory_mb || 0} MB RAM, ${req.cpu_units || 0} CPU-units`,
                `Budget (marge 20%): ${res.memory_budget_mb || 0} MB RAM, ${res.cpu_units_budget || 0} CPU-units`,
                suggestion ? `Suggestion (max): ${suggestion}` : "",
            ].filter(Boolean).join(" | ");
        }
        // Montrer les erreurs de validation spécifiques en priorité
        if (detail.validation && Array.isArray(detail.validation.errors) && detail.validation.errors.length) {
            return `Validation: ${detail.validation.errors.join(" | ")}`;
        }
        if (typeof detail.error === "string" && detail.error) return detail.error;
        try {
            return JSON.stringify(detail);
        } catch (_) {
            return "Erreur de traitement";
        }
    }
    return String(detail);
}

async function apiFetch(url, options = {}) {
    const response = await fetch(url, {
        ...options,
        headers: {
            ...(options.headers || {}),
        },
    });

    if (response.status === 401) {
        setSessionUser(null);
        throw new Error("Authentification requise");
    }

    if (response.status === 403) {
        throw new Error("Accès refusé (403).");
    }

    return response;
}

async function apiFetchJson(url, options = {}) {
    const response = await apiFetch(url, options);
    if (!response.ok) {
        const text = await response.text();
        let detail = text || `HTTP ${response.status}`;
        try { detail = _extractApiErrorDetail(JSON.parse(text)) || detail; } catch (_) {}
        throw new Error(detail);
    }
    return response.json();
}
