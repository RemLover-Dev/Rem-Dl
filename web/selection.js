// Drag-paint + file-manager marquee selection for tag pills and gallery cards.
// Tag selection lives in the DOM (.tag-sel-on), so a re-render drops it for
// free — no ids to prune and no stale state between views.

const TAG_SEL_SEL = '[data-sel-tag], [data-sel-dims]';
const TAG_BOX_SEL = '.g-meta-tags, .hist-tags, .console-log, #historyListUI, #favoritesListUI';
const SEL_THRESHOLD = 5;

let _tagArm = null;      // armed click on a pill: {el, on, x, y}
let _tagPainting = null; // {on} once the drag passes the threshold
let _band = null;        // {mode, box, x0, y0, moved, base, scroller, pt}
let _bandRAF = 0;        // coalesces mousemove bursts into one apply per frame
let _edgeRAF = 0;        // shared edge scroller for band and paint drags
let _edgeScroller = null;
let _edgePt = null;
let _supClick = false;
let _pinnedMeta = null;  // viewer meta held expanded for the whole tag drag

// leaving the tag box mid-drag would collapse the viewer meta under the cursor
function _pinMeta(el) {
    const meta = el && el.closest && el.closest('.gallery-viewer-meta');
    if (meta && meta !== _pinnedMeta) { _unpinMeta(); meta.classList.add('band-pin'); _pinnedMeta = meta; }
}

function _unpinMeta() {
    if (!_pinnedMeta) return;
    _pinnedMeta.classList.remove('band-pin');
    _pinnedMeta = null;
}

function tagSelClear() {
    document.querySelectorAll('.tag-sel-on').forEach(el => el.classList.remove('tag-sel-on'));
}

function tagSelItems() {
    return [...document.querySelectorAll('.tag-sel-on[data-sel-tag]')]
        .map(el => ({ tag: el.dataset.selTag, site: el.dataset.selSite || '' }));
}

// dimension pills are selectable but are not tags — the bulk menu only
// copies them, it never searches/favourites them as a tag
function dimSelItems() {
    return [...document.querySelectorAll('.tag-sel-on[data-sel-dims]')]
        .map(el => el.dataset.selDims);
}

function _selHits(el, r) {
    const q = el.getBoundingClientRect();
    return q.right >= r.left && q.left <= r.right && q.bottom >= r.top && q.top <= r.bottom;
}

// the band auto-scrolls the nearest scrollable ancestor when the pointer
// reaches its top/bottom edge (a held mouse fires no mousemove, so the tick
// below keeps scrolling on its own)
function _scrollAncestor(el) {
    for (let n = el; n && n !== document.body; n = n.parentElement) {
        const cs = getComputedStyle(n);
        if (n.scrollHeight > n.clientHeight + 1 && /(auto|scroll)/.test(cs.overflowY)) return n;
    }
    return null;
}

function _bandStart(mode, box, e) {
    if (mode === 'tag') _pinMeta(box);
    _band = { mode, box, x0: e.clientX, y0: e.clientY, moved: false,
              shift: !!e.shiftKey, entered: false,
              // marquee semantics: pills selected before the drag stay selected,
              // anything the band leaves behind is dropped live
              base: mode === 'tag' ? new Set(box.querySelectorAll('.tag-sel-on')) : null,
              // the grid scrolls, not the tab, even when the drag starts in the
              // toolbar/pagination padding above or below it
              scroller: _scrollAncestor(e.target) ||
                        (mode === 'image' ? document.getElementById('galleryGrid') : null),
              pt: { x: e.clientX, y: e.clientY } };
}

function _bandSchedule() {
    if (_bandRAF) return;
    _bandRAF = requestAnimationFrame(() => {
        _bandRAF = 0;
        if (_band) _bandApply();
    });
}

function _bandApply() {
    const b = _band;
    const e = b.pt;
    const box = b.box.getBoundingClientRect();
    const rect = {
        left: Math.max(box.left, Math.min(b.x0, e.x)),
        top: Math.max(box.top, Math.min(b.y0, e.y)),
        right: Math.min(box.right, Math.max(b.x0, e.x)),
        bottom: Math.min(box.bottom, Math.max(b.y0, e.y)),
    };
    b.el.style.transform = `translate(${rect.left}px, ${rect.top}px)`;
    b.el.style.width = Math.max(0, rect.right - rect.left) + 'px';
    b.el.style.height = Math.max(0, rect.bottom - rect.top) + 'px';
    if (b.mode === 'tag') {
        const hits = new Set();
        b.box.querySelectorAll(TAG_SEL_SEL).forEach(el => { if (_selHits(el, rect)) hits.add(el); });
        // like a file-manager marquee: leaving a pill unselects it while the
        // button is still held, re-entering selects it again
        b.box.querySelectorAll('.tag-sel-on').forEach(el => {
            if (!b.base.has(el) && !hits.has(el)) el.classList.remove('tag-sel-on');
        });
        hits.forEach(el => el.classList.add('tag-sel-on'));
        return;
    }
    if (typeof gallerySelected === "undefined") return;
    const hitCards = [];
    b.box.querySelectorAll('.gallery-card[data-id]').forEach(card => {
        if (_selHits(card, rect)) hitCards.push(card);
    });
    // an empty band over blank grid space must not flip the grid into select
    // mode: nothing is selected yet, so nothing changes until a card is hit
    if (!b.entered) {
        if (!hitCards.length) return;
        b.entered = true;
        if (!b.shift) clearSelection();
        gallerySelectMode = true;
        updateSelectBar();
    }
    const wanted = new Set(b.base || []);
    hitCards.forEach(card => wanted.add(card.dataset.id));
    // apply the delta directly: setGallerySelected() calls updateSelectBar()
    // per card, which is O(n²) on every frame and makes the band stutter
    let changed = false;
    document.querySelectorAll('#galleryGrid .gallery-card.selected').forEach(card => {
        if (wanted.has(card.dataset.id)) return;
        gallerySelected.delete(card.dataset.id);
        card.classList.remove('selected');
        const box = card.querySelector('.gallery-card-select');
        if (box) box.checked = false;
        changed = true;
    });
    hitCards.forEach(card => {
        const id = card.dataset.id;
        if (gallerySelected.has(id)) return;
        const img = galleryState.images.find(i => i.id === id);
        if (!img) return;
        gallerySelected.set(id, img.filepath || "");
        card.classList.add('selected');
        const box = card.querySelector('.gallery-card-select');
        if (box) box.checked = true;
        changed = true;
    });
    if (changed) updateSelectBar();
}

const EDGE_SCROLL_PX = 40;
const EDGE_SCROLL_MAX = 24;

function _edgeStart(scroller, pt) {
    _edgeScroller = scroller;
    _edgePt = pt;
    if (!_edgeRAF) _edgeRAF = requestAnimationFrame(_edgeTick);
}

function _edgeStop() {
    if (_edgeRAF) cancelAnimationFrame(_edgeRAF);
    _edgeRAF = 0;
    _edgeScroller = null;
    _edgePt = null;
}

// the app sets `scroll-behavior: smooth` globally; a per-frame scrollTop write
// would animate and fight the next frame, so edge scrolls go instant
function _edgeScrollBy(dy) {
    const el = _edgeScroller || document.documentElement;
    const prev = el.style.scrollBehavior;
    el.style.scrollBehavior = 'auto';
    const before = el.scrollTop;
    el.scrollTop = before + dy;
    const moved = el.scrollTop !== before;
    el.style.scrollBehavior = prev;
    return moved;
}

function _edgeTick() {
    _edgeRAF = 0;
    const sc = _edgeScroller;
    const pt = _edgePt;
    if (!pt || (!_band && !_tagPainting)) return;
    let dy = 0;
    const r = sc ? sc.getBoundingClientRect() : { top: 0, bottom: window.innerHeight };
    if (pt.y < r.top + EDGE_SCROLL_PX) dy = -EDGE_SCROLL_MAX * Math.min(1, (r.top + EDGE_SCROLL_PX - pt.y) / EDGE_SCROLL_PX);
    else if (pt.y > r.bottom - EDGE_SCROLL_PX) dy = EDGE_SCROLL_MAX * Math.min(1, (pt.y - (r.bottom - EDGE_SCROLL_PX)) / EDGE_SCROLL_PX);
    if (dy) {
        _edgeScrollBy(dy);
        if (_band) _bandApply();
        else _paintAt(pt);
    }
    _edgeRAF = requestAnimationFrame(_edgeTick);
}

// one pill under the pointer (paint drags select through the exposed rows too)
function _paintAt(pt) {
    const el = document.elementFromPoint(pt.x, pt.y);
    const hit = el && el.closest && el.closest(TAG_SEL_SEL);
    if (hit) hit.classList.toggle('tag-sel-on', _tagPainting.on);
}

function _bandMove(e) {
    const b = _band;
    b.pt = { x: e.clientX, y: e.clientY };
    if (!b.moved) {
        if (Math.hypot(e.clientX - b.x0, e.clientY - b.y0) < SEL_THRESHOLD) return;
        b.moved = true;
        _supClick = true;
        if (b.mode === 'image') {
            b.base = b.shift ? new Set(gallerySelected.keys()) : new Set();
        }
        b.el = document.createElement('div');
        b.el.className = 'sel-band';
        document.body.appendChild(b.el);
        _edgeStart(b.scroller, b.pt);
    }
    _bandSchedule();
    if (b.moved) _edgePt = b.pt;
}

function _bandEnd() {
    const b = _band;
    if (b && b.el) _bandApply();   // the last move may still be waiting on rAF
    _band = null;
    if (_bandRAF) { cancelAnimationFrame(_bandRAF); _bandRAF = 0; }
    _edgeStop();
    _unpinMeta();
    if (!b) return;
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
        _pinMeta(tag);
        _tagArm = { el: tag, on: !tag.classList.contains('tag-sel-on'), x: e.clientX, y: e.clientY };
        return;
    }
    const box = e.target.closest && e.target.closest(TAG_BOX_SEL);
    if (box) { e.preventDefault(); _bandStart('tag', box, e); return; }
    const ui = e.target.closest && e.target.closest('#Gallery');
    // the whole tab is the marquee's box, so a drag can start on the toolbar's
    // empty line or under the pagination bar and still select grid cards
    if (ui && !e.target.closest('.gallery-card, button, input, select, textarea, a, label, .gallery-select-bar, .gallery-dropdown-menu')) {
        e.preventDefault();
        _bandStart('image', ui, e);
        return;
    }
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
        _edgeStart(_scrollAncestor(a.el), { x: e.clientX, y: e.clientY });
        return;
    }
    if (_tagPainting) {
        _edgePt = { x: e.clientX, y: e.clientY };
        const el = e.target.closest && e.target.closest(TAG_SEL_SEL);
        if (el) { el.classList.toggle('tag-sel-on', _tagPainting.on); _supClick = true; }
        return;
    }
    if (_band) _bandMove(e);
}, true);

function _endDrag() {
    _tagArm = null;
    _tagPainting = null;
    _edgeStop();
    _unpinMeta();
    if (_band) _bandEnd();
}
document.addEventListener('mouseup', _endDrag);
window.addEventListener('blur', _endDrag);

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
