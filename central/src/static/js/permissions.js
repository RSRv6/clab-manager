function canOperateLab(lab) {
    if (!currentUser) {
        return false;
    }
    if (isAdmin()) {
        return true;
    }
    const ownerUsername = String(lab?.reservation?.owner_username || "").toLowerCase();
    return Boolean(ownerUsername) && ownerUsername === String(currentUser.username || "").toLowerCase();
}

function canReleaseReservation(lab) {
    return canOperateLab(lab);
}

function canRedeployLab() {
    return isAdmin() || isGroupAdmin();
}

function canSetDefaultConfig() {
    return isAdmin() || isGroupAdmin();
}

function confirmDangerActionTwice(actionLabel, labName) {
    const first = window.prompt(
        `${actionLabel} du LAB ${labName}.\n\nValidation 1/2: tapez OUI pour continuer (NON par défaut).`,
        "",
    );
    if (String(first || "").trim().toUpperCase() !== "OUI") {
        return false;
    }

    const secondExpected = String(actionLabel || "").trim().toUpperCase();
    const second = window.prompt(
        `Validation 2/2: tapez exactement ${secondExpected} pour confirmer.`,
        "",
    );
    return String(second || "").trim().toUpperCase() === secondExpected;
}
