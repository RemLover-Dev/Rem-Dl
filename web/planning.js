// ============================================================
// Download Planning List — persistent planned-download entries,
// bulk Telegram-paste importer, per-entry "Open on site".
// Loaded after script.js; relies on its top-level helpers
// (escapeHtml, cleanTagDisplay, siteLabel, showToast,
// toggleDropdownCheck) and .custom-confirm-overlay CSS.
// ============================================================
let planningEntries = [];
let planningSites = [];
let planningDefaultSite = "gelbooru";
let planningRenderCap = 50;
let planBulkModal = null;
let planBulkRows = [];
const planSelSites = new Set(); // empty = all sources

// rating options per site for the add/edit + bulk-preview selects;
// entries with a value outside these become a "Custom:" option
const PLAN_RATING_OPTIONS = {
    dan: [["rating:g", "Safe"], ["rating:s", "Sensitive"], ["rating:q", "Questionable"], ["rating:e", "NSFW"]],
    gelbooru: [["rating:g", "Safe"], ["rating:s", "Sensitive"], ["rating:q", "Questionable"], ["rating:e", "NSFW"]],
    gsbooru: [["rating:g", "Safe"], ["rating:s", "Sensitive"], ["rating:q", "Questionable"]],
    kona: [["rating:s", "Safe"], ["rating:q", "Questionable"], ["rating:e", "NSFW"]],
    yande: [["rating:s", "Safe"], ["rating:q", "Questionable"], ["rating:e", "NSFW"]],
    sankaku: [["rating:s", "Safe"], ["rating:q", "Questionable"], ["rating:e", "NSFW"]],
    pixiv: [["rating:g", "Safe"], ["rating:r18", "R-18"], ["rating:r18g", "R-18g"]],
    nekosapi: [["safe", "Safe"], ["suggestive", "Suggestive"], ["borderline", "Borderline"], ["explicit", "Explicit"]],
    nekosia: [["safe", "Safe"], ["suggestive", "Suggestive"]],
};

function planFetch(url, body) {
    return fetch(url, body === undefined ? undefined : {
        method: body.method || "POST",
        headers: { "Content-Type": "application/json" },
        body: body.json
    }).then(async r => {
        const d = await r.json().catch(() => ({}));
        if (!r.ok) throw Object.assign(new Error(d.error || r.statusText), { data: d, status: r.status });
        return d;
    });
}

function planToggleMenu(id) {
    const menu = document.getElementById(id);
    document.querySelectorAll('.gallery-dropdown-menu.open').forEach(m => { if (m.id !== id) m.classList.remove('open'); });
    if (menu) menu.classList.toggle('open');
}

function planModal(inner, escHandler) {
    const ov = document.createElement("div");
    ov.className = "custom-confirm-overlay";
    ov.style.alignItems = "flex-start";
    ov.style.overflowY = "auto";
    const panel = document.createElement("div");
    panel.className = "custom-confirm-box";
    panel.style.cssText = "width:min(760px,94vw); max-height:88vh; overflow:auto; display:flex; flex-direction:column; gap:12px; text-align:left;";
    panel.innerHTML = inner;
    ov.appendChild(panel);
    document.body.appendChild(ov);
    const close = () => { document.removeEventListener("keydown", escHandler, true); ov.remove(); };
    ov.addEventListener("click", e => { if (e.target === ov) close(); });
    panel.querySelectorAll("[data-close]").forEach(b => b.addEventListener("click", close));
    document.addEventListener("keydown", escHandler, true);
    // selects injected into this modal post-load still need the app's
    // custom-select treatment
    enhanceAllSelects();
    return { ov, panel, close };
}

function planConfirm(message, okLabel) {
    return new Promise(resolve => {
        let handlers;
        const { close } = planModal(
            `<div style="font-size:14px;">${escapeHtml(message)}</div>
             <div style="display:flex; justify-content:flex-end; gap:10px;">
                 <button class="action-btn" data-close>Cancel</button>
                 <button class="action-btn stop-btn" id="planConfirmOk">${escapeHtml(okLabel || "Delete")}</button>
             </div>`,
            e => { if (e.key === "Escape") { handlers.close(); resolve(false); } });
        handlers = { close };
        document.getElementById("planConfirmOk").addEventListener("click", () => { close(); resolve(true); });
    });
}

function planChip(text) {
    return `<span style="display:inline-flex; align-items:center; padding:1px 8px; border-radius:8px; font-size:11px; border:1px solid var(--border-color);">${escapeHtml(text)}</span>`;
}

function planWarnChip(text) {
    return `<span style="display:inline-flex; align-items:center; padding:1px 8px; border-radius:8px; font-size:11px; border:1px solid #e8a13d; color:#e8a13d;">${escapeHtml(text)}</span>`;
}

function planFormatDate(ts) {
    if (!ts) return "";
    try { return new Date(ts * 1000).toLocaleString(); } catch { return ""; }
}

function planRatingBadge(entry) {
    if (!entry.rating) return "";
    const opts = (PLAN_RATING_OPTIONS[entry.site] || []).find(o => o[0] === entry.rating);
    return planWarnChip("Rating: " + (opts ? opts[1] : entry.rating.replace(/^rating:/, "")));
}

function siteBadge(site) {
    return `<span style="display:inline-block; padding:1px 8px; border-radius:8px; font-size:11px; background:var(--input-bg);">${escapeHtml(siteLabel(site))}</span>`;
}

function planTagsDisplay(tags) {
    let html = "";
    const negative = [];
    String(tags || "").trim().split(/\s+/).filter(Boolean).forEach(t => {
        if (t.startsWith("-")) negative.push(t);
        else html += `<span>${escapeHtml(cleanTagDisplay(t))}</span> `;
    });
    if (negative.length) html += `<span style="color:#e8a13d;">${escapeHtml(negative.join(" "))}</span>`;
    return html || escapeHtml(tags || "");
}

function renderPlanSourceDropdown() {
    const menu = document.getElementById("planSrcDropdown");
    if (!menu) return;
    const all = !planSelSites.size || planningSites.every(s => planSelSites.has(s));
    menu.innerHTML =
        `<div class="dd-item" onclick="toggleDropdownCheck(this, event); planOnSourceChange()"><span>All sources</span><input type="checkbox" value="" ${all ? "checked" : ""}></div>` +
        planningSites.map(s =>
            `<div class="dd-item" onclick="toggleDropdownCheck(this, event); planOnSourceChange()"><span>${escapeHtml(siteLabel(s))}</span><input type="checkbox" data-plan-src="${s}" value="${escapeHtml(s)}" ${planSelSites.has(s) ? "checked" : ""}></div>`
        ).join("");
    const btn = document.getElementById("planSrcBtn");
    if (btn) {
        const n = planSelSites.size;
        btn.textContent = all ? "All sources ▾"
            : n === 1 ? siteLabel([...planSelSites][0]) + " ▾"
            : n + " sources ▾";
    }
}

function planOnSourceChange() {
    planSelSites.clear();
    document.querySelectorAll('#planSrcDropdown input[data-plan-src]').forEach(cb => {
        if (cb.checked) planSelSites.add(cb.value);
    });
    renderPlanSourceDropdown();
    renderPlanning();
}

function planFilterList() {
    const terms = [];
    const statusEl = document.getElementById("planStatusFilter");
    const searchEl = document.getElementById("planSearch");
    if (searchEl) terms.push(...searchEl.value.toLowerCase().trim().split(/\s+/).filter(Boolean));
    return planningEntries.filter(e => {
        if (planSelSites.size && !planSelSites.has(e.site)) return false;
        if (statusEl && statusEl.value === "completed" && !e.completed) return false;
        if (statusEl && statusEl.value === "pending" && e.completed) return false;
        if (!terms.length) return true;
        const hay = [siteLabel(e.site), e.tags || "", e.note || "", e.rating || ""].join(" ").toLowerCase();
        return terms.every(t => hay.includes(t));
    });
}

function getPlanEntry(id) {
    return planningEntries.find(e => e.id === id) || null;
}

function renderPlanning() {
    const box = document.getElementById("planList");
    if (!box) return;
    const shownAll = planFilterList();
    const shown = shownAll.slice(0, planningRenderCap);
    const hidden = shownAll.length - shown.length;
    const countEl = document.getElementById("planCount");
    if (countEl) {
        const pending = planningEntries.filter(e => !e.completed).length;
        countEl.textContent = `${pending} pending · ${planningEntries.length} total`;
    }
    const moreBtn = document.getElementById("planMoreBtn");
    if (moreBtn) moreBtn.style.display = hidden > 0 ? "inline-flex" : "none";
    if (!planningEntries.length) {
        box.innerHTML = `<div style="opacity:0.6; font-size:13px;">Nothing planned yet — add entries above or paste a chat dump with "Bulk import".</div>`;
        return;
    }
    box.innerHTML = shown.map(e => {
        const meta = [];
        if (e.note) meta.push("note: " + e.note);
        const hoverMeta = e.completed
            ? ((e.completed_at && `completed ${planFormatDate(e.completed_at)}`) || "")
            : ((e.created_at && `added ${planFormatDate(e.created_at)}`) || "");
        return `<div class="plan-item" data-id="${escapeHtml(e.id)}" style="display:flex; gap:10px; align-items:center; padding:10px 12px; border:1px solid var(--border-color); border-radius:10px; flex-wrap:wrap; ${e.completed ? "opacity:0.55;" : ""}">
            <button class="action-btn plan-toggle" data-act="toggle" title="${e.completed ? "Mark as pending" : "Mark as completed"}" style="padding: 4px 8px; font-size: 12px; background: transparent; border: 1px solid transparent; box-shadow: 0 0 0 1px ${e.completed ? "var(--accent-color)" : "var(--border-color)"}; color: var(--text-color);">${e.completed ? "✓" : "○"}</button>
            <div style="flex:1; min-width:260px;">
                <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
                    ${siteBadge(e.site)}
                    <span style="display:flex; gap:6px; flex-wrap:wrap;">${planTagsDisplay(e.tags)}</span>
                    ${planRatingBadge(e)}
                </div>
                <div style="font-size:11px; opacity:0.65; margin-top:3px;">${escapeHtml(meta.join(" · "))}${meta.length && hoverMeta ? " · " : ""}<span class="plan-added">${escapeHtml(hoverMeta)}</span></div>
            </div>
            <div class="plan-actions" style="display:flex; gap:8px;">
                <button class="action-btn" data-act="open" title="Load into the ${escapeHtml(siteLabel(e.site))} tab" style="padding: 4px 8px; font-size: 12px; background: transparent; border: 1px solid transparent; box-shadow: 0 0 0 1px var(--border-color); color: var(--text-color);"><svg width="1em" height="1em" viewBox="0 0 24 24" fill="none" style="vertical-align:-0.125em;"><path fill="currentColor" d="M23.987 12a2.411 2.411 0 0 0 -0.814 -1.8L11.994 0.361a1.44 1.44 0 0 0 -1.9 2.162l8.637 7.6a0.25 0.25 0 0 1 -0.165 0.437H1.452a1.44 1.44 0 0 0 0 2.88h17.111a0.251 0.251 0 0 1 0.165 0.438l-8.637 7.6a1.44 1.44 0 1 0 1.9 2.161L23.172 13.8a2.409 2.409 0 0 0 0.815 -1.8Z"/></svg></button>
                <button class="action-btn" data-act="edit" title="Edit entry" style="padding: 4px 8px; font-size: 12px; background: transparent; border: 1px solid transparent; box-shadow: 0 0 0 1px var(--border-color); color: var(--text-color); display: inline-flex; align-items: center; justify-content: center;"><svg width="1em" height="1em" viewBox="0 0 24 24" fill="none"><path stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"/></svg></button>
                <button class="action-btn stop-btn" data-act="delete" title="Delete entry" style="padding: 4px 8px; font-size: 12px; display: inline-flex; align-items: center; justify-content: center;"><svg width="1em" height="1em" viewBox="0 0 24 24" fill="none"><path stroke="currentColor" stroke-width="2.5" stroke-linecap="round" d="M6 6l12 12M18 6L6 18"/></svg></button>
            </div>
        </div>`;
    }).join("") + (hidden
        ? `<div style="opacity:0.6; font-size:12px; margin-top:8px;">${hidden} more hidden — "Show all" below</div>`
        : "");
    box.querySelectorAll("[data-act]").forEach(btn => btn.addEventListener("click", () => {
        const act = btn.dataset.act;
        const id = btn.closest(".plan-item").dataset.id;
        if (act === "toggle") togglePlanDone(id);
        else if (act === "open") queuePlanEntry(id);
        else if (act === "edit") openPlanEntryModal(getPlanEntry(id) || undefined);
        else if (act === "delete") deletePlanEntry(id);
    }));
}

function planShowMore() { planningRenderCap = planningEntries.length + 1; renderPlanning(); }

async function loadPlanning() {
    try {
        const d = await planFetch("/api/planning");
        planningEntries = Array.isArray(d.entries) ? d.entries : [];
        if (Array.isArray(d.sites) && d.sites.length) planningSites = d.sites;
        planningDefaultSite = d.default_site || planningDefaultSite;
        renderPlanSourceDropdown();
        renderPlanning();
    } catch (err) {
        showToast("Could not load the planning list: " + err.message, { warn: true });
    }
}

async function togglePlanDone(id) {
    const entry = getPlanEntry(id);
    if (!entry) return;
    try {
        const d = await planFetch("/api/planning/update", { json: JSON.stringify({ id, completed: !entry.completed }) });
        const idx = planningEntries.findIndex(e => e.id === id);
        if (idx >= 0 && d.entry) planningEntries[idx] = d.entry;
        renderPlanning();
    } catch (err) {
        showToast(err.message, { warn: true });
    }
}

async function deletePlanEntry(id) {
    if (!await planConfirm("Delete this entry from the planning list?", "Delete")) return;
    try {
        await planFetch("/api/planning/remove", { json: JSON.stringify({ id }) });
        planningEntries = planningEntries.filter(e => e.id !== id);
        renderPlanning();
    } catch (err) {
        showToast(err.message, { warn: true });
    }
}

// arrow button: queue the entry on the matching site's worker (same as
// typing the tags there and pressing "Add to queue") — pills are only the
// staging step; addWorkerToQueue does the emit
const PLAN_QUEUE_ADD = {
    gelbooru: "addGelbooruTag", dan: "addDanTag", gsbooru: "addGsbooruTag",
    kona: "addKonaTag", yande: "addYandeTag", safe: "addSafeTag", sankaku: "addSankakuTag",
    nekosia: "addNekosiaTag", eshuushuu: "addEshuushuuTag", zero: "addZerochanTag"
};
const PLAN_QUEUE_INPUT = {
    gelbooru: "gelbooruTag", dan: "danTag", gsbooru: "gsbooruTag", kona: "konaTag",
    yande: "yandeTag", safe: "safeTag", sankaku: "sankakuTag", nekosia: "nekosiaTag",
    eshuushuu: "eshuushuuTag", zero: "zeroTag", rule34: "rule34TagInput"
};

function _planOpenTab(site) {
    const mapping = siteMap[site] || { tab: "Safe" };
    const btn = Array.from(document.querySelectorAll('.tab-btn')).find(el => (el.getAttribute('onclick') || '').includes("'" + mapping.tab + "'"));
    if (btn) openTab(mapping.tab, btn);
}

function queuePlanEntry(id) {
    const entry = getPlanEntry(id);
    if (!entry) return;
    const site = entry.site;
    let tags = String(entry.tags || "").trim();
    const addFn = PLAN_QUEUE_ADD[site];
    const inputId = PLAN_QUEUE_INPUT[site];
    if (!addFn || !inputId) { jumpToSite(site, tags, entry.rating || ""); addWorkerToQueue(site); return; }
    // saved AI exclusion lives in the tab's checkbox, never as a pill
    if (site === "gelbooru" || site === "sankaku") {
        const re = site === "gelbooru" ? /-ai[_ ]generated\b/gi : /-ai-created\b/gi;
        const box = document.getElementById(site === "gelbooru" ? "gelNoAI" : "sankakuExcludeAi");
        if (box && re.test(tags)) { box.checked = true; tags = tags.replace(re, "").trim(); }
    }
    if (site === "rule34") {
        tags.split(/\s+/).filter(Boolean).forEach(v => { if (!currentRule34Tags.includes(v)) currentRule34Tags.push(v); });
        renderRule34Tags();
    } else {
        const input = document.getElementById(inputId);
        if (!input) { jumpToSite(site, tags, entry.rating || ""); addWorkerToQueue(site); return; }
        input.value = tags;
        window[addFn]();
    }
    const rid = RATING_INPUT_BY_WORKER[site];
    if (rid && entry.rating) setSelectValue(document.getElementById(rid), entry.rating);
    _planOpenTab(site);
    addWorkerToQueue(site);
}

async function clearPlanningList(mode, msg) {
    if (!await planConfirm(msg, "Clear")) return;
    try {
        await planFetch("/api/planning/clear", { json: JSON.stringify({ mode }) });
        await loadPlanning();
    } catch (err) {
        showToast(err.message, { warn: true });
    }
}

function clearPlanningAll() {
    clearPlanningList("all", "Delete EVERY entry in the planning list? This cannot be undone.");
}
function clearPlanningCompleted() {
    clearPlanningList("completed", "Clear all completed entries? Pending ones stay.");
}
async function planningListDedupe() {
    if (!await planConfirm("Remove duplicate entries? One copy of each identical entry (site, tags, rating, note) stays.", "Remove duplicates")) return;
    try {
        const d = await planFetch("/api/planning/clear", { json: JSON.stringify({ mode: "duplicates" }) });
        showToast(d.removed ? `Removed ${d.removed} duplicate${d.removed === 1 ? "" : "s"}` : "Nothing removed");
        await loadPlanning();
    } catch (err) {
        showToast(err.message, { warn: true });
    }
}

// --- add / edit entry modal ---
function planRatingOptionsHtml(site, current) {
    const opts = (PLAN_RATING_OPTIONS[site] || []).slice();
    if (current && !opts.some(o => o[0] === current)) opts.push([current, "Custom: " + current.replace(/^rating:/, "")]);
    return `<option value="">All (no rating filter)</option>` +
        opts.map(o => `<option value="${escapeHtml(o[0])}" ${o[0] === current ? "selected" : ""}>${escapeHtml(o[1])}</option>`).join("");
}

function openPlanEntryModal(entry) {
    entry = entry && entry.id ? entry : null;
    const { panel, close } = planModal(
        `<h3 style="margin-bottom:0;">${entry ? "Edit entry" : "Add entry"}</h3>
         <form id="planEntryForm" style="display:flex; flex-direction:column; gap:10px;">
             <div style="display:flex; gap:10px; flex-wrap:wrap;">
                 <select id="planEntrySite" style="flex:0 1 200px; min-width:150px;">
                     ${planningSites.map(s => `<option value="${escapeHtml(s)}" ${(entry ? entry.site : planningDefaultSite) === s ? "selected" : ""}>${escapeHtml(siteLabel(s))}</option>`).join("")}
                 </select>
                 <select id="planEntryRating" style="flex:0 1 200px; min-width:130px;">${planRatingOptionsHtml(entry ? entry.site : planningDefaultSite, entry ? entry.rating || "" : "")}</select>
             </div>
             <label style="font-size:13px;">Tags — worker syntax; "-" tokens are downloaded exclusions</label>
             <input type="text" id="planEntryTags" style="flex:1;" placeholder="e.g. rem blue_hair -ai_generated" value="${escapeHtml(entry ? entry.tags || "" : "")}">
             <label style="font-size:13px;">Note (optional)</label>
             <input type="text" id="planEntryNote" style="flex:1;" placeholder="why this is on the list / source of the paste" value="${escapeHtml(entry ? entry.note || "" : "")}">
             <div style="display:flex; justify-content:flex-end; gap:10px; margin-top:6px;">
                 <button type="button" class="action-btn" data-close>Cancel</button>
                 <button type="submit" class="action-btn">Save</button>
             </div>
         </form>`,
        e => { if (e.key === "Escape") { e.preventDefault(); close(); } });
    const siteSel = panel.querySelector("#planEntrySite");
    siteSel.addEventListener("change", () => {
        panel.querySelector("#planEntryRating").innerHTML = planRatingOptionsHtml(siteSel.value, "");
    });
    panel.querySelector("#planEntryForm").addEventListener("submit", async e => {
        e.preventDefault();
        const payload = {
            site: siteSel.value,
            tags: panel.querySelector("#planEntryTags").value,
            rating: panel.querySelector("#planEntryRating").value,
            note: panel.querySelector("#planEntryNote").value,
        };
        if (!payload.site) { showToast("Pick a site", { warn: true }); return; }
        if (!payload.tags.trim()) { showToast("Tags are required", { warn: true }); return; }
        try {
            if (entry) {
                await planFetch("/api/planning/update", { json: JSON.stringify({ id: entry.id, ...payload }) });
                showToast("Entry updated");
            } else {
                await planFetch("/api/planning/add", { json: JSON.stringify(payload) });
                showToast("Entry added");
            }
            await loadPlanning();
            close();
        } catch (err) {
            showToast(err.status === 409 ? "An identical entry is already in the list" : err.message, { warn: true });
        }
    });
    panel.querySelector("#planEntryTags").focus();
}

// --- bulk import + normalize ---
function openPlanBulkModal() {
    let draft = "";
    try { draft = localStorage.getItem("planBulkDraft") || ""; } catch { /* storage unreachable */ }
    const { panel, close } = planModal(
        `<h3 style="margin-bottom:0;">Bulk import</h3>
         <div style="font-size:13px; opacity:0.8;">Paste a chat dump. Each tag line becomes one entry. Prefix a line with the site
         ("gel: rem blue_hair", "rem gel") or use a standalone site label ("Dan:") so the lines that follow inherit it.
         Site-less lines at the start merge into one Gelbooru entry. Unparseable lines stay editable — fix or delete them below.</div>
         <textarea id="planBulkText" style="min-height:170px; width:100%; font-family:inherit; background:var(--input-bg); border:1px solid var(--border-color); border-radius:8px; color:var(--text-color); padding:10px; box-sizing:border-box;">${escapeHtml(draft)}</textarea>
         <div style="display:flex; gap:12px; align-items:center; flex-wrap:wrap;">
             <button class="action-btn" id="planNormalizeBtn">Normalize</button>
             <span id="planBulkInfo" style="font-size:12px; opacity:0.7;"></span>
         </div>
         <div id="planBulkPreview" style="display:none; flex-direction:column; gap:8px;"></div>
         <div style="display:flex; justify-content:flex-end; gap:10px;">
             <button class="action-btn" data-close>Close</button>
             <button class="action-btn" id="planImportBtn" style="display:none;">Import previewed entries</button>
         </div>`,
        e => { if (e.key === "Escape") { e.preventDefault(); planSaveBulkDraft(); close(); } });
    panel.querySelectorAll("[data-close]").forEach(b => b.addEventListener("click", planSaveBulkDraft));
    panel.querySelector("#planNormalizeBtn").addEventListener("click", planNormalize);
    panel.querySelector("#planImportBtn").addEventListener("click", planImportSelected);
    planBulkModal = { panel, close };
}

function planSaveBulkDraft() {
    const el = document.getElementById("planBulkText");
    try { if (el) localStorage.setItem("planBulkDraft", el.value); } catch { /* ignore */ }
}

async function planNormalize() {
    const textEl = document.getElementById("planBulkText");
    if (!textEl) return;
    const text = textEl.value;
    if (!text || !text.trim()) {
        const info = document.getElementById("planBulkInfo");
        if (info) info.textContent = "Paste something first.";
        return;
    }
    try {
        const d = await planFetch("/api/planning/normalize", { json: JSON.stringify({ text }) });
        planBulkRows = d.rows || [];
        if (Array.isArray(d.sites) && d.sites.length) planningSites = d.sites;
        planBulkCountdown();
        renderPlanBulkPreview();
    } catch (err) {
        showToast(err.message, { warn: true });
    }
}

function planFillable(row) {
    return row.site && String(row.tags || "").trim();
}

function planBulkCountdown() {
    const good = planBulkRows.filter(planFillable);
    const dup = good.filter(r => r.exists || r.duplicate).length;
    const info = document.getElementById("planBulkInfo");
    if (info) {
        info.textContent = planBulkRows.length
            ? `${good.length} of ${planBulkRows.length} rows are ready${dup ? " — including " + dup + " duplicate flag(s)" : ""}`
            : "Nothing parsed — every line was dropped or is only warnings.";
    }
    const btn = document.getElementById("planImportBtn");
    if (btn) {
        btn.style.display = good.length ? "inline-flex" : "none";
        btn.textContent = `Import ${good.length} entr${good.length === 1 ? "y" : "ies"}`;
    }
}

function planRatingHtml(row, n) {
    const opts = (PLAN_RATING_OPTIONS[row.site] || []).slice();
    const custom = row.rating && !opts.some(o => o[0] === row.rating);
    return `<select data-f="rating" style="min-width:120px;">
        <option value="">—</option>
        ${opts.map(o => `<option value="${escapeHtml(o[0])}" ${o[0] === row.rating ? "selected" : ""}>${escapeHtml(o[1])}</option>`).join("")}
        ${custom ? `<option value="${escapeHtml(row.rating)}" selected>${escapeHtml("Custom: " + row.rating.replace(/^rating:/, ""))}</option>` : ""}
    </select>`;
}

function renderPlanBulkPreview() {
    const box = document.getElementById("planBulkPreview");
    if (!box) return;
    box.style.display = "flex";
    box.innerHTML = planBulkRows.map((row, n) => {
        const warns = row.warnings || [];
        const badges =
            (row.site && planFillable(row) && row.exists ? planWarnChip("already in list") : "") +
            (row.duplicate ? planWarnChip("duplicate in this paste") : "");
        const src = String(row.source || "").replace(/\n/g, " ⤶ ");
        return `<div class="plan-bulk-row" data-i="${n}" style="border:1px solid var(--border-color); border-radius:8px; padding:8px;">
            <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
                <select data-f="site" style="min-width:150px;">
                    <option value="">Site…</option>
                    ${planningSites.map(s => `<option value="${escapeHtml(s)}" ${s === row.site ? "selected" : ""}>${escapeHtml(siteLabel(s))}${row.site && s !== row.site && (row.candidates || []).includes(s) ? " ✓" : ""}</option>`).join("")}
                </select>
                <input data-f="tags" style="flex:1; min-width:180px;" value="${escapeHtml(row.tags || "")}" placeholder="tags (exclusions keep -word)">
                ${planRatingHtml(row, n)}
                <input data-f="note" style="flex:0 1 190px; min-width:120px;" value="${escapeHtml(row.note || "")}" placeholder="note">
                <button data-act="row-del" class="action-btn stop-btn" title="Remove this row">✕</button>
            </div>
            <div style="font-size:11px; opacity:0.75; margin-top:5px;">
                <span style="opacity:0.8;">${escapeHtml(src)}</span>
                ${badges}
                ${warns.length ? `<span style="color:#e8a13d;"> ⚠ ${escapeHtml(warns.join("; "))}</span>` : ""}
            </div>
        </div>`;
    }).join("");
    box.querySelectorAll(".plan-bulk-row").forEach(el => {
        const n = parseInt(el.dataset.i, 10);
        const row = () => planBulkRows[n];
        const input = (name, fn) => el.querySelector(`[data-f="${name}"]`).addEventListener("input", fn);
        const bindRating = () => el.querySelector('[data-f="rating"]')
            .addEventListener("change", e => { row().rating = e.target.value; });
        input("tags", e => { row().tags = e.target.value; planBulkCountdown(); });
        input("note", e => { row().note = e.target.value; });
        bindRating();
        el.querySelector('[data-f="site"]').addEventListener("change", e => {
            row().site = e.target.value;
            row().rating = ""; // rating options differ per site; start clean
            // same-node swap: outerHTML would drop the .custom-select wrap
            el.querySelector('[data-f="rating"]').innerHTML =
                planRatingOptionsHtml(row().site, "").replace(/<\/?select[^>]*>/g, "");
            planBulkCountdown();
        });
        el.querySelector('[data-act="row-del"]').addEventListener("click", () => {
            row().deleted = true;
            el.remove();
            planBulkCountdown();
        });
    });
    enhanceAllSelects(); // rows with selects arrived after page load
    planBulkCountdown();
}

async function planImportSelected() {
    planSaveBulkDraft();
    const rows = planBulkRows.filter(r => !r.deleted && planFillable(r))
        .map(r => ({ site: r.site, tags: String(r.tags).trim(), rating: r.rating || "", note: r.note || "" }));
    if (!rows.length) return;
    try {
        const d = await planFetch("/api/planning/import", { json: JSON.stringify({ rows }) });
        const skipped = d.skipped || 0;
        showToast(`Imported ${d.added || 0} entr${(d.added || 0) === 1 ? "y" : "ies"} (${skipped} skipped as duplicates/empty)`);
        try { localStorage.removeItem("planBulkDraft"); } catch { /* ignore */ }
        if (planBulkModal) { planBulkModal.close(); planBulkModal = null; }
        await loadPlanning();
    } catch (err) {
        showToast(err.message, { warn: true });
    }
}
