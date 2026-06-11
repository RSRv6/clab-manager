async function showProvisionModal(vm) {
    // Fetch defaults (repo URL, branch) from backend
    let defaultRepoUrl = "";
    let defaultBranch = "main";
    try {
        const resp = await apiFetch("/api/admin/provision-defaults");
        if (resp.ok) {
            const data = await resp.json();
            defaultRepoUrl = String(data.repo_url || "");
            defaultBranch = String(data.branch || "main");
        }
    } catch (_) {}

    const vmId = String(vm.id || "");
    const vmName = String(vm.name || vmId);
    const vmIp = String(vm.ip || "");

    const overlay = showOverlayModal({
        title: `Provisionner ${vmName}`,
        tone: "slate",
        widthClass: "max-w-2xl",
        bodyHtml: `
            <div id="provision-form-section">
                <p class="text-xs text-slate-400 mb-4">
                    Installe et configure l'agent VLM sur <span class="text-cyan-300 font-mono">${escapeHtml(vmIp)}</span>
                    via SSH en utilisant les scripts de déploiement existants.<br>
                    Le token API <span class="text-amber-300">(${escapeHtml(maskToken(String(vm.token || "")))})</span>
                    sera injecté automatiquement dans la configuration de l'agent.
                </p>
                <form id="provision-form" class="grid gap-4 md:grid-cols-2">
                    <div>
                        <label class="block text-xs uppercase tracking-wide text-slate-400 mb-1">Utilisateur SSH</label>
                        <input name="ssh_user" value="clab-user" required
                            class="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-emerald-500" />
                    </div>
                    <div>
                        <label class="block text-xs uppercase tracking-wide text-slate-400 mb-1">Mot de passe SSH <span class="text-slate-600">(optionnel si clé)</span></label>
                        <input name="ssh_password" type="password" autocomplete="off"
                            class="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-emerald-500" />
                    </div>
                    <div>
                        <label class="block text-xs uppercase tracking-wide text-slate-400 mb-1">Mot de passe sudo <span class="text-slate-600">(optionnel)</span></label>
                        <input name="sudo_password" type="password" autocomplete="off"
                            class="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-emerald-500" />
                    </div>
                    <div>
                        <label class="block text-xs uppercase tracking-wide text-slate-400 mb-1">Répertoire app <span class="text-slate-600">(auto si vide)</span></label>
                        <input name="app_dir" placeholder="/opt/virtual-labs-management"
                            class="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-emerald-500" />
                    </div>
                    <div class="md:col-span-2">
                        <label class="block text-xs uppercase tracking-wide text-slate-400 mb-1">URL du dépôt Git</label>
                        <input name="repo_url" value="${escapeHtml(defaultRepoUrl)}" required
                            class="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-emerald-500" />
                        <p class="text-[11px] text-slate-500 mt-1">SSH URL accessible depuis la VM cible, ex: <code class="text-slate-400">ssh://user@central-ip/home/user/git/repo.git</code></p>
                    </div>
                    <div>
                        <label class="block text-xs uppercase tracking-wide text-slate-400 mb-1">Mot de passe Git SSH <span class="text-slate-600">(si dépôt protégé par MDP)</span></label>
                        <input name="repo_ssh_password" type="password" autocomplete="off"
                            class="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-emerald-500" />
                    </div>
                    <div>
                        <label class="block text-xs uppercase tracking-wide text-slate-400 mb-1">Branche</label>
                        <input name="branch" value="${escapeHtml(defaultBranch)}"
                            class="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-emerald-500" />
                    </div>
                    <p id="provision-form-error" class="hidden text-sm text-rose-300 md:col-span-2"></p>
                    <div class="md:col-span-2 flex items-center justify-end gap-2">
                        <button type="button" data-modal-close="true" class="text-xs px-3 py-2 rounded border border-slate-600 hover:bg-slate-800">Annuler</button>
                        <button type="submit" id="provision-submit"
                            class="text-xs px-3 py-2 rounded border border-emerald-700 text-emerald-200 hover:bg-emerald-900/30">
                            Lancer le provisionnement
                        </button>
                    </div>
                </form>
            </div>
            <div id="provision-log-section" class="hidden">
                <div id="provision-status-banner" class="hidden mb-3 px-3 py-2 rounded text-xs font-semibold"></div>
                <pre id="provision-log"
                    class="bg-slate-950 border border-slate-800 rounded p-3 text-[11px] text-slate-300 font-mono overflow-auto max-h-96 whitespace-pre-wrap"></pre>
                <div class="mt-3 flex justify-end">
                    <button type="button" data-modal-close="true" id="provision-close-btn"
                        class="hidden text-xs px-3 py-2 rounded border border-slate-600 hover:bg-slate-800">Fermer</button>
                </div>
            </div>
        `,
    });

    if (!overlay) return;

    const form = overlay.querySelector("#provision-form");
    const errorNode = overlay.querySelector("#provision-form-error");
    const submitBtn = overlay.querySelector("#provision-submit");
    const formSection = overlay.querySelector("#provision-form-section");
    const logSection = overlay.querySelector("#provision-log-section");
    const logEl = overlay.querySelector("#provision-log");
    const statusBanner = overlay.querySelector("#provision-status-banner");
    const closeBtn = overlay.querySelector("#provision-close-btn");

    if (!(form instanceof HTMLFormElement)) return;

    let pollInterval = null;
    let lastLineCount = 0;

    function stopPolling() {
        if (pollInterval !== null) {
            clearInterval(pollInterval);
            pollInterval = null;
        }
    }

    async function pollJob(jobId) {
        try {
            const resp = await apiFetch(`/api/admin/provision-jobs/${encodeURIComponent(jobId)}`);
            if (!resp.ok) return;
            const data = await resp.json();
            const job = data.item;
            const lines = Array.isArray(job.lines) ? job.lines : [];

            if (lines.length > lastLineCount) {
                const newLines = lines.slice(lastLineCount);
                logEl.textContent += newLines.join("\n") + (newLines.length ? "\n" : "");
                lastLineCount = lines.length;
                logEl.scrollTop = logEl.scrollHeight;
            }

            if (job.status === "success") {
                stopPolling();
                statusBanner.textContent = "✓ Provisionnement terminé avec succès";
                statusBanner.className = "mb-3 px-3 py-2 rounded text-xs font-semibold bg-emerald-900/40 border border-emerald-700 text-emerald-200";
                statusBanner.classList.remove("hidden");
                closeBtn.classList.remove("hidden");
            } else if (job.status === "error") {
                stopPolling();
                statusBanner.textContent = "✗ Provisionnement échoué — consultez les logs ci-dessous";
                statusBanner.className = "mb-3 px-3 py-2 rounded text-xs font-semibold bg-rose-900/40 border border-rose-700 text-rose-200";
                statusBanner.classList.remove("hidden");
                closeBtn.classList.remove("hidden");
            }
        } catch (_) {}
    }

    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        submitBtn.disabled = true;
        errorNode.classList.add("hidden");

        const fd = new FormData(form);
        const payload = {
            ssh_user: String(fd.get("ssh_user") || "").trim(),
            ssh_password: String(fd.get("ssh_password") || "").trim() || null,
            sudo_password: String(fd.get("sudo_password") || "").trim() || null,
            app_dir: String(fd.get("app_dir") || "").trim() || null,
            branch: String(fd.get("branch") || "main").trim(),
            repo_url: String(fd.get("repo_url") || "").trim(),
            repo_ssh_password: String(fd.get("repo_ssh_password") || "").trim() || null,
        };

        if (!payload.ssh_user) {
            errorNode.textContent = "L'utilisateur SSH est obligatoire.";
            errorNode.classList.remove("hidden");
            submitBtn.disabled = false;
            return;
        }
        if (!payload.repo_url) {
            errorNode.textContent = "L'URL du dépôt Git est obligatoire.";
            errorNode.classList.remove("hidden");
            submitBtn.disabled = false;
            return;
        }

        try {
            const resp = await apiFetch(`/api/admin/vms/${encodeURIComponent(vmId)}/provision`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            if (!resp.ok) {
                const text = await resp.text();
                let detail = text || `HTTP ${resp.status}`;
                try { detail = JSON.parse(text)?.detail || detail; } catch (_) {}
                throw new Error(detail);
            }
            const data = await resp.json();
            const jobId = String(data.job_id || "");

            // Switch to log view
            formSection.classList.add("hidden");
            logSection.classList.remove("hidden");
            logEl.textContent = "";
            lastLineCount = 0;

            // Start polling
            pollInterval = setInterval(() => pollJob(jobId), 1500);
            pollJob(jobId);

        } catch (error) {
            errorNode.textContent = error?.message || "Erreur lors du lancement.";
            errorNode.classList.remove("hidden");
            submitBtn.disabled = false;
        }
    });

    // Clean up polling when modal is closed
    const modalRoot = overlay.closest("#overlay-modal-root");
    if (modalRoot) {
        const observer = new MutationObserver(() => {
            if (!document.contains(overlay)) {
                stopPolling();
                observer.disconnect();
            }
        });
        observer.observe(document.body, { childList: true, subtree: true });
    }
}

// Expose explicitly for cross-file handlers loaded in separate script tags.
window.showProvisionModal = showProvisionModal;
