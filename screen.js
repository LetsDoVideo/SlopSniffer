// SlopSniffer browser agent.
//
// Runs invisibly inside SlopSmith. Watches playback via the
// window.slopsmith event bus + highway getters, and POSTs a compact
// state snapshot to the SlopSniffer backend, which assembles the
// RockSniffer JSON served on :9938 for OBS widgets.
//
// We drive state transitions off window.slopsmith events (song:play /
// song:pause / song:ended / screen:changed) rather than monkey-patching
// playSong/showScreen. Those events are emitted by SlopSmith core
// itself (static/app.js), so they're the canonical signal and avoid the
// playSong wrapper-chain race CLAUDE.md warns about. We still POLL
// highway.getTime() for the live timer and read highway.getSongInfo()
// defensively (it can be {} for a moment after song:play).

(function () {
    'use strict';

    // RockSniffer currentState machine (matches routes.py):
    //   1 = in menus, 4 = song playing.
    var STATE_MENU = 1;
    var STATE_PLAYING = 4;

    var INGEST_URL = '/api/plugins/slopsniffer/state';

    // Local mirror of what we last sent, so we don't spam the backend
    // with identical posts every tick. We post on a timer (for the
    // moving songTimer) and immediately on state-change events.
    var _currentState = STATE_MENU;
    var _lastFilename = '';
    var _lastSongInfoComplete = false;
    var _pollTimer = null;

    function _slopsmith() {
        return (typeof window !== 'undefined') ? window.slopsmith : null;
    }

    function _highway() {
        return (typeof window !== 'undefined') ? window.highway : null;
    }

    // Read the current song's metadata from the highway. May be {} for a
    // beat after song:play (CLAUDE.md pitfall #1), so callers must treat
    // an empty/title-less result as "not ready yet" and retry.
    function _songInfo() {
        var hw = _highway();
        if (!hw || typeof hw.getSongInfo !== 'function') return {};
        try {
            return hw.getSongInfo() || {};
        } catch (e) {
            return {};
        }
    }

    function _songTime() {
        var hw = _highway();
        if (!hw || typeof hw.getTime !== 'function') return 0;
        try {
            var t = hw.getTime();
            return (typeof t === 'number' && isFinite(t)) ? t : 0;
        } catch (e) {
            return 0;
        }
    }

    // Build the POST body from highway state. The backend fills in album
    // art (by filename) and, in v0.2.0, scoring. We send what the
    // browser already knows so the JSON matches exactly what SlopSmith
    // is displaying.
    function _buildSnapshot() {
        var info = _songInfo();
        var arrangements = Array.isArray(info.arrangements) ? info.arrangements : [];
        // arrangement_index identifies the active arrangement;
        // arrangements is the full switcher list.
        var arrIdx = (typeof info.arrangement_index === 'number') ? info.arrangement_index : 0;

        return {
            currentState: _currentState,
            // The filename is NOT in getSongInfo()'s live return (despite
            // the docs listing it) -- so we capture it from the playSong()
            // call argument instead (see _wire). We store it DECODED
            // (human-readable); the backend re-encodes once for the
            // /api/song/<filename>/art lookup, matching how SlopSmith's own
            // frontend builds that URL.
            filename: _lastFilename || '',
            songName: info.title || '',
            artistName: info.artist || '',
            // albumName / albumYear are NOT in the song_info payload at all
            // -- the backend resolves them from meta_db by filename. We
            // send blanks here; the widget degrades gracefully if the
            // backend can't resolve them either.
            albumName: info.album || info.albumName || '',
            albumYear: info.year || info.albumYear || 0,
            songLength: (typeof info.duration === 'number') ? info.duration : 0,
            numArrangements: arrangements.length,
            arrangements: arrangements,
            arrangementID: String(arrIdx),
            songTimer: _songTime()
        };
    }

    function _post(body) {
        try {
            fetch(INGEST_URL, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
                // keepalive so a post fired during teardown (song end /
                // navigation) still completes.
                keepalive: true
            }).catch(function () { /* backend transient -- next tick retries */ });
        } catch (e) {
            /* swallow -- never let telemetry break playback */
        }
    }

    function _postNow() {
        _post(_buildSnapshot());
    }

    // While playing, poll so the moving songTimer reaches the widgets
    // (~4 Hz is plenty; the widgets themselves poll at ~900ms-1s). Also
    // re-checks getSongInfo so a late-populating title (the CLAUDE.md
    // race) gets sent once it lands.
    function _startPolling() {
        _stopPolling();
        _pollTimer = setInterval(function () {
            if (_currentState !== STATE_PLAYING) return;
            var info = _songInfo();
            // If metadata only just became available, remember the
            // filename so a subsequent screen change still has it.
            if (info.filename) _lastFilename = info.filename;
            if (!_lastSongInfoComplete && info.title) {
                _lastSongInfoComplete = true;
            }
            _postNow();
        }, 250);
    }

    function _stopPolling() {
        if (_pollTimer) {
            clearInterval(_pollTimer);
            _pollTimer = null;
        }
    }

    // ── Event handlers ──────────────────────────────────────────────
    // window.slopsmith.emit wraps payloads in a CustomEvent, so handlers
    // receive an Event and the payload (if any) is in e.detail. We don't
    // need the payload here -- we read live state from the highway.

    function _onPlay() {
        _currentState = STATE_PLAYING;
        var info = _songInfo();
        // NOTE: filename is captured by the playSong wrapper (_wire), not
        // from getSongInfo() -- the live getSongInfo() return doesn't
        // include it. _lastFilename is already set by the time play fires.
        _lastSongInfoComplete = !!info.title;
        _postNow();
        _startPolling();
    }

    function _onPauseOrEnd() {
        // Pause and natural end both stop the moving timer. We keep
        // currentState PLAYING on a mere pause so the widget keeps
        // showing the song (RockSniffer does the same -- a paused song
        // is still "the current song"); only a screen change back to
        // menus drops us to STATE_MENU. So pause just stops polling and
        // posts one final frozen-timer snapshot.
        _postNow();
        _stopPolling();
    }

    function _onScreenChanged(e) {
        // Leaving the player screen => back in menus. song:ended fires
        // for natural end, but the user can also navigate away mid-song;
        // screen:changed is the reliable "no longer playing" signal.
        var id = (e && e.detail && e.detail.id) ? e.detail.id : null;
        if (id && id !== 'player') {
            _currentState = STATE_MENU;
            _lastFilename = '';
            _lastSongInfoComplete = false;
            _stopPolling();
            _postNow();
        }
    }

    // Capture the current song's filename. getSongInfo() does NOT expose
    // it in the live build, but it's the first argument to playSong() and
    // it rides along on the arrangement:changed event payload. We store it
    // DECODED so the backend can re-encode once for the art lookup and so
    // the text outputs are human-readable.
    function _captureFilename(raw) {
        if (!raw) return;
        var decoded = raw;
        try { decoded = decodeURIComponent(raw); } catch (e) { /* already decoded */ }
        _lastFilename = decoded;
    }

    function _wrapPlaySong() {
        if (typeof window === 'undefined' || typeof window.playSong !== 'function') {
            return false;
        }
        // Idempotency guard: never double-wrap (e.g. if _wire retries).
        if (window.playSong.__slopsniffer_wrapped) return true;
        var original = window.playSong;
        var wrapped = function (filename /*, arrangement */) {
            // Capture BEFORE calling through, so the filename is set by the
            // time the song:play event (fired from inside) reaches _onPlay.
            _captureFilename(filename);
            // Always call and await the original so we don't break the
            // wrapper chain for other plugins (notedetect, 3dhighway, ...).
            return original.apply(this, arguments);
        };
        wrapped.__slopsniffer_wrapped = true;
        window.playSong = wrapped;
        return true;
    }

    function _onArrangementChanged(e) {
        // Carries { index, filename } -- a reliable backup filename source
        // (and keeps us correct if the user switches arrangements).
        if (e && e.detail && e.detail.filename) {
            _captureFilename(e.detail.filename);
        }
    }

    function _wire() {
        var sm = _slopsmith();
        if (!sm || typeof sm.on !== 'function') {
            // slopsmith bus not ready yet -- retry shortly. (Plugin load
            // order: app.js defines window.slopsmith before plugins load,
            // but guard anyway per CLAUDE.md's runtime-check guidance.)
            setTimeout(_wire, 100);
            return;
        }
        sm.on('song:play', _onPlay);
        sm.on('song:pause', _onPauseOrEnd);
        sm.on('song:ended', _onPauseOrEnd);
        sm.on('screen:changed', _onScreenChanged);
        sm.on('arrangement:changed', _onArrangementChanged);

        // Wrap playSong to capture the filename. If it's not defined yet,
        // retry on the same cadence as the bus check.
        if (!_wrapPlaySong()) {
            var tries = 0;
            var iv = setInterval(function () {
                if (_wrapPlaySong() || ++tries > 50) clearInterval(iv);
            }, 100);
        }

        // Post an initial idle snapshot so the backend (and any widget
        // already open in OBS) reflects "in menus" immediately on load.
        _postNow();
    }

    _wire();
})();
