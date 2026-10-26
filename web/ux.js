/* Small, event-driven UI helpers. No polling, external assets or saved search text. */
(function () {
    'use strict';
    if (new URLSearchParams(window.location.hash.slice(1)).has('rems-token')) history.replaceState(null, '', window.location.pathname + window.location.search);
    let dirty = false, revision = 0;
    const status = document.getElementById('settingsSaveStatus');
    const savedFields = new Set(['r34Key','r34Uid','danLogin','danApiKey','gelKey','gelUid','gsApiKey',
        'konaLogin','konaPassword','sankaLogin','sankaPassword','zeroLogin','zeroPassword',
        'pinterestCookies','pinterestEmail','pinterestPassword','pixivToken','pixivCookie',
        'verifyTls','apiTimeout','retryWait','antiBanPause','downloadRetries','reqRateLimit','dedupEnabled']);
    function setDirty(value) {
        dirty = value;
        const marker = document.getElementById('settingsDirtyMark');
        if (marker) marker.hidden = !value;
        if (status) status.textContent = value ? 'Unsaved changes' : 'All changes saved';
    }
    function edited(event) {
        if (savedFields.has(event.target.id) || event.target.classList.contains('blur-rating-box')) {
            revision++;
            setDirty(true);
        }
    }
    document.getElementById('Options').addEventListener('input', edited);
    document.getElementById('Options').addEventListener('change', edited);
    window.addEventListener('beforeunload', event => {
        if (dirty) { event.preventDefault(); event.returnValue = ''; }
    });
    window.RemUX = {
        settingsRevision: () => revision,
        settingsSaved: started => setDirty(revision !== started),
        settingsFailed: () => { dirty = true; document.getElementById('settingsDirtyMark').hidden = false; },
        hasUnsavedChanges: () => dirty
    };

    document.addEventListener('keydown', event => {
        if (event.defaultPrevented || event.isComposing || event.altKey || document.querySelector('.custom-confirm-overlay')) return;
        const viewer = document.getElementById('galleryViewer');
        if (viewer && viewer.style.display === 'flex') return;
        if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's' && document.getElementById('Options').style.display !== 'none') { event.preventDefault(); saveAllSettings(); }
        else if (!event.ctrlKey && !event.metaKey && !isTypingTarget(event.target)) {
            if (event.key === '/') { event.preventDefault(); openTab('Gallery'); const search = document.getElementById('gallerySearch'); search.focus(); search.select(); }
        }
    });

    const banner = document.getElementById('connectionBanner');
    const message = document.getElementById('connectionMessage');
    const retry = document.getElementById('connectionRetry');
    const signIn = document.getElementById('connectionSignIn');
    let everConnected = socket.connected, lastAuthCheck = 0, checking = false, connectionEpoch = 0;
    function showConnection(text, auth = false) {
        banner.hidden = false; message.textContent = text; retry.hidden = auth; signIn.hidden = !auth;
    }
    socket.on('connect', () => {
        connectionEpoch++;
        banner.hidden = true;
        if (everConnected) { showToast('Connection restored'); socket.emit('get_queue'); }
        everConnected = true;
    });
    socket.on('disconnect', () => { connectionEpoch++; showConnection('Connection lost. Reconnecting… Downloads may still be running.'); });
    socket.on('connect_error', async () => {
        showConnection('Unable to connect. Retrying automatically…');
        if (checking || Date.now() - lastAuthCheck < 10000) return;
        lastAuthCheck = Date.now(); checking = true;
        const epoch = connectionEpoch;
        try {
            const response = await fetch('/api/config', {cache:'no-store'});
            if (response.status === 401 && epoch === connectionEpoch && !socket.connected) {
                socket.disconnect();
                showConnection('Your session has ended. Sign in again to reconnect.', true);
            }
        } catch (_) { /* The connection message already explains network failure. */ }
        finally { checking = false; }
    });
    retry.onclick = () => { lastAuthCheck = 0; socket.disconnect(); socket.connect(); };
    signIn.onclick = async () => {
        if (dirty && !await customConfirm('Sign in again? Unsaved settings on this page will be lost.', 'Sign in')) return;
        dirty = false; window.location.assign('/');
    };
})();
