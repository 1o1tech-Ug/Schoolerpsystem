// ================= GLOBAL NAV =================
window.show = show;
window.toggleSidebar = toggleSidebar;

// ================= HTTP WRAPPER =================
async function secureRequest(url, options = {}) {
    const { headers: extraHeaders, ...restOptions } = options;

    const res = await fetch(url, {
        credentials: "include",
        ...restOptions,
        headers: {
            "Content-Type": "application/json",
            ...(extraHeaders || {})
        }
    });

    const isJson = res.headers.get("content-type")?.includes("application/json");
    const data   = isJson ? await res.json() : null;

    if (!res.ok) {
        throw new Error(data?.message || `Request failed (${res.status})`);
    }

    return data;
}

window.secureRequest = secureRequest;

// ================= SIDEBAR =================
function toggleSidebar() {
    const sidebar = document.getElementById("sidebar");
    const main    = document.getElementById("main");

    if (!sidebar || !main) return;

    if (window.innerWidth <= 900) {
        sidebar.classList.toggle("show");
    } else {
        sidebar.classList.toggle("hide");
        main.classList.toggle("expand");
    }
}

// ================= PAGE SWITCH =================
function show(id) {
    document.querySelectorAll(".section").forEach(s => s.classList.remove("active"));

    const el = document.getElementById(id);
    if (el) el.classList.add("active");

    document.querySelectorAll(".menu a").forEach(a => a.classList.remove("active"));

    const active = document.querySelector(`.menu a[onclick="show('${id}')"]`);
    if (active) active.classList.add("active");

    // Only clear edit mode when navigating TO add from menu, not from code
    if (id === "add" && !window.__editModeActive) {
        if (window.__clearEditMode) window.__clearEditMode();
    }

    if (window.innerWidth <= 900) {
        document.getElementById("sidebar")?.classList.remove("show");
    }

    // ================= LAZY SECTION LOADERS =================
    if (id === "schools"       && window.loadSchools)       window.loadSchools();
    if (id === "notifications" && window.loadNotifications) window.loadNotifications();
    if (id === "settings"      && window.loadBlacklist)     window.loadBlacklist();
}