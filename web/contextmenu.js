// ---- right-click menus ---------------------------------------------------
// one engine: a menu is a list of {label, run} leaves, {label, items} submenus
// and {sep:true}, so nested menus come free
const CTX_CATEGORY_LABELS = {artist:"Artist", character:"Character", copyright:"Copyright", metadata:"Other tags", outfit:"Outfit", group:"Group", hair:"Hair", eyes:"Eyes", mangaka:"Mangaka", game:"Game", theme:"Theme", source:"Source", meta:"Meta", vtuber:"VTuber", series:"Series", studio:"Studio", tag:"General tags"};
// preset-only workers (neko/nekosapi/nekos_life) take no free-text tag
const QUEUE_SITE_KEYS = ["dan", "gelbooru", "gsbooru", "kona", "yande", "safe", "sankaku", "rule34", "zero", "pixiv", "pinterest", "x", "anime_dl", "eshuushuu", "nekosia"];

let _ctxMenus = [];
let _ctxCloseTimer = 0;
function closeContextMenu() {
    if (_ctxCloseTimer) { clearTimeout(_ctxCloseTimer); _ctxCloseTimer = 0; }
    _ctxMenus.forEach(m => m.remove());
    _ctxMenus = [];
}
// menus are pointer-driven: walking off them dismisses them, with a short grace
// period so the diagonal trip from a row into its submenu doesn't drop it
document.addEventListener('mouseover', e => {
    if (!_ctxMenus.length) return;
    if (e.target.closest && e.target.closest('.ctx-menu')) {
        if (_ctxCloseTimer) { clearTimeout(_ctxCloseTimer); _ctxCloseTimer = 0; }
        return;
    }
    if (_ctxCloseTimer) return;
    _ctxCloseTimer = setTimeout(() => { _ctxCloseTimer = 0; closeContextMenu(); }, 200);
}, true);
function _ctxCloseDeeper(depth) {
    _ctxMenus = _ctxMenus.filter(m => {
        if (m._depth > depth) { m.remove(); return false; }
        return true;
    });
}
function _buildContextMenu(items, depth) {
    const menu = document.createElement('div');
    menu.className = 'ctx-menu' + (depth ? ' ctx-sub' : '');
    menu._depth = depth;
    items.forEach(it => {
        if (it.sep) {
            const sep = document.createElement('div');
            sep.className = 'ctx-sep';
            menu.appendChild(sep);
            return;
        }
        const row = document.createElement('div');
        row.className = 'ctx-item' + (it.disabled ? ' disabled' : '') + (it.items ? ' has-sub' : '');
        row.innerHTML = `<span>${escapeHtml(it.label)}</span>` + (it.items ? '<span class="ctx-arrow">›</span>' : '');
        if (!it.disabled && it.items) {
            row.addEventListener('mouseenter', () => {
                _ctxCloseDeeper(depth);
                const sub = _buildContextMenu(it.items, depth + 1);
                sub.style.left = '0px'; sub.style.top = '0px';
                document.body.appendChild(sub);
                const r = row.getBoundingClientRect();
                const sr = sub.getBoundingClientRect();
                let x = r.right + 2, y = r.top - 4;
                if (x + sr.width > window.innerWidth - 8) x = Math.max(8, r.left - sr.width - 2);
                if (y + sr.height > window.innerHeight - 8) y = Math.max(8, window.innerHeight - sr.height - 8);
                sub.style.left = x + 'px';
                sub.style.top = y + 'px';
                _ctxMenus.push(sub);
            });
        } else if (!it.disabled && it.run) {
            row.addEventListener('mouseenter', () => _ctxCloseDeeper(depth));
            row.addEventListener('click', ev => {
                ev.stopPropagation();
                closeContextMenu();
                Promise.resolve().then(it.run).catch(err => console.error("context menu action failed:", err));
            });
        }
        menu.appendChild(row);
    });
    return menu;
}
function showContextMenu(e, items) {
    e.preventDefault();
    e.stopPropagation();
    closeContextMenu();
    const menu = _buildContextMenu(items, 0);
    menu.style.left = '0px';
    menu.style.top = '0px';
    document.body.appendChild(menu);
    const r = menu.getBoundingClientRect();
    let x = e.clientX, y = e.clientY;
    if (x + r.width > window.innerWidth - 8) x = Math.max(8, window.innerWidth - r.width - 8);
    if (y + r.height > window.innerHeight - 8) y = Math.max(8, window.innerHeight - r.height - 8);
    menu.style.left = x + 'px';
    menu.style.top = y + 'px';
    _ctxMenus = [menu];
}
document.addEventListener('mousedown', e => {
    if (_ctxMenus.length && !(e.target.closest && e.target.closest('.ctx-menu'))) closeContextMenu();
}, true);
// wheel, not scroll: background panes (log consoles, gallery autoscroll) fire
// scroll events on their own and would tear the menu down mid-hover
document.addEventListener('wheel', () => closeContextMenu(), { capture: true, passive: true });
document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && _ctxMenus.length) { e.preventDefault(); closeContextMenu(); }
}, true);

async function copyTextToClipboard(text, okMsg) {
    if (!text) { showToast("Nothing to copy", { warn: true, icon: WARN_ICON }); return; }
    try {
        await navigator.clipboard.writeText(text);
    } catch (err) {
        const holder = document.createElement('textarea');
        holder.value = text;
        holder.style.position = 'fixed';
        holder.style.opacity = '0';
        document.body.appendChild(holder);
        holder.select();
        const ok = document.execCommand('copy');
        holder.remove();
        if (!ok) { showToast("Copy failed - browser refused clipboard access", { warn: true, icon: WARN_ICON }); return; }
    }
    showToast(okMsg || "Copied to clipboard", { icon: COPY_ICON });
}

function imageContextMenu(e, img, historyMode) {
    const stem = String(img.filename || "").replace(/\.[^.]+$/, "");
    const dict = normalizeTags(img.tags || {});
    const allTags = TAG_CATEGORIES.reduce((acc, c) => acc.concat(dict[c] || []), []);
    const catRank = c => (c === "metadata" ? TAG_CATEGORIES.length : TAG_CATEGORIES.indexOf(c));
    const catItems = TAG_CATEGORIES
        .filter(c => (dict[c] || []).length)
        .sort((a, b) => catRank(a) - catRank(b))
        .map(c => ({ label: CTX_CATEGORY_LABELS[c] || c, run: () => copyTextToClipboard(dict[c].join(" "), `Copied ${dict[c].length} ${CTX_CATEGORY_LABELS[c] || c} tag(s)`) }));
    showContextMenu(e, [
        { label: "Copy", items: [
            { label: "Image hash", run: () => copyTextToClipboard(stem, "Copied image hash") },
            { label: "Image", run: () => copyToClipboard(fullImageUrl(img.filepath, img.filename), img.filename) },
            { label: "Tags", items: [
                { label: "All tags", run: () => copyTextToClipboard(allTags.join(" "), "Copied all tags") },
                ...(catItems.length ? [{ sep: true }, ...catItems] : []),
            ]},
        ]},
        { sep: true },
        { label: img.favourite ? "Unfavourite" : "Favourite", run: () => toggleImageFavourite(img, historyMode) },
        { label: "Delete", run: () => deleteImageFromMenu(img, historyMode) },
    ]);
}

async function toggleImageFavourite(img, historyMode) {
    try {
        if (historyMode || !img.id) {
            const r = await fetch("/api/gallery/favourite_by_name", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ filename: img.filename, filepath: img.filepath }) });
            const d = await r.json();
            if (d && d.success) img.favourite = d.favourite;
        } else {
            await fetch("/api/gallery/favourite", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ id: img.id }) });
            img.favourite = !img.favourite;
        }
    } catch (err) { console.error("favourite save failed:", err); }
    if (historyMode) { if (typeof renderImageHistory === "function") renderImageHistory(); }
    else loadGallery();
}

async function deleteImageFromMenu(img, historyMode) {
    if (!await customConfirm("Delete this image? It will be removed from disk.", "Delete")) return;
    const byName = historyMode || !img.id;
    try {
        const r = await fetch(byName ? "/api/gallery/delete_by_name" : "/api/gallery/delete", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify(byName ? { filename: img.filename, filepath: img.filepath } : { id: img.id }),
        });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) { showToast("Delete failed: " + (d.error || r.status), { warn: true, icon: WARN_ICON }); return; }
        markDeleted(img.filepath, img.filename);
        if (img.id) {
            const card = document.querySelector(`.gallery-card[data-id="${CSS.escape(String(img.id))}"]`);
            if (card) card.remove();
        }
        showToast(d.dedup_warning ? "Image deleted - duplicate cleanup will finish on Refresh" : "Image deleted", d.dedup_warning ? { warn: true, icon: WARN_ICON } : { icon: TRASH_ICON });
        if (historyMode) { if (typeof renderImageHistory === "function") renderImageHistory(); }
        else loadGallery();
    } catch (err) { showToast("Delete failed: " + err.message, { warn: true, icon: WARN_ICON }); }
}

function historyImageContextMenu(e, filepath) {
    const all = (typeof imgHistFiltered !== "undefined" && imgHistFiltered.length) ? imgHistFiltered : (typeof imageHistory !== "undefined" ? imageHistory : []);
    const img = all.find(i => String(i.filepath || "") === String(filepath));
    if (img) imageContextMenu(e, img, true);
}

function tagContextMenu(e, tag, site) {
    if (!tag) return;
    const fav = !!site && typeof isFavorite === "function" && isFavorite(site, tag);
    showContextMenu(e, [
        { label: fav ? "Remove tag favourite" : "Favourite tag", disabled: !site, run: () => toggleFavorite(site, tag) },
        { sep: true },
        { label: "Search tag", items: [
            { label: "Exact match", run: () => searchGalleryTag(tag, true) },
            { label: "Substring", run: () => searchGalleryTag(tag, false) },
        ]},
        { label: "Add to queue", items: QUEUE_SITE_KEYS
            .map(s => ({ key: s, label: siteLabel(s) }))
            .sort((a, b) => a.label.localeCompare(b.label))
            .map(s => ({ label: s.label, run: () => queueHistoryEntry(s.key, tag) })) },
    ]);
}
