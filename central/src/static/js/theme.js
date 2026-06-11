function _themeStorageKeyFor(username) {
    const normalized = String(username || "").trim().toLowerCase();
    if (!normalized) {
        return "vlm:theme:guest";
    }
    return `vlm:theme:user:${normalized}`;
}

function _normalizeTheme(value) {
    return String(value || "").toLowerCase() === "light" ? "light" : "dark";
}

function _loadThemePreference(username) {
    try {
        const userKey = _themeStorageKeyFor(username);
        const saved = localStorage.getItem(userKey);
        if (saved) {
            return _normalizeTheme(saved);
        }
        const fallback = localStorage.getItem("vlm:theme:last");
        if (fallback) {
            return _normalizeTheme(fallback);
        }
    } catch (_) {
    }
    return "dark";
}

function _saveThemePreference(theme, username) {
    const normalized = _normalizeTheme(theme);
    try {
        localStorage.setItem("vlm:theme:last", normalized);
        localStorage.setItem(_themeStorageKeyFor(username), normalized);
    } catch (_) {
    }
}

function _updateThemeToggleLabel() {
    const toggleButton = document.getElementById("theme-toggle");
    if (!(toggleButton instanceof HTMLButtonElement)) {
        return;
    }

    const sunIcon = `
        <svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <circle cx="12" cy="12" r="5"></circle>
            <path d="M12 1v2"></path>
            <path d="M12 21v2"></path>
            <path d="M4.22 4.22l1.42 1.42"></path>
            <path d="M18.36 18.36l1.42 1.42"></path>
            <path d="M1 12h2"></path>
            <path d="M21 12h2"></path>
            <path d="M4.22 19.78l1.42-1.42"></path>
            <path d="M18.36 5.64l1.42-1.42"></path>
        </svg>
    `;
    const moonIcon = `
        <svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M21 12.79A9 9 0 1 1 11.21 3c.12 0 .24 0 .36.01A7 7 0 0 0 21 12.79z"></path>
        </svg>
    `;

    if (currentTheme === "light") {
        toggleButton.innerHTML = `${moonIcon}<span class="sr-only">Basculer le thème</span>`;
        toggleButton.setAttribute("aria-label", "Activer le mode sombre");
        toggleButton.setAttribute("title", "Activer le mode sombre");
    } else {
        toggleButton.innerHTML = `${sunIcon}<span class="sr-only">Basculer le thème</span>`;
        toggleButton.setAttribute("aria-label", "Activer le mode clair");
        toggleButton.setAttribute("title", "Activer le mode clair");
    }
}

function applyTheme(theme, options = {}) {
    const { persist = true } = options;
    currentTheme = _normalizeTheme(theme);
    document.body.classList.toggle("theme-light", currentTheme === "light");
    document.body.classList.toggle("theme-dark", currentTheme !== "light");
    _updateThemeToggleLabel();
    if (persist) {
        _saveThemePreference(currentTheme, currentUser?.username || null);
    }
    // Re-render theme-aware components
    if (latestAdminCentralResources) {
        renderAdminCentralResources(latestAdminCentralResources);
    }
    if (latestAdminVmResources) {
        renderAdminVmResources(latestAdminVmResources);
    }
    if (latestStateItems && latestStateItems.length) {
        renderState(latestStateItems);
    }
    if (latestAdminStats) {
        renderAdminStats(latestAdminStats);
    }
}

function toggleTheme() {
    const nextTheme = currentTheme === "light" ? "dark" : "light";
    applyTheme(nextTheme, { persist: true });
}
