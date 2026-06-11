function makeDocumentationKey(vmId, labName) {
    return `${String(vmId || "").trim()}::${String(labName || "").trim()}`;
}

function makeDocumentationAnchorId(vmId, labName) {
    const raw = `${String(vmId || "").trim()}-${String(labName || "").trim()}`;
    return `doc-lab-${raw.replace(/[^a-zA-Z0-9_-]+/g, "-").toLowerCase()}`;
}

function scrollToDocumentationAnchor(vmId, labName) {
    const anchorId = makeDocumentationAnchorId(vmId, labName);
    const section = document.getElementById(anchorId);
    if (!section) {
        pendingDocumentationAnchor = { vmId, labName };
        return;
    }
    if (section instanceof HTMLDetailsElement && !section.open) {
        section.open = true;
    }
    section.scrollIntoView({ behavior: "smooth", block: "start" });
    section.classList.add("ring-2", "ring-cyan-500/70");
    setTimeout(() => {
        section.classList.remove("ring-2", "ring-cyan-500/70");
    }, 1800);
    pendingDocumentationAnchor = null;
}

function openDocumentationForLab(vmId, labName) {
    showView("doc");
    scrollToDocumentationAnchor(vmId, labName);
}

function exitDocumentationFullscreen() {
    if (!(documentationFullscreenCard instanceof HTMLElement)) {
        return;
    }
    documentationFullscreenCard.classList.remove("doc-card-fullscreen");
    documentationFullscreenCard = null;
    document.body.classList.remove("doc-fullscreen-open");
}

function toggleDocumentationFullscreen(card) {
    if (!(card instanceof HTMLElement)) {
        return;
    }
    if (documentationFullscreenCard && documentationFullscreenCard !== card) {
        documentationFullscreenCard.classList.remove("doc-card-fullscreen");
    }

    const shouldOpen = !card.classList.contains("doc-card-fullscreen");
    if (!shouldOpen) {
        exitDocumentationFullscreen();
        return;
    }

    card.classList.add("doc-card-fullscreen");
    documentationFullscreenCard = card;
    document.body.classList.add("doc-fullscreen-open");
}

function _ensureMermaidReady() {
    if (!window.mermaid) {
        return false;
    }
    if (!window.__vlmMermaidInitialized) {
        window.mermaid.initialize({
            startOnLoad: false,
            securityLevel: "loose",
            theme: "dark",
        });
        window.__vlmMermaidInitialized = true;
    }
    return true;
}

function sanitizeRichHtml(value) {
    const html = String(value || "").trim();
    if (!html) {
        return "<p>Aucune documentation texte pour le moment.</p>";
    }
    if (window.DOMPurify) {
        return window.DOMPurify.sanitize(html, {
            USE_PROFILES: { html: true },
            FORBID_TAGS: ["script", "style"],
        });
    }
    return `<p>${escapeHtml(html)}</p>`;
}

function initializeDocumentationEditors(items) {
    documentationEditors.clear();
    if (!canEditDocumentation() || !window.Quill || !Array.isArray(items)) {
        return;
    }

    for (const item of items) {
        const docKey = makeDocumentationKey(item.vm_id, item.lab_name);
        const cardId = makeDocumentationAnchorId(item.vm_id, item.lab_name);
        const host = document.getElementById(`doc-editor-host-${cardId}`);
        if (!(host instanceof HTMLElement)) {
            continue;
        }

        const quill = new window.Quill(host, {
            theme: "snow",
            placeholder: "Documentation texte du LAB...",
            modules: {
                toolbar: {
                    container: [
                        [{ header: [1, 2, 3, false] }],
                        ["bold", "italic", "underline", "strike"],
                        [{ color: [] }, { background: [] }],
                        [{ list: "ordered" }, { list: "bullet" }],
                        [{ align: [] }],
                        ["blockquote", "code-block"],
                        ["link", "image"],
                        ["clean"],
                    ],
                    handlers: {
                        image() {
                            const input = document.createElement("input");
                            input.type = "file";
                            input.accept = "image/*";
                            input.onchange = () => {
                                const file = input.files && input.files[0];
                                if (!file) {
                                    return;
                                }
                                const reader = new FileReader();
                                reader.onload = () => {
                                    const range = quill.getSelection(true);
                                    const position = range ? range.index : quill.getLength();
                                    quill.insertEmbed(position, "image", String(reader.result || ""), "user");
                                    quill.setSelection(position + 1, 0, "user");
                                };
                                reader.readAsDataURL(file);
                            };
                            input.click();
                        },
                    },
                },
            },
        });

        quill.root.innerHTML = String(item.text || "");
        documentationEditors.set(docKey, quill);
    }
}

async function renderDocumentationDiagrams(items) {
    if (!_ensureMermaidReady()) {
        return;
    }

    for (let index = 0; index < items.length; index += 1) {
        const item = items[index];
        const diagramCode = String(item?.diagram || "").trim();
        if (!diagramCode) {
            continue;
        }

        const hostId = `doc-diagram-host-${makeDocumentationAnchorId(item.vm_id, item.lab_name)}`;
        const host = document.getElementById(hostId);
        if (!(host instanceof HTMLElement)) {
            continue;
        }

        try {
            const renderId = `doc-diagram-${index}-${Date.now()}`;
            const result = await window.mermaid.render(renderId, diagramCode);
            host.innerHTML = result.svg;
        } catch (error) {
            host.innerHTML = `<pre class="text-xs text-rose-300 whitespace-pre-wrap">Diagramme invalide:\n${escapeHtml(String(error?.message || "Erreur Mermaid"))}</pre>`;
        }
    }
}

function _buildGeneralSectionHtml(section, isOpen) {
    const title = String((section || {}).title || "").trim();
    const text = String((section || {}).text || "").trim();
    const updatedAt = formatCompletionDate((section || {}).updated_at);
    const updatedBy = String((section || {}).updated_by || "");
    const metaHtml = (updatedAt ? `Mis à jour le ${escapeHtml(updatedAt)}` : "Jamais modifié") + (updatedBy ? ` · par ${escapeHtml(updatedBy)}` : "");

    if (!isAdmin()) {
        if (!title && !text) return "";
        return `
            <details id="general-doc-section" ${isOpen ? "open" : ""} class="group rounded-xl border border-l-4 border-slate-800 border-l-fuchsia-600/60 bg-slate-950/70 p-4 mb-3">
                <summary class="cursor-pointer list-none flex items-start justify-between gap-3">
                    <div>
                        <h3 class="text-sm font-semibold text-fuchsia-200">${escapeHtml(title || "Guide général")}</h3>
                        <p class="text-[11px] text-slate-500 mt-1">${metaHtml}</p>
                    </div>
                    <span class="text-slate-500 group-open:rotate-180 transition-transform">▾</span>
                </summary>
                <div class="mt-3 rounded-lg border border-slate-800 bg-slate-950/70 overflow-hidden">
                    <div class="ql-snow border-0"><div class="ql-editor text-xs text-slate-200 min-h-[80px]">${sanitizeRichHtml(text)}</div></div>
                </div>
            </details>
        `;
    }

    return `
        <details id="general-doc-section" ${isOpen ? "open" : ""} class="group rounded-xl border border-l-4 border-slate-800 border-l-fuchsia-600/60 bg-slate-950/70 p-4 mb-3">
            <summary class="cursor-pointer list-none flex items-start justify-between gap-3">
                <div>
                    <h3 class="text-sm font-semibold text-fuchsia-200">Section générale <span class="text-[11px] font-normal text-slate-400">(guide d'utilisation / rebond…)</span></h3>
                    <p class="text-[11px] text-slate-500 mt-1">${metaHtml}</p>
                </div>
                <span class="text-slate-500 group-open:rotate-180 transition-transform">▾</span>
            </summary>
            <div class="mt-3 space-y-3">
                <div>
                    <p class="text-xs uppercase tracking-wide text-slate-400 mb-1">Titre</p>
                    <input id="general-section-title-input" type="text" maxlength="200" value="${escapeHtml(title)}"
                        class="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-xs text-slate-100 focus:outline-none focus:ring-2 focus:ring-fuchsia-500"
                        placeholder="Ex: Guide d'utilisation de l'outil et du serveur de rebond" />
                </div>
                <div>
                    <p class="text-xs uppercase tracking-wide text-slate-400 mb-1">Contenu</p>
                    <div id="general-section-editor-host" class="doc-rich-editor rounded-lg border border-slate-700 bg-slate-950 overflow-hidden"></div>
                </div>
                <div class="flex items-center gap-2 mt-2">
                    <button type="button" data-action="save-general-doc" class="text-xs px-2.5 py-1.5 rounded border border-fuchsia-700 text-fuchsia-200 hover:bg-fuchsia-900/30">Enregistrer la section</button>
                </div>
            </div>
        </details>
    `;
}

function initializeGeneralSectionEditor(section) {
    const host = document.getElementById("general-section-editor-host");
    if (!(host instanceof HTMLElement) || !window.Quill) return;
    if (documentationEditors.has("__general__")) {
        documentationEditors.get("__general__").off();
        documentationEditors.delete("__general__");
    }
    const quill = new window.Quill(host, {
        theme: "snow",
        placeholder: "Contenu de la section générale (guide d'utilisation, accès rebond…)",
        modules: {
            toolbar: [
                [{ header: [1, 2, 3, false] }],
                ["bold", "italic", "underline", "strike"],
                [{ color: [] }, { background: [] }],
                [{ list: "ordered" }, { list: "bullet" }],
                [{ align: [] }],
                ["blockquote", "code-block"],
                ["link"],
                ["clean"],
            ],
        },
    });
    quill.root.innerHTML = String((section || {}).text || "");
    documentationEditors.set("__general__", quill);
}

function renderDocumentationPanel(items) {
    const container = document.getElementById("doc-labs");
    if (!container) {
        return;
    }

    const expandedDocumentation = new Set(
        Array.from(container.querySelectorAll("details[data-doc-key][open]"))
            .map((node) => String(node.getAttribute("data-doc-key") || ""))
            .filter(Boolean),
    );
    const generalSectionWasOpen = Boolean(container.querySelector("#general-doc-section[open]"));

    const generalSectionHtml = _buildGeneralSectionHtml(latestGeneralDocSection, generalSectionWasOpen);

    if (!Array.isArray(items) || !items.length) {
        documentationEditors.clear();
        container.innerHTML = generalSectionHtml + '<div class="rounded-xl border border-slate-800 bg-slate-900/70 p-4 text-sm text-slate-400">Aucune documentation disponible pour les LABs visibles.</div>';
        initializeGeneralSectionEditor(latestGeneralDocSection);
        return;
    }

    const vmBuckets = new Map();
    for (const item of items) {
        const vmId = String(item.vm_id || "");
        const vmName = String(item.vm_name || vmId);
        const key = vmId || vmName;
        if (!vmBuckets.has(key)) {
            vmBuckets.set(key, { vmId, vmName, labs: [] });
        }
        vmBuckets.get(key).labs.push(item);
    }

    const sortedVm = Array.from(vmBuckets.values()).sort((left, right) =>
        String(left.vmName || "").toLowerCase().localeCompare(String(right.vmName || "").toLowerCase())
    );

    container.innerHTML = generalSectionHtml + sortedVm.map((vm) => {
        const labsHtml = vm.labs
            .sort((left, right) => String(left.lab_name || "").toLowerCase().localeCompare(String(right.lab_name || "").toLowerCase()))
            .map((item) => {
                const text = String(item.text || "");
                const diagram = String(item.diagram || "");
                const pdfAvailable = Boolean(item.pdf_available);
                const updatedAt = formatCompletionDate(item.updated_at);
                const updatedBy = String(item.updated_by || "");
                const docKey = makeDocumentationKey(item.vm_id, item.lab_name);
                const cardId = makeDocumentationAnchorId(item.vm_id, item.lab_name);
                const diagramHostId = `doc-diagram-host-${cardId}`;
                const canEdit = canEditDocumentation();
                const isAnchorTarget = String(pendingDocumentationAnchor?.vmId || "") === String(item.vm_id || "")
                    && String(pendingDocumentationAnchor?.labName || "") === String(item.lab_name || "");
                const isOpen = expandedDocumentation.has(docKey) || isAnchorTarget;
                const textField = canEdit
                    ? `<div id="doc-editor-host-${cardId}" data-doc-field="text-editor" class="doc-rich-editor rounded-lg border border-slate-700 bg-slate-950 overflow-hidden"></div>`
                    : `<div class="rounded-lg border border-slate-800 bg-slate-950/70 overflow-hidden"><div class="ql-snow border-0"><div class="ql-editor text-xs text-slate-200 min-h-[120px]">${sanitizeRichHtml(text)}</div></div></div>`;
                const diagramField = canEdit
                    ? `<textarea data-doc-field="diagram" class="w-full min-h-[110px] rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-xs text-slate-100 font-mono focus:outline-none focus:ring-2 focus:ring-cyan-500" placeholder="Code Mermaid (optionnel)...">${escapeHtml(diagram)}</textarea>`
                    : (diagram
                        ? `<div id="${diagramHostId}" class="rounded-lg border border-slate-800 bg-slate-950/70 p-3 overflow-x-auto"></div>`
                        : '<p class="text-xs text-slate-500">Aucun diagramme.</p>');

                const editorActions = `<div class="flex flex-wrap items-center gap-2 mt-2">
                        <button type="button" data-action="toggle-doc-fullscreen" class="text-xs px-2.5 py-1.5 rounded border border-slate-600 text-slate-200 hover:bg-slate-800">Plein ecran</button>
                        ${canEdit ? `<button type="button" data-action="save-lab-doc" data-vm-id="${escapeHtml(item.vm_id)}" data-lab-name="${escapeHtml(item.lab_name)}" class="text-xs px-2.5 py-1.5 rounded border border-cyan-700 text-cyan-200 hover:bg-cyan-900/30">Enregistrer texte/diagramme</button>` : ""}
                        ${canEdit ? `<input type="file" data-doc-field="pdf" accept="application/pdf" class="text-xs text-slate-300" />` : ""}
                        ${canEdit ? `<button type="button" data-action="upload-lab-doc-pdf" data-vm-id="${escapeHtml(item.vm_id)}" data-lab-name="${escapeHtml(item.lab_name)}" class="text-xs px-2.5 py-1.5 rounded border border-emerald-700 text-emerald-200 hover:bg-emerald-900/30">Uploader PDF</button>` : ""}
                        ${canEdit && pdfAvailable ? `<button type="button" data-action="delete-lab-doc-pdf" data-vm-id="${escapeHtml(item.vm_id)}" data-lab-name="${escapeHtml(item.lab_name)}" class="text-xs px-2.5 py-1.5 rounded border border-rose-700 text-rose-200 hover:bg-rose-900/30">Supprimer PDF</button>` : ""}
                    </div>`;

                const pdfPreview = pdfAvailable
                    ? `<div class="mt-2">
                            <div class="flex items-center justify-between gap-2 mb-1">
                                <p class="text-xs text-emerald-300">PDF: ${escapeHtml(item.pdf_name || "document.pdf")}</p>
                                <div class="flex items-center gap-3">
                                    <a class="text-xs text-cyan-300 hover:underline" target="_blank" rel="noopener" href="/api/docs/${encodeURIComponent(item.vm_id)}/labs/${encodeURIComponent(item.lab_name)}/pdf">Ouvrir dans un nouvel onglet</a>
                                    <a class="text-xs text-slate-300 hover:underline" href="/api/docs/${encodeURIComponent(item.vm_id)}/labs/${encodeURIComponent(item.lab_name)}/pdf?download=1">Telecharger</a>
                                </div>
                            </div>
                            <iframe src="/api/docs/${encodeURIComponent(item.vm_id)}/labs/${encodeURIComponent(item.lab_name)}/pdf" class="doc-pdf-frame w-full h-64 rounded-lg border border-slate-800 bg-slate-950" title="PDF ${escapeHtml(item.lab_name)}"></iframe>
                        </div>`
                    : '<p class="text-xs text-slate-500 mt-2">Aucun PDF associé.</p>';

                return `
                    <details id="${cardId}" data-doc-key="${escapeHtml(docKey)}" ${isOpen ? "open" : ""} class="group rounded-xl border border-slate-800 bg-slate-900/65 p-4">
                        <summary class="cursor-pointer list-none flex items-start justify-between gap-3">
                            <div>
                                <h4 class="text-sm font-semibold text-slate-100">${escapeHtml(item.lab_name)}</h4>
                                <p class="text-[11px] text-slate-500 mt-1">${updatedAt ? `Mis à jour le ${escapeHtml(updatedAt)}` : "Jamais modifié"}${updatedBy ? ` · par ${escapeHtml(updatedBy)}` : ""}</p>
                            </div>
                            <span class="text-slate-500 group-open:rotate-180 transition-transform">▾</span>
                        </summary>
                        <article data-doc-key="${escapeHtml(docKey)}" class="mt-3 space-y-3">
                            <div>
                                <p class="text-xs uppercase tracking-wide text-slate-400 mb-1">Texte</p>
                                ${textField}
                            </div>
                            <div>
                                <p class="text-xs uppercase tracking-wide text-slate-400 mb-1">Diagramme</p>
                                ${diagramField}
                            </div>
                            ${editorActions}
                            <div>
                                <p class="text-xs uppercase tracking-wide text-slate-400 mb-1">PDF</p>
                                ${pdfPreview}
                            </div>
                        </article>
                    </details>
                `;
            }).join("");

        return `
            <section class="rounded-xl border border-l-4 border-slate-800 border-l-cyan-600/60 bg-slate-950/70 p-4">
                <div class="mb-3">
                    <h3 class="text-sm font-semibold text-cyan-200">${escapeHtml(vm.vmName)} <span class="text-[11px] text-slate-500">(${escapeHtml(vm.vmId)})</span></h3>
                </div>
                <div class="space-y-3">${labsHtml}</div>
            </section>
        `;
    }).join("");

    void renderDocumentationDiagrams(items);
    initializeDocumentationEditors(items);
    initializeGeneralSectionEditor(latestGeneralDocSection);

    if (pendingDocumentationAnchor?.vmId && pendingDocumentationAnchor?.labName) {
        scrollToDocumentationAnchor(pendingDocumentationAnchor.vmId, pendingDocumentationAnchor.labName);
    }
}

async function refreshDocumentationPanel() {
    if (!currentUser) {
        return;
    }
    try {
        const response = await apiFetch("/api/docs");
        if (!response.ok) {
            const payloadText = await response.text();
            let detail = payloadText || `HTTP ${response.status}`;
            try {
                detail = JSON.parse(payloadText)?.detail || detail;
            } catch (_) {
            }
            throw new Error(detail);
        }
        const payload = await response.json();
        latestDocumentationItems = Array.isArray(payload.items) ? payload.items : [];
        latestGeneralDocSection = (payload.general_section && typeof payload.general_section === "object")
            ? payload.general_section
            : null;
        renderDocumentationPanel(latestDocumentationItems);
    } catch (error) {
        const container = document.getElementById("doc-labs");
        if (container) {
            container.innerHTML = `<div class="rounded-xl border border-rose-800/60 bg-rose-950/20 p-4 text-sm text-rose-200">Chargement documentation impossible: ${escapeHtml(error?.message || "Erreur inconnue")}</div>`;
        }
    }
}
