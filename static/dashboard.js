// Shared utilities for VPS Dashboard

/**
 * Show content in an output box element.
 * @param {HTMLElement} el
 * @param {string} content
 * @param {boolean} success
 */
function showOutput(el, content, success) {
    el.textContent = content || "(no output)";
    el.className = "output-box " + (success ? "ok" : "err");
    el.style.display = "block";
    el.scrollTop = el.scrollHeight;
}

/**
 * POST to an API action endpoint, show result in outputEl.
 * Disables btn while running.
 */
async function postAction(url, outputEl, btn) {
    const origHTML = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> Running\u2026';
    if (outputEl) outputEl.style.display = "none";

    try {
        const res = await fetch(url, { method: "POST" });
        const data = await res.json();
        if (outputEl) showOutput(outputEl, data.output || data.detail, data.success !== false);
        return data;
    } catch (err) {
        if (outputEl) showOutput(outputEl, "Request failed: " + err.message, false);
        return null;
    } finally {
        btn.disabled = false;
        btn.innerHTML = origHTML;
    }
}

/**
 * Copy text to clipboard. Briefly flashes the element green.
 */
function copyText(text, el) {
    navigator.clipboard.writeText(text).then(() => {
        el.classList.add("copied");
        const hint = el.querySelector(".cheat-copy-hint");
        if (hint) { const prev = hint.textContent; hint.textContent = "copied!"; setTimeout(() => hint.textContent = prev, 1200); }
        setTimeout(() => el.classList.remove("copied"), 1200);
    });
}

/**
 * Build a status badge element from an API status object.
 */
function statusBadge(svc) {
    const active = svc.service_active;
    const reachable = svc.port_reachable;

    let cls, label;
    if (active === "active")         { cls = "s-active";   label = "active"; }
    else if (active === "failed")    { cls = "s-failed";   label = "failed"; }
    else if (active === "inactive")  { cls = "s-inactive"; label = "inactive"; }
    else if (reachable === true)     { cls = "s-active";   label = "reachable"; }
    else if (reachable === false)    { cls = "s-failed";   label = "down"; }
    else                             { cls = "s-unknown";  label = "unknown"; }

    const portLine = svc.port
        ? `<span class="text-muted" style="font-size:0.73rem">port ${svc.port}: ${reachable ? "reachable" : reachable === false ? "unreachable" : "—"}</span>`
        : "";

    return `<span class="status-badge ${cls}"><span class="dot"></span>${label}</span> ${portLine}`;
}
