function renderAdminUsers(items, auditByUser = new Map()) {
    const container = document.getElementById("admin-users");
    const countBadge = document.getElementById("admin-users-count");
    const groupsCountBadge = document.getElementById("admin-groups-count");
    const adminsCountBadge = document.getElementById("admin-admins-count");
    if (!container) {
        return;
    }

    if (!Array.isArray(items) || !items.length) {
        container.innerHTML = '<p class="text-xs text-slate-500">Aucun utilisateur.</p>';
        if (countBadge) countBadge.textContent = "0";
        if (groupsCountBadge) groupsCountBadge.textContent = "0";
        if (adminsCountBadge) adminsCountBadge.textContent = "0";
        return;
    }

    const userCards = new Map();
    const admins = [];
    const groupBuckets = new Map();
    const groupOrder = [];

    for (const group of latestAdminGroups || []) {
        const name = String(group?.name || "").trim();
        if (!name || groupBuckets.has(name)) {
            continue;
        }
        groupBuckets.set(name, []);
        groupOrder.push(name);
    }

    const groupsByMember = new Map();
    const groupDetailsByName = new Map();
    for (const group of latestAdminGroups || []) {
        const groupName = String(group?.name || "").trim();
        if (!groupName) {
            continue;
        }
        groupDetailsByName.set(groupName, group);
        const members = Array.isArray(group?.member_details)
            ? group.member_details.map((member) => String(member?.username || ""))
            : Array.isArray(group?.members)
                ? group.members.map((member) => String(member || ""))
                : [];
        for (const username of members) {
            const key = username.trim().toLowerCase();
            if (!key) {
                continue;
            }
            if (!groupsByMember.has(key)) {
                groupsByMember.set(key, []);
            }
            groupsByMember.get(key).push(groupName);
        }
    }

    const renderUserCard = (user) => {
        const isSelf = isCurrentUser(user.username);
        const canManageThisUser = canManageUserInUi(user) && !isSelf;
        const isGroupAdminRole = String(user.role || "").toLowerCase() === "group-admin";
        const displayNameClass = isGroupAdminRole ? "text-amber-200 font-extrabold" : "text-slate-100";
        const usernameLineClass = isGroupAdminRole ? "text-amber-300/90" : "text-slate-400";
        const selfProtectionNote = isSelf
            ? '<p class="text-[11px] mt-2 text-slate-500">Votre compte admin ne peut pas être désactivé, rétrogradé ou supprimé depuis l’interface.</p>'
            : "";
        const groupsLabel = Array.isArray(user.groups) && user.groups.length
            ? `<p class="text-[11px] mt-1 text-cyan-300">Groupes: ${escapeHtml(user.groups.join(", "))}</p>`
            : '<p class="text-[11px] mt-1 text-slate-500">Aucun groupe assigné</p>';

        const usernameKey = String(user.username || "");
        const userAuditEvents = auditByUser.get(usernameKey) || [];
        const userAuditButton = userAuditEvents.length
            ? `<button type="button" data-action="open-user-audit-modal" data-username="${escapeHtml(usernameKey)}" class="text-xs px-2 py-0.5 rounded border border-fuchsia-700 text-fuchsia-200 hover:bg-fuchsia-900/30">Logs (${userAuditEvents.length})</button>`
            : "";

        const roleClass = user.role === "admin" ? "border-fuchsia-700 text-fuchsia-200" : user.role === "group-admin" ? "border-amber-700 text-amber-200" : "border-cyan-700 text-cyan-200";
        const groupsInline = Array.isArray(user.groups) && user.groups.length
            ? `<span class="text-cyan-300">${escapeHtml(user.groups.join(", "))}</span>`
            : `<span class="text-slate-500">sans groupe</span>`;
        const statusInline = `<span class="${user.active ? "text-emerald-300" : "text-amber-300"}">${user.active ? "Actif" : "Désactivé"}${user.must_change_password ? " · reset demandé" : ""}</span>`;

        return `
        <div class="rounded-lg border border-slate-800 bg-slate-900/70 px-3 py-2 flex items-center gap-3 justify-between">
            <div class="min-w-0">
                <p class="text-sm font-semibold ${displayNameClass} truncate">${escapeHtml(user.full_name || user.username)}</p>
                <p class="text-[11px] ${usernameLineClass} mt-0.5">${escapeHtml(user.username)} · ${escapeHtml(user.email)} · ${statusInline} · ${groupsInline}</p>
                ${selfProtectionNote}
            </div>
            <div class="flex items-center gap-1.5 shrink-0 flex-wrap justify-end">
                <span class="text-[11px] rounded-full border px-2 py-0.5 ${roleClass}">${escapeHtml(user.role)}</span>
                ${isAdmin() ? `<button type="button" data-action="admin-edit-user" data-username="${escapeHtml(user.username)}" data-full-name="${escapeHtml(user.full_name)}" data-email="${escapeHtml(user.email)}" data-role="${escapeHtml(user.role)}" data-active="${user.active ? "true" : "false"}" class="text-xs px-2 py-0.5 rounded border border-slate-600 text-slate-200 hover:bg-slate-800">Éditer</button>` : ""}
                ${(isAdmin() || canManageThisUser) ? `<button type="button" data-action="admin-reset-password" data-username="${escapeHtml(user.username)}" class="text-xs px-2 py-0.5 rounded border border-amber-700 text-amber-200 hover:bg-amber-900/30">Reset MDP</button>` : ""}
                ${isAdmin() && !isSelf ? `<button type="button" data-action="admin-toggle-user" data-username="${escapeHtml(user.username)}" data-active="${user.active ? "true" : "false"}" class="text-xs px-2 py-0.5 rounded border ${user.active ? "border-amber-700 text-amber-200 hover:bg-amber-900/30" : "border-emerald-700 text-emerald-200 hover:bg-emerald-900/30"}">${user.active ? "Désactiver" : "Activer"}</button>` : ""}
                ${canManageThisUser ? `<button type="button" data-action="admin-delete-user" data-username="${escapeHtml(user.username)}" class="text-xs px-2 py-0.5 rounded border border-rose-700 text-rose-200 hover:bg-rose-900/30">Supprimer</button>` : ""}
                ${userAuditButton}
            </div>
        </div>
    `;
    };

    const nonAdminUsers = [];
    for (const user of items) {
        const role = String(user?.role || "").toLowerCase();
        if (role === "admin") {
            admins.push(user);
            continue;
        }
        nonAdminUsers.push(user);
    }

    if (countBadge) countBadge.textContent = String(nonAdminUsers.length);
    if (adminsCountBadge) adminsCountBadge.textContent = String(admins.length);

    for (const user of nonAdminUsers) {
        const username = String(user?.username || "").trim();
        const userGroupList = Array.isArray(user?.groups)
            ? user.groups.map((groupName) => String(groupName || "").trim()).filter(Boolean)
            : [];
        const membershipGroups = groupsByMember.get(username.toLowerCase()) || [];
        const candidateGroups = userGroupList.length ? userGroupList : membershipGroups;
        const primaryGroup = candidateGroups[0] || "Sans groupe";

        if (!groupBuckets.has(primaryGroup)) {
            groupBuckets.set(primaryGroup, []);
            groupOrder.push(primaryGroup);
        }
        groupBuckets.get(primaryGroup).push(user);
    }

    const visibleGroups = groupOrder.filter((groupName) => {
        const members = groupBuckets.get(groupName) || [];
        return members.length > 0;
    });
    if (groupsCountBadge) groupsCountBadge.textContent = String(visibleGroups.length);

    const groupsHtml = visibleGroups.length
        ? visibleGroups.map((groupName) => {
            const members = groupBuckets.get(groupName) || [];
            const groupDetails = groupDetailsByName.get(groupName) || null;
            const groupDescription = String(groupDetails?.description || "").trim();
            const vmLabel = Array.isArray(groupDetails?.vm_ids) && groupDetails.vm_ids.length
                ? groupDetails.vm_ids.join(", ")
                : "Aucune VM complète";
            const labScopeLabel = Array.isArray(groupDetails?.lab_permissions) && groupDetails.lab_permissions.length
                ? groupDetails.lab_permissions
                    .map((item) => `${item.vm_id}: ${Array.isArray(item.labs) ? item.labs.join(", ") : ""}`)
                    .join(" | ")
                : "Aucune portée LAB spécifique";
            const groupActions = isAdmin() && groupDetails
                ? `<div class="flex items-center gap-2 mt-2">
                    <button type="button" data-action="admin-edit-group" data-group-name="${escapeHtml(groupName)}" class="text-xs px-2 py-1 rounded border border-slate-600 text-slate-200 hover:bg-slate-800">Éditer</button>
                    <button type="button" data-action="admin-delete-group" data-group-name="${escapeHtml(groupName)}" class="text-xs px-2 py-1 rounded border border-rose-700 text-rose-200 hover:bg-rose-900/30">Supprimer</button>
                </div>`
                : "";
            return `
                <details data-key="admin-group-${escapeHtml(groupName)}" class="rounded-lg border border-slate-700 bg-slate-950/50" open>
                    <summary class="flex items-center justify-between gap-2 px-3 py-2 cursor-pointer list-none select-none hover:bg-slate-800/30 rounded">
                        <span class="text-xs font-semibold text-cyan-200">${escapeHtml(groupName)}</span>
                        <span class="text-[11px] text-slate-400">${members.length} utilisateur${members.length !== 1 ? "s" : ""}</span>
                    </summary>
                    <div class="border-t border-slate-800 p-2 space-y-2">
                        ${groupDescription ? `<p class="text-[11px] text-slate-400">${escapeHtml(groupDescription)}</p>` : ""}
                        ${groupDetails ? `<p class="text-[11px] text-emerald-300">VMs: ${escapeHtml(vmLabel)}</p>` : ""}
                        ${groupDetails ? `<p class="text-[11px] text-amber-300">LABs: ${escapeHtml(labScopeLabel)}</p>` : ""}
                        ${groupActions}
                        ${members.map((user) => renderUserCard(user)).join("")}
                    </div>
                </details>
            `;
        }).join("")
        : '<p class="text-xs text-slate-500">Aucun groupe avec membres.</p>';

    const adminsHtml = admins.length
        ? admins.map((user) => renderUserCard(user)).join("")
        : '<p class="text-xs text-slate-500">Aucun admin.</p>';

    container.innerHTML = `
        <div class="space-y-3">
            ${groupsHtml}
            <div class="rounded-lg border border-fuchsia-700/40 bg-slate-950/50 p-2">
                <div class="flex items-center justify-between gap-2 mb-2">
                    <p class="text-xs font-semibold uppercase tracking-wide text-fuchsia-200">Admins</p>
                    <span class="text-[11px] rounded-full border border-fuchsia-700/60 px-2 py-0.5 text-fuchsia-200">${admins.length}</span>
                </div>
                <div class="space-y-2">${adminsHtml}</div>
            </div>
        </div>
    `;
}

function collectGroupInventory(currentGroupName = "") {
    const normalizedCurrentGroup = String(currentGroupName || "").toLowerCase();
    const assignedGroupByUser = new Map();

    for (const group of latestAdminGroups || []) {
        const groupName = String(group?.name || "").toLowerCase();
        for (const member of group?.members || []) {
            const username = String(member || "").toLowerCase();
            if (!username) {
                continue;
            }
            if (groupName !== normalizedCurrentGroup) {
                assignedGroupByUser.set(username, group.name || "");
            }
        }
    }

    const users = (latestAdminUsers || []).map((user) => {
        const username = String(user?.username || "").toLowerCase();
        return {
            username,
            label: `${user?.full_name || username} (${username})`,
            assignedGroup: assignedGroupByUser.get(username) || "",
        };
    });

    const vmMap = new Map();
    for (const item of latestStateItems || []) {
        const vmId = String(item?.id || "").trim();
        if (!vmId) {
            continue;
        }
        const vm = vmMap.get(vmId) || {
            id: vmId,
            label: item?.name ? `${item.name} (${vmId})` : vmId,
            labs: new Set(),
        };
        for (const lab of item?.labs || []) {
            const labName = String(lab?.name || "").trim();
            if (labName) {
                vm.labs.add(labName);
            }
        }
        vmMap.set(vmId, vm);
    }

    const vms = Array.from(vmMap.values())
        .map((vm) => ({
            id: vm.id,
            label: vm.label,
            labs: Array.from(vm.labs).sort((left, right) => left.localeCompare(right)),
        }))
        .sort((left, right) => left.id.localeCompare(right.id));

    return { users, vms };
}
