// =========================================================
// SETTINGS — BLACKLIST
// =========================================================

const API = "";  // base URL if needed

// ── Load blacklisted schools on section open ──────────────
function loadBlacklist() {
    fetch(`${API}/api/blacklist`, {
        headers: { Authorization: `Bearer ${localStorage.getItem("token")}` }
    })
    .then(r => r.json())
    .then(data => renderBlacklist(data))
    .catch(() => showToast("Failed to load blacklist", "error"));
}

function renderBlacklist(data) {
    const tbody = document.getElementById("blacklistTable");

    if (!data.length) {
        tbody.innerHTML = `<tr><td colspan="5" style="text-align:center; color:#94a3b8; padding:28px;">No blacklisted schools</td></tr>`;
        return;
    }

    tbody.innerHTML = data.map(e => `
        <tr>
            <td>#${e.school_id}</td>
            <td>${e.name}</td>
            <td>${e.reason}</td>
            <td>${e.date}</td>
            <td>
                <button class="edit"   style="margin-right:6px;"
                    onclick="unblacklistSchool(${e.school_id})">Unblacklist</button>
                <button class="delete"
                    onclick="unblacklistSchool(${e.school_id})">Delete</button>
            </td>
        </tr>
    `).join("");
}

// ── Blacklist a school ────────────────────────────────────
document.getElementById("blacklistBtn").addEventListener("click", () => {
    const id     = document.getElementById("blacklistSchoolId").value.trim();
    const reason = document.getElementById("blacklistReason")?.value.trim()
                   || "No reason provided";

    if (!id) {
        showToast("Please enter a School ID", "error");
        return;
    }

    if (!confirm(`Blacklist school #${id}? All users will be suspended immediately.`)) return;

    fetch(`${API}/api/schools/${id}/blacklist`, {
        method:  "POST",
        headers: {
            "Content-Type":  "application/json",
            Authorization: `Bearer ${localStorage.getItem("token")}`
        },
        body: JSON.stringify({ reason })
    })
    .then(r => r.json())
    .then(data => {
        showToast(data.message);
        document.getElementById("blacklistSchoolId").value = "";
        loadBlacklist();
    })
    .catch(() => showToast("Request failed", "error"));
});

// ── Unblacklist a school ──────────────────────────────────
function unblacklistSchool(schoolId) {
    if (!confirm(`Restore school #${schoolId} and reactivate all its users?`)) return;

    fetch(`${API}/api/schools/${schoolId}/blacklist`, {
        method:  "DELETE",
        headers: { Authorization: `Bearer ${localStorage.getItem("token")}` }
    })
    .then(r => r.json())
    .then(data => {
        showToast(data.message);
        loadBlacklist();
    })
    .catch(() => showToast("Request failed", "error"));
}

// ── Auto-load when settings section is opened ─────────────
// Call loadBlacklist() inside your show() function when section === 'settings'