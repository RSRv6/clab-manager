async function submitAdminUserUpdate(username, payload) {
    const response = await apiFetch(`/api/admin/users/${encodeURIComponent(username)}`, {
        method: "PATCH",
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
    return response.json();
}

function showEditUserModal(user) {
    const isSelf = isCurrentUser(user.username);
    const currentGroupName = Array.isArray(user.groups) && user.groups.length
        ? String(user.groups[0] || "").trim()
        : "";
    const availableGroups = Array.from(new Set([
        ...latestAdminGroups.map((group) => String(group?.name || "").trim()).filter(Boolean),
        currentGroupName,
    ].filter(Boolean)));
    const groupOptions = [
        `<option value="" ${!currentGroupName ? "selected" : ""}>Aucun groupe (accès LAB = aucun)</option>`,
        ...availableGroups.map((groupName) => `<option value="${escapeHtml(groupName)}" ${groupName === currentGroupName ? "selected" : ""}>${escapeHtml(groupName)}</option>`),
    ].join("");
    const overlay = showOverlayModal({
        title: `Éditer ${user.username}`,
        tone: "slate",
        widthClass: "max-w-2xl",
        bodyHtml: `
            <form id="admin-edit-user-form" class="grid gap-4 md:grid-cols-2">
                <div>
                    <label class="block text-xs uppercase tracking-wide text-slate-400 mb-1">Nom complet</label>
                    <input name="full_name" value="${escapeHtml(user.full_name || "")}" required class="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-cyan-500" />
                </div>
                <div>
                    <label class="block text-xs uppercase tracking-wide text-slate-400 mb-1">Email</label>
                    <input name="email" type="email" value="${escapeHtml(user.email || "")}" required class="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-cyan-500" />
                </div>
                <div>
                    <label class="block text-xs uppercase tracking-wide text-slate-400 mb-1">Rôle</label>
                    <select name="role" ${isSelf ? "disabled" : ""} class="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-cyan-500 disabled:cursor-not-allowed disabled:opacity-50">
                        <option value="user" ${user.role === "user" ? "selected" : ""}>Utilisateur</option>
                        <option value="group-admin" ${user.role === "group-admin" ? "selected" : ""}>Group Admin</option>
                        <option value="admin" ${user.role === "admin" ? "selected" : ""}>Admin</option>
                    </select>
                    ${isSelf ? '<p class="mt-1 text-[11px] text-slate-500">Votre propre rôle admin reste verrouillé dans l’interface.</p>' : ""}
                </div>
                <div>
                    <label class="block text-xs uppercase tracking-wide text-slate-400 mb-1">Groupe</label>
                    <select name="group_name" class="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-cyan-500">
                        ${groupOptions}
                    </select>
                </div>
                <div class="flex items-center gap-2 mt-6">
                    <input id="edit-user-active" name="active" type="checkbox" ${user.active ? "checked" : ""} ${isSelf ? "disabled" : ""} class="rounded border-slate-600 bg-slate-950 text-cyan-500 focus:ring-cyan-500 disabled:cursor-not-allowed disabled:opacity-50" />
                    <label for="edit-user-active" class="text-sm text-slate-300">Compte actif</label>
                </div>
                ${isSelf ? '<p class="text-[11px] text-slate-500 md:col-span-2">Vous pouvez modifier votre nom et votre email, mais pas désactiver votre compte depuis cette fenêtre.</p>' : ""}
                <p id="admin-edit-user-error" class="hidden text-sm text-rose-300 md:col-span-2"></p>
                <div class="md:col-span-2 flex items-center justify-end gap-2">
                    <button type="button" data-modal-close="true" class="text-xs px-3 py-2 rounded border border-slate-600 hover:bg-slate-800">Annuler</button>
                    <button type="submit" id="admin-edit-user-submit" class="text-xs px-3 py-2 rounded border border-cyan-700 text-cyan-200 hover:bg-cyan-900/30">Enregistrer</button>
                </div>
            </form>
        `,
    });

    const form = overlay?.querySelector("#admin-edit-user-form");
    const errorNode = overlay?.querySelector("#admin-edit-user-error");
    const submitButton = overlay?.querySelector("#admin-edit-user-submit");
    if (!(form instanceof HTMLFormElement) || !(errorNode instanceof HTMLElement) || !(submitButton instanceof HTMLButtonElement)) {
        return;
    }

    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        submitButton.disabled = true;
        errorNode.classList.add("hidden");
        const formData = new FormData(form);

        try {
            await submitAdminUserUpdate(user.username, {
                full_name: String(formData.get("full_name") || "").trim(),
                email: String(formData.get("email") || "").trim(),
                role: String(formData.get("role") || user.role || "user"),
                active: formData.has("active") ? formData.get("active") === "on" : Boolean(user.active),
                group_name: String(formData.get("group_name") || "").trim() || null,
            });
            closeOverlayModal();
            await refreshAdminPanel();
            showToastMessage("Utilisateur mis à jour", `${user.username} a été modifié.`);
        } catch (error) {
            errorNode.textContent = error?.message || "Mise à jour impossible.";
            errorNode.classList.remove("hidden");
        } finally {
            submitButton.disabled = false;
        }
    });
}

function showResetPasswordModal(username) {
    const overlay = showOverlayModal({
        title: `Réinitialiser le mot de passe de ${username}`,
        tone: "amber",
        widthClass: "max-w-xl",
        bodyHtml: `
            <div class="flex gap-2 mb-4">
                <button type="button" id="reset-tab-manual" class="text-xs px-3 py-1.5 rounded border border-amber-600 bg-amber-950/50 text-amber-200">Définir manuellement</button>
                <button type="button" id="reset-tab-link" class="text-xs px-3 py-1.5 rounded border border-slate-600 text-slate-400 hover:bg-slate-800">Générer un lien</button>
            </div>

            <div id="reset-panel-manual">
                <form id="admin-reset-password-form" class="space-y-4">
                    <div>
                        <label class="block text-xs uppercase tracking-wide text-slate-400 mb-1">Nouveau mot de passe</label>
                        <input name="new_password" type="password" required minlength="10" class="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-amber-500" />
                    </div>
                    <p id="admin-reset-password-error" class="hidden text-sm text-rose-300"></p>
                    <div class="flex items-center justify-end gap-2">
                        <button type="button" data-modal-close="true" class="text-xs px-3 py-2 rounded border border-slate-600 hover:bg-slate-800">Annuler</button>
                        <button type="submit" id="admin-reset-password-submit" class="text-xs px-3 py-2 rounded border border-amber-700 text-amber-200 hover:bg-amber-900/30">Réinitialiser</button>
                    </div>
                </form>
            </div>

            <div id="reset-panel-link" class="hidden space-y-3">
                <p class="text-xs text-slate-400">Génère un lien à usage unique valable <strong class="text-slate-200">2 heures</strong>. Copiez-le et envoyez-le à l'utilisateur (Teams, mail, etc.).<br>Tout lien précédent pour cet utilisateur est révoqué.</p>
                <p id="reset-link-error" class="hidden text-sm text-rose-300"></p>
                <div id="reset-link-result" class="hidden">
                    <label class="block text-xs uppercase tracking-wide text-slate-400 mb-1">Lien de réinitialisation</label>
                    <div class="flex gap-2">
                        <input id="reset-link-url" type="text" readonly class="flex-1 rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-xs text-slate-100 font-mono" />
                        <button type="button" id="reset-link-copy" class="text-xs px-3 py-2 rounded border border-cyan-700 text-cyan-200 hover:bg-cyan-900/30 shrink-0">Copier</button>
                    </div>
                    <p class="text-[11px] text-slate-500 mt-1">Le lien expire dans 2 heures et ne peut être utilisé qu'une seule fois.</p>
                </div>
                <div class="flex items-center justify-end gap-2">
                    <button type="button" data-modal-close="true" class="text-xs px-3 py-2 rounded border border-slate-600 hover:bg-slate-800">Fermer</button>
                    <button type="button" id="reset-link-generate" class="text-xs px-3 py-2 rounded border border-amber-700 text-amber-200 hover:bg-amber-900/30">Générer le lien</button>
                </div>
            </div>
        `,
    });

    const tabManual = overlay?.querySelector("#reset-tab-manual");
    const tabLink = overlay?.querySelector("#reset-tab-link");
    const panelManual = overlay?.querySelector("#reset-panel-manual");
    const panelLink = overlay?.querySelector("#reset-panel-link");

    function activateTab(which) {
        const isManual = which === "manual";
        tabManual?.classList.toggle("border-amber-600", isManual);
        tabManual?.classList.toggle("bg-amber-950/50", isManual);
        tabManual?.classList.toggle("text-amber-200", isManual);
        tabManual?.classList.toggle("border-slate-600", !isManual);
        tabManual?.classList.toggle("text-slate-400", !isManual);
        tabLink?.classList.toggle("border-amber-600", !isManual);
        tabLink?.classList.toggle("bg-amber-950/50", !isManual);
        tabLink?.classList.toggle("text-amber-200", !isManual);
        tabLink?.classList.toggle("border-slate-600", isManual);
        tabLink?.classList.toggle("text-slate-400", isManual);
        panelManual?.classList.toggle("hidden", !isManual);
        panelLink?.classList.toggle("hidden", isManual);
    }
    tabManual?.addEventListener("click", () => activateTab("manual"));
    tabLink?.addEventListener("click", () => activateTab("link"));

    const form = overlay?.querySelector("#admin-reset-password-form");
    const errorNode = overlay?.querySelector("#admin-reset-password-error");
    const submitButton = overlay?.querySelector("#admin-reset-password-submit");
    if (form instanceof HTMLFormElement && errorNode instanceof HTMLElement && submitButton instanceof HTMLButtonElement) {
        form.addEventListener("submit", async (event) => {
            event.preventDefault();
            submitButton.disabled = true;
            errorNode.classList.add("hidden");
            const formData = new FormData(form);
            try {
                const response = await apiFetch(`/api/admin/users/${encodeURIComponent(username)}/reset-password`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ new_password: String(formData.get("new_password") || "") }),
                });
                if (!response.ok) {
                    const payloadText = await response.text();
                    let detail = payloadText || `HTTP ${response.status}`;
                    try { detail = JSON.parse(payloadText)?.detail || detail; } catch (_) {}
                    throw new Error(detail);
                }
                closeOverlayModal();
                await refreshAdminPanel();
                showToastMessage("Mot de passe réinitialisé", `Le mot de passe de ${username} a été réinitialisé.`);
            } catch (error) {
                errorNode.textContent = error?.message || "Réinitialisation impossible.";
                errorNode.classList.remove("hidden");
            } finally {
                submitButton.disabled = false;
            }
        });
    }

    const generateBtn = overlay?.querySelector("#reset-link-generate");
    const linkError = overlay?.querySelector("#reset-link-error");
    const linkResult = overlay?.querySelector("#reset-link-result");
    const linkInput = overlay?.querySelector("#reset-link-url");
    const copyBtn = overlay?.querySelector("#reset-link-copy");

    generateBtn?.addEventListener("click", async () => {
        if (!(generateBtn instanceof HTMLButtonElement)) return;
        generateBtn.disabled = true;
        linkError?.classList.add("hidden");
        linkResult?.classList.add("hidden");
        try {
            const response = await apiFetch(`/api/admin/users/${encodeURIComponent(username)}/generate-reset-link`, {
                method: "POST",
            });
            if (!response.ok) {
                const txt = await response.text();
                let detail = txt || `HTTP ${response.status}`;
                try { detail = JSON.parse(txt)?.detail || detail; } catch (_) {}
                throw new Error(detail);
            }
            const payload = await response.json();
            if (linkInput instanceof HTMLInputElement) linkInput.value = payload.reset_url || "";
            linkResult?.classList.remove("hidden");
        } catch (err) {
            if (linkError instanceof HTMLElement) {
                linkError.textContent = err?.message || "Impossible de générer le lien.";
                linkError.classList.remove("hidden");
            }
        } finally {
            generateBtn.disabled = false;
        }
    });

    copyBtn?.addEventListener("click", async () => {
        if (!(linkInput instanceof HTMLInputElement) || !linkInput.value) return;
        try {
            await navigator.clipboard.writeText(linkInput.value);
            showToastMessage("Lien copié", "Le lien de réinitialisation a été copié.");
        } catch (_) {
            linkInput.select();
            document.execCommand("copy");
            showToastMessage("Lien copié", "Le lien de réinitialisation a été copié.");
        }
    });
}

function showCreateGroupModal() {
    showGroupFormModal(null);
}

function showCreateUserModal() {
    const actorIsGroupAdmin = isGroupAdmin();
    const actorGroups = (currentUser?.groups || []).map((item) => String(item || "")).filter(Boolean);
    const availableGroups = actorIsGroupAdmin ? actorGroups : latestAdminGroups.map((group) => group.name);
    const groupOptions = [
        '<option value="">Aucun groupe (accès LAB = aucun)</option>',
        ...availableGroups.map((groupName) => `<option value="${escapeHtml(groupName)}" ${actorIsGroupAdmin && availableGroups.length === 1 ? "selected" : ""}>${escapeHtml(groupName)}</option>`),
    ].join("");

    const overlay = showOverlayModal({
        title: "Créer un utilisateur",
        tone: "slate",
        widthClass: "max-w-2xl",
        bodyHtml: `
            <form id="admin-create-user-form" class="grid gap-4 md:grid-cols-2">
                <div>
                    <label class="block text-xs uppercase tracking-wide text-slate-400 mb-1">Nom d'utilisateur</label>
                    <input name="username" required class="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-fuchsia-500" />
                </div>
                <div>
                    <label class="block text-xs uppercase tracking-wide text-slate-400 mb-1">Nom complet</label>
                    <input name="full_name" required class="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-fuchsia-500" />
                </div>
                <div>
                    <label class="block text-xs uppercase tracking-wide text-slate-400 mb-1">Email</label>
                    <input name="email" type="email" required class="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-fuchsia-500" />
                </div>
                <div>
                    <label class="block text-xs uppercase tracking-wide text-slate-400 mb-1">Rôle</label>
                    <select name="role" ${actorIsGroupAdmin ? "disabled" : ""} class="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-fuchsia-500 disabled:cursor-not-allowed disabled:opacity-50">
                        <option value="user">Utilisateur</option>
                        ${actorIsGroupAdmin ? "" : '<option value="group-admin">Group Admin</option><option value="admin">Admin</option>'}
                    </select>
                </div>
                <div>
                    <label class="block text-xs uppercase tracking-wide text-slate-400 mb-1">Groupe</label>
                    <select name="group_name" ${actorIsGroupAdmin && availableGroups.length === 1 ? "disabled" : ""} class="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-fuchsia-500 disabled:cursor-not-allowed disabled:opacity-50">
                        ${groupOptions}
                    </select>
                </div>
                <div class="md:col-span-2">
                    <label class="block text-xs uppercase tracking-wide text-slate-400 mb-1">Mot de passe initial</label>
                    <input name="password" type="password" required minlength="10" class="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-fuchsia-500" />
                </div>
                <p id="admin-create-user-error" class="hidden text-sm text-rose-300 md:col-span-2"></p>
                <div class="md:col-span-2 flex items-center justify-end gap-2">
                    <button type="button" data-modal-close="true" class="text-xs px-3 py-2 rounded border border-slate-600 hover:bg-slate-800">Annuler</button>
                    <button type="submit" id="admin-create-user-submit" class="text-xs px-3 py-2 rounded border border-fuchsia-700 text-fuchsia-200 hover:bg-fuchsia-900/30">Créer</button>
                </div>
            </form>
        `,
    });

    const form = overlay?.querySelector("#admin-create-user-form");
    const errorNode = overlay?.querySelector("#admin-create-user-error");
    const submitButton = overlay?.querySelector("#admin-create-user-submit");

    if (!(form instanceof HTMLFormElement) || !(errorNode instanceof HTMLElement) || !(submitButton instanceof HTMLButtonElement)) {
        return;
    }

    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        submitButton.disabled = true;
        errorNode.classList.add("hidden");

        const formData = new FormData(form);
        try {
            const response = await apiFetch("/api/admin/users", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    username: String(formData.get("username") || "").trim(),
                    full_name: String(formData.get("full_name") || "").trim(),
                    email: String(formData.get("email") || "").trim(),
                    password: String(formData.get("password") || ""),
                    role: actorIsGroupAdmin ? "user" : String(formData.get("role") || "user"),
                    group_name: actorIsGroupAdmin && availableGroups.length === 1
                        ? availableGroups[0]
                        : (String(formData.get("group_name") || "").trim() || null),
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
            closeOverlayModal();
            await refreshAdminPanel();
            showToastMessage("Utilisateur créé", "Le nouvel utilisateur a été ajouté.");
        } catch (error) {
            errorNode.textContent = error?.message || "Création impossible.";
            errorNode.classList.remove("hidden");
        } finally {
            submitButton.disabled = false;
        }
    });
}
