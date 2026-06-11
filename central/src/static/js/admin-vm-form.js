function showVMFormModal(vm = null) {
    const isEdit = Boolean(vm);
    const initial = {
        id: String(vm?.id || ""),
        name: String(vm?.name || ""),
        ip: String(vm?.ip || ""),
        port: Number(vm?.port || 8081),
        token: String(vm?.token || ""),
        management_subnet: String(vm?.management_subnet || ""),
        ssh_user: String(vm?.ssh_user || "clab-user"),
    };

    const idFieldHtml = isEdit
        ? `
                <div>
                    <label class="block text-xs uppercase tracking-wide text-slate-400 mb-1">ID VM</label>
                    <input name="id" value="${escapeHtml(initial.id)}" required class="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-cyan-500" />
                </div>
        `
        : `
                <div>
                    <label class="block text-xs uppercase tracking-wide text-slate-400 mb-1">ID VM</label>
                    <input value="Généré automatiquement" disabled class="w-full rounded-lg border border-slate-800 bg-slate-900 px-3 py-2 text-sm text-slate-500" />
                </div>
        `;

    const tokenFieldHtml = isEdit
        ? `
                <div class="md:col-span-2">
                    <label class="block text-xs uppercase tracking-wide text-slate-400 mb-1">Token API agent</label>
                    <input name="token" value="${escapeHtml(initial.token)}" required class="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-cyan-500" />
                </div>
        `
        : `
                <div class="md:col-span-2">
                    <label class="block text-xs uppercase tracking-wide text-slate-400 mb-1">Token API agent</label>
                    <input value="Généré automatiquement (format robuste 32 caractères)" disabled class="w-full rounded-lg border border-slate-800 bg-slate-900 px-3 py-2 text-sm text-slate-500" />
                </div>
        `;

    const overlay = showOverlayModal({
        title: isEdit ? `Editer la VM ${initial.id}` : "Créer une VM",
        tone: "slate",
        widthClass: "max-w-2xl",
        bodyHtml: `
            <form id="admin-vm-form" class="grid gap-4 md:grid-cols-2">
                ${idFieldHtml}
                <div>
                    <label class="block text-xs uppercase tracking-wide text-slate-400 mb-1">Nom VM</label>
                    <input name="name" value="${escapeHtml(initial.name)}" required class="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-cyan-500" />
                </div>
                <div>
                    <label class="block text-xs uppercase tracking-wide text-slate-400 mb-1">IP</label>
                    <input name="ip" value="${escapeHtml(initial.ip)}" required class="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-cyan-500" />
                </div>
                <div>
                    <label class="block text-xs uppercase tracking-wide text-slate-400 mb-1">Port</label>
                    <input name="port" type="number" min="1" max="65535" value="${escapeHtml(initial.port)}" required class="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-cyan-500" />
                </div>
                <div>
                    <label class="block text-xs uppercase tracking-wide text-slate-400 mb-1">SSH user déploiement</label>
                    <input name="ssh_user" value="${escapeHtml(initial.ssh_user)}" class="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-cyan-500" />
                </div>
                <div class="md:col-span-2">
                    <label class="block text-xs uppercase tracking-wide text-slate-400 mb-1">Subnet management (optionnel)</label>
                    <input name="management_subnet" value="${escapeHtml(initial.management_subnet)}" placeholder="172.35.0.0/16" class="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-cyan-500" />
                    <p class="text-[11px] text-slate-500 mt-1">Automatique: vérification d'unicité, route netplan et netplan apply côté central.</p>
                </div>
                <div class="md:col-span-2">
                    <label class="block text-xs uppercase tracking-wide text-slate-400 mb-1">SSH password (optionnel, stocké pour déploiement)</label>
                    <input name="ssh_password" type="password" autocomplete="new-password" placeholder="Laisser vide pour garder l'existant" class="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-cyan-500" />
                </div>
                <div class="md:col-span-2">
                    <label class="block text-xs uppercase tracking-wide text-slate-400 mb-1">Sudo password (optionnel, stocké pour déploiement)</label>
                    <input name="sudo_password" type="password" autocomplete="new-password" placeholder="Laisser vide pour garder l'existant" class="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-cyan-500" />
                </div>
                ${tokenFieldHtml}
                <p id="admin-vm-error" class="hidden text-sm text-rose-300 md:col-span-2"></p>
                <div class="md:col-span-2 flex items-center justify-end gap-2">
                    <button type="button" data-modal-close="true" class="text-xs px-3 py-2 rounded border border-slate-600 hover:bg-slate-800">Annuler</button>
                    <button type="submit" id="admin-vm-submit" class="text-xs px-3 py-2 rounded border border-cyan-700 text-cyan-200 hover:bg-cyan-900/30">${isEdit ? "Enregistrer" : "Créer"}</button>
                </div>
            </form>
        `,
    });

    const form = overlay?.querySelector("#admin-vm-form");
    const errorNode = overlay?.querySelector("#admin-vm-error");
    const submitButton = overlay?.querySelector("#admin-vm-submit");
    if (!(form instanceof HTMLFormElement) || !(errorNode instanceof HTMLElement) || !(submitButton instanceof HTMLButtonElement)) {
        return;
    }

    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        submitButton.disabled = true;
        errorNode.classList.add("hidden");

        const formData = new FormData(form);
        const payload = {
            name: String(formData.get("name") || "").trim(),
            ip: String(formData.get("ip") || "").trim(),
            port: Number(formData.get("port") || 8081),
            ssh_user: String(formData.get("ssh_user") || "").trim() || null,
            management_subnet: String(formData.get("management_subnet") || "").trim(),
        };
        const sshPassword = String(formData.get("ssh_password") || "").trim();
        const sudoPassword = String(formData.get("sudo_password") || "").trim();
        if (sshPassword) {
            payload.ssh_password = sshPassword;
        }
        if (sudoPassword) {
            payload.sudo_password = sudoPassword;
        }
        if (isEdit) {
            payload.id = String(formData.get("id") || "").trim();
            payload.token = String(formData.get("token") || "").trim();
        }

        try {
            const endpoint = isEdit
                ? `/api/admin/vms/${encodeURIComponent(initial.id)}`
                : "/api/admin/vms";
            const response = await apiFetch(endpoint, {
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
            const payloadResponse = await response.json();
            const savedVmId = String(payloadResponse?.item?.id || payload.id || initial.id || "");

            closeOverlayModal();
            await refreshAdminPanel();
            showToastMessage(isEdit ? "VM mise à jour" : "VM créée", `La VM ${savedVmId} a été enregistrée.`);
        } catch (error) {
            errorNode.textContent = error?.message || "Enregistrement impossible.";
            errorNode.classList.remove("hidden");
        } finally {
            submitButton.disabled = false;
        }
    });
}

function showCreateVMModal() {
    showVMFormModal(null);
}

// Expose explicitly for cross-file handlers loaded in separate script tags.
window.showVMFormModal = showVMFormModal;
window.showCreateVMModal = showCreateVMModal;
