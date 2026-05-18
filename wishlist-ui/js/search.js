// search.js - Search Stalk rendering + multi-filter search (AND logic)

function decodeB64Json(b64) {
    // Match clone_sniper decode behavior (handles unicode safely)
    const decoded = decodeURIComponent(escape(atob(b64)));
    return JSON.parse(decoded);
}

function escapeHtml(s) {
    return String(s)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
}

function hydrateLogs() {
    const logs = document.querySelectorAll(".log-item");

    logs.forEach((log) => {
        if (log.dataset.rendered === "true") return;

        try {
            const hit = decodeB64Json(log.dataset.hit);

            const time = hit.time || "";
            const user = hit.user || "";
            const mode = hit.mode || "";
            const trigger = hit.trigger || "";
            const query = hit.query || "";

            const html =
                `<span class="log-time">${escapeHtml(time)}</span> ` +
                `<span class="log-msg">[` +
                `${escapeHtml(mode)}] ` +
                `'${escapeHtml(user)}': ` +
                `'${escapeHtml(query)}'</span>`;

            log.innerHTML = html;
            log.dataset.rendered = "true";
        } catch (e) {
            log.innerHTML = "<b>Render error</b>";
            log.dataset.rendered = "true";
        }
    });
}

function applyMultiFilter() {
    const inputs = Array.from(document.querySelectorAll(".search-input"));
    const terms = inputs
        .map((i) => (i.value || "").toLowerCase().trim())
        .filter((t) => t.length > 0);

    const logs = document.querySelectorAll(".log-item");

    // If all inputs empty, show everything
    if (terms.length === 0) {
        logs.forEach((log) => {
            log.style.display = "";
        });
        return;
    }

    logs.forEach((log) => {
        const text = (log.textContent || "").toLowerCase();
        const ok = terms.every((t) => text.includes(t)); // AND logic
        log.style.display = ok ? "" : "none";
    });
}

// Initial render + initial filter
hydrateLogs();
applyMultiFilter();

// Bind all three inputs
document.querySelectorAll(".search-input").forEach((input) => {
    input.addEventListener("input", applyMultiFilter);
});
