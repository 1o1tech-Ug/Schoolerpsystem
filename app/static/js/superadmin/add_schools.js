// ================= EDIT MODE STATE =================
let __editSchoolId = null;

window.__setEditSchoolId = function(id) {
    __editSchoolId        = id;
    window.__editModeActive = true;   // ← tells show() not to clear edit mode

    const btn     = document.getElementById("submitBtn");
    const heading = document.querySelector("#add .card:first-of-type h3");

    if (btn)     btn.innerText     = "Update School";
    if (heading) heading.innerText = "EDIT SCHOOL INFO";
};

window.__clearEditMode = function() {
    __editSchoolId          = null;
    window.__editModeActive = false;

    const btn     = document.getElementById("submitBtn");
    const heading = document.querySelector("#add .card:first-of-type h3");

    if (btn)     btn.innerText     = "Add School";
    if (heading) heading.innerText = "SCHOOL INFO";

    clearForm();
};

// ================= CLEAR FORM =================
function clearForm() {
    document.querySelectorAll("#add input").forEach(i  => i.value = "");
    document.querySelectorAll("#add select").forEach(s => s.selectedIndex = 0);
}

// ================= INIT =================
(function init() {
    const btn = document.getElementById("submitBtn");

    if (!btn) {
        alert("❌ submitBtn not found — check HTML");
        return;
    }

    btn.addEventListener("click", handleSchoolCreationPipeline);
})();

// ================= SUBMIT HANDLER =================
async function handleSchoolCreationPipeline() {
    const btn = document.getElementById("submitBtn");

    // ✅ Snapshot BEFORE anything clears it
    const isEditing = !!__editSchoolId;
    const editId    = __editSchoolId;

    const payload = {
        name:           document.getElementById("school_name")?.value.trim(),
        school_type:    document.getElementById("school_type")?.value,
        address:        document.getElementById("address")?.value.trim(),
        motto:          document.getElementById("motto")?.value.trim(),
        username:       document.getElementById("username")?.value.trim(),
        contact:        document.getElementById("contact")?.value.trim(),
        password:       document.getElementById("password")?.value,
        plan:           document.getElementById("plan")?.value,
        payment_status: document.getElementById("payment_status")?.value
    };

    // ================= VALIDATION =================
    const errors = [];

    if (!payload.name)     errors.push("School Name is required");
    if (!payload.address)  errors.push("Address is required");
    if (!payload.username) errors.push("Admin Username is required");

    if (!isEditing && !payload.password) {
        errors.push("Password is required");
    }

    if (errors.length) {
        alert("⚠️ Please fix the following:\n\n" + errors.map(e => "• " + e).join("\n"));
        return;
    }

    // ================= REQUEST =================
    btn.disabled  = true;
    btn.innerText = isEditing ? "Updating..." : "Processing...";

    try {
        let result;

        if (isEditing) {
            result = await window.secureRequest(`/api/schools/${editId}`, {
                method: "PUT",
                body: JSON.stringify(payload)
            });
        } else {
            result = await window.secureRequest("/api/schools", {
                method: "POST",
                body: JSON.stringify(payload)
            });
        }

        alert("✅ " + (result?.message || "School saved successfully"));

        window.__clearEditMode();

        if (window.loadAllSchoolsRegistry) {
            await window.loadAllSchoolsRegistry();
        }

        window.show("schools");

    } catch (err) {
        alert("❌ " + err.message);

    } finally {
        // ✅ Use snapshotted isEditing — __editSchoolId is null by now
        btn.disabled  = false;
        btn.innerText = isEditing ? "Update School" : "Add School";
    }
}