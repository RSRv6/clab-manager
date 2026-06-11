function closeOverlayModal() {
    const existing = document.getElementById("overlay-modal");
    if (existing) {
        existing.remove();
    }
}

function showOverlayModal({ title, bodyHtml, tone = "slate", widthClass = "max-w-xl" }) {
    closeOverlayModal();

    const overlay = document.createElement("div");
    overlay.id = "overlay-modal";
    overlay.className = "fixed inset-0 z-50 bg-black/70 backdrop-blur-sm flex items-center justify-center p-4";

    const toneClass = tone === "amber"
        ? "border-amber-700/60"
        : tone === "rose"
            ? "border-rose-700/60"
            : "border-slate-700";

    overlay.innerHTML = `
        <div class="w-full ${widthClass} rounded-xl border ${toneClass} bg-slate-900 shadow-2xl">
            <div class="flex items-start justify-between gap-4 p-4 border-b border-slate-700">
                <h3 class="text-base font-semibold text-slate-100">${escapeHtml(title)}</h3>
                <button type="button" data-modal-close="true" class="text-xs px-2 py-1 rounded border border-slate-600 hover:bg-slate-800">Fermer</button>
            </div>
            <div class="p-4">${bodyHtml}</div>
        </div>
    `;

    overlay.addEventListener("click", (event) => {
        const element = event.target;
        if (!(element instanceof HTMLElement)) {
            return;
        }
        if (element === overlay || element.closest("[data-modal-close='true']")) {
            closeOverlayModal();
        }
    });

    document.body.appendChild(overlay);
    return overlay;
}

function showToastMessage(title, message, isError = false) {
    showOverlayModal({
        title,
        tone: isError ? "rose" : "slate",
        bodyHtml: `<p class="text-sm text-slate-300 leading-6">${escapeHtml(message)}</p>`,
    });
}
