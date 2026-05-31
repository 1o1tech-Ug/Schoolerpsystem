document.addEventListener("DOMContentLoaded", () => {
    loadAllSchoolsRegistry();
});

async function loadAllSchoolsRegistry() {
    const tbody = document.getElementById("schoolsTable");
    if (!tbody) return;

    try {
        tbody.innerHTML = `<tr><td colspan="6" style="text-align:center; color:#94a3b8; padding:24px;">Loading...</td></tr>`;

        const schools = await window.secureRequest("/api/schools");

        if (!schools || !schools.length) {
            tbody.innerHTML = `<tr><td colspan="6" style="text-align:center; color:#94a3b8; padding:24px;">No schools found</td></tr>`;
            return;
        }

        tbody.innerHTML = "";

        schools.forEach(s => {
            const tr = document.createElement("tr");

            tr.innerHTML = `
                <td style="font-weight:600; color:var(--text-muted);">#${s.id}</td>
                <td style="font-weight:600; color:#0f172a;"></td>
                <td></td>
                <td></td>
                <td></td>
                <td>
                    <button class="edit" style="margin-right:4px;">Edit</button>
                    <button class="delete">Delete</button>
                </td>
            `;

            // Set via textContent to prevent XSS
            tr.cells[1].textContent = s.name        || "—";
            tr.cells[2].textContent = s.school_type || "Secondary";
            tr.cells[3].textContent = s.status      || "Active";
            tr.cells[4].textContent = s.plan        || "—";

            tr.querySelector(".edit").addEventListener("click",   () => triggerSchoolEdit(s.id));
            tr.querySelector(".delete").addEventListener("click", () => triggerSchoolDeletion(s.id));

            tbody.appendChild(tr);
        });

    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="6" style="text-align:center; color:#ef4444; padding:24px;">⚠️ ${err.message}</td></tr>`;
    }
}

// ================= DELETE =================
async function triggerSchoolDeletion(id) {
    if (!confirm(`⚠️ Are you sure you want to delete school #${id}?\nThis action cannot be undone.`)) return;

    try {
        const result = await window.secureRequest(`/api/schools/${id}`, {
            method: "DELETE"
        });

        alert("✅ " + (result?.message || "School deleted successfully"));
        await loadAllSchoolsRegistry();

    } catch (err) {
        alert("❌ " + err.message);
    }
}

// ================= EDIT =================
async function triggerSchoolEdit(id) {
    try {
        const s = await window.secureRequest(`/api/schools/${id}`);

        // Navigate to add/edit page first
        window.show("add");

        // Populate all fields
        document.getElementById("school_name").value        = s.name           || "";
        document.getElementById("school_type").value        = s.school_type    || "";
        document.getElementById("address").value            = s.address        || "";
        document.getElementById("motto").value              = s.motto          || "";
        document.getElementById("username").value           = s.username       || "";
        document.getElementById("contact").value            = s.contact        || "";
        document.getElementById("plan").value               = s.plan           || "Basic";
        document.getElementById("payment_status").value     = s.payment_status || "Paid";
        document.getElementById("password").value           = "";  // never pre-fill password

        // Activate edit mode
        window.__setEditSchoolId(id);

    } catch (err) {
        alert("❌ Failed to load school: " + err.message);
    }
}

window.loadAllSchoolsRegistry = loadAllSchoolsRegistry;
window.triggerSchoolEdit      = triggerSchoolEdit;
window.triggerSchoolDeletion  = triggerSchoolDeletion;