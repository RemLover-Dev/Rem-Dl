// Drag-paint + file-manager marquee selection for tag pills and gallery cards.
// Tag selection lives in the DOM (.tag-sel-on), so a re-render drops it for
// free — no ids to prune and no stale state between views.

const TAG_SEL_SEL = '[data-sel-tag]';
const TAG_BOX_SEL = '.g-meta-tags, .hist-tags, .console-log, #historyListUI, #favoritesListUI';
const SEL_THRESHOLD = 5;

let _tagArm = null;      // armed click on a pill: {el, on, x, y}
let _tagPainting = null; // {on} once the drag passes the threshold
let _band = null;        // {mode, box, x0, y0, moved, base}
let _supClick = false;

function tagSelClear() {
    document.querySelectorAll('.tag-sel-on').forEach(el => el.classList.remove('tag-sel-on'));
}

function tagSelItems() {
    return [...document.querySelectorAll('.tag-sel-on')]
        .map(el => ({ tag: el.dataset.selTag, site: el.dataset.selSite || '' }));
}

function _selHits(el, r) {
    const q = el.getBoundingClientRect();
    return q.right >= r.left && q.left <= r.right && q.bottom >= r.top && q.top <= r.bottom;
}

function _bandStart(mode, box, e) {
    _band = { mode, box, x0: e.clientX, y0: e.clientY, moved: false, base: null };
}

function _bandMove(e) {
    const b = _band;
    if (!b.moved) {
        if (Math.hypot(e.clientX - b.x0, e.clientY - b.y0) < SEL_THRESHOLD) return;
        b.moved = true;
        _supClick = true;
        if (b.mode === 'image') {
            gallerySelectMode = true;
            b.base = e.shiftKey ? new Set(gallerySelected.keys()) : new Set();
            if (!e.shiftKey) clearSelection();
            updateSelectBar();
        }
        b.el = document.createElement('div');
        b.el.className = 'sel-band';
        document.body.appendChild(b.el);
    }
    const box = b.box.getBoundingClientRect();
    const rect = {
        left: Math.max(box.left, Math.min(b.x0, e.clientX)),
        top: Math.max(box.top, Math.min(b.y0, e.clientY)),
        right: Math.min(box.right, Math.max(b.x0, e.clientX)),
        bottom: Math.min(box.bottom, Math.max(b.y0, e.clientY)),
    };
    b.el.style.left = rect.left + 'px';
    b.el.style.top = rect.top + 'px';
    b.el.style.width = Math.max(0, rect.right - rect.left) + 'px';
    b.el.style.height = Math.max(0, rect.bottom - rect.top) + 'px';
    if (b.mode === 'tag') {
        b.box.querySelectorAll(TAG_SEL_SEL).forEach(el => { if (_selHits(el, rect)) el.classList.add('tag-sel-on'); });
        return;
    }
    const wanted = new Set(b.base);
    b.box.querySelectorAll('.gallery-card[data-id]').forEach(card => { if (_selHits(card, rect)) wanted.add(card.dataset.id); });
    document.querySelectorAll('#galleryGrid .gallery-card.selected').forEach(card => {
        if (!wanted.has(card.dataset.id)) setGallerySelected(card.dataset.id, false);
    });
    wanted.forEach(id => { if (!gallerySelected.has(id)) setGallerySelected(id, true); });
}

function _bandEnd() {
    const b = _band;
    _band = null;
    if (b.el) b.el.remove();
    else if (b.mode === 'tag') tagSelClear();
    else if (b.mode === 'image' && gallerySelectMode) clearSelection();
}

document.addEventListener('mousedown', e => {
    if (e.button !== 0) return;
    _supClick = false;
    if (e.target.closest && e.target.closest('.ctx-menu')) return;
    const tag = e.target.closest && e.target.closest(TAG_SEL_SEL);
    if (tag) {
        e.preventDefault(); // no text drag-select under the pills
        _tagArm = { el: tag, on: !tag.classList.contains('tag-sel-on'), x: e.clientX, y: e.clientY };
        return;
    }
    const box = e.target.closest && e.target.closest(TAG_BOX_SEL);
    if (box) { e.preventDefault(); _bandStart('tag', box, e); return; }
    const grid = e.target.closest && e.target.closest('#galleryGrid');
    if (grid && !e.target.closest('.gallery-card')) { e.preventDefault(); _bandStart('image', grid, e); return; }
    tagSelClear();
}, true);

document.addEventListener('mousemove', e => {
    if (_tagArm) {
        const a = _tagArm;
        if (Math.hypot(e.clientX - a.x, e.clientY - a.y) < SEL_THRESHOLD) return;
        _tagArm = null;
        _tagPainting = { on: a.on };
        _supClick = true;
        a.el.classList.toggle('tag-sel-on', a.on);
        return;
    }
    if (_tagPainting) {
        const el = e.target.closest && e.target.closest(TAG_SEL_SEL);
        if (el) { el.classList.toggle('tag-sel-on', _tagPainting.on); _supClick = true; }
        return;
    }
    if (_band) _bandMove(e);
}, true);

document.addEventListener('mouseup', () => {
    _tagArm = null;
    _tagPainting = null;
    if (_band) _bandEnd();
});
window.addEventListener('blur', () => {
    _tagArm = null;
    _tagPainting = null;
    if (_band) _bandEnd();
});

// a paint drag must not also fire the pill's search / the card's open-viewer
document.addEventListener('click', e => {
    if (!_supClick) return;
    _supClick = false;
    e.preventDefault();
    e.stopPropagation();
}, true);

document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && document.querySelector('.tag-sel-on')) tagSelClear();
}, true);

// viewer tag pills / log tags / history rows all go through this, so the
// quoted-token format matches a single-pill search and more terms can follow
function searchGalleryTags(raws, exact = true) {
    tagSelClear();
    const s = document.getElementById('gallerySearch');
    if (s) s.value = raws.map(t => exact ? '"' + cleanTagDisplay(t) + '",' : cleanTagDisplay(t)).join(' ');
    openTab('Gallery');
    closeGalleryViewer();
    loadGallery(1);
}
