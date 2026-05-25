"""SlopSniffer backend.

Drop-in RockSniffer replacement for SlopSmith. Serves the RockSniffer
HTTP JSON contract on port 9938 so existing OBS browser-source widgets
"just work", and writes the RockSniffer text-file output layer for
streamers who consume via OBS Text (GDI+) / Image sources.

Architecture
------------
- A single in-memory ``_state`` dict holds the latest snapshot. The
  browser-side agent (screen.js) POSTs updates to ``/api/plugins/slopsniffer/state``
  on the main SlopSmith app (uvicorn, port 8000). That handler updates
  ``_state`` and writes the text-file outputs.
- A *separate* thread runs a stdlib ``http.server`` on port 9938 that
  serves the assembled RockSniffer-shaped JSON on ``GET /``. We use the
  stdlib server (not uvicorn) deliberately: it needs no event loop and
  won't conflict with SlopSmith's own uvicorn loop, and it has zero
  dependencies.
- OBS browser sources fetch cross-origin, so the :9938 responses MUST
  carry ``Access-Control-Allow-Origin: *`` or the widgets fail silently.

In v0.1.0 the accuracy / streak fields ship as zeros (the JSON shape is
complete; those fields populate in v0.2.0 once we subscribe to the
note_detect ``note:hit`` / ``note:miss`` events on ``window.slopsmith``).
"""

import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# RockSniffer's default port. Existing widgets hardcode 127.0.0.1:9938
# in their config.js, so we must serve here for drop-in compatibility.
SNIFFER_PORT = 9938

# RockSniffer currentState machine (from its sniffer-poller.js):
#   0 = none, 1 = in menus, 2 = song selected, 3 = song starting,
#   4 = song playing, 5 = song ending.
STATE_MENU = 1
STATE_PLAYING = 4

# Process-wide singletons. The plugin loader imports this module once,
# so module-level state is shared across all requests.
_state_lock = threading.Lock()
_state = {}
_log = None
_output_dir = None
_server = None
_server_thread = None
# Absolute path to the bundled addons/ dir, so the :9938 server can serve
# the widget files itself. Set in setup(). The :8000 main app is NOT a
# reliable URL for OBS on the Desktop build (the Python server runs as an
# internal Electron subprocess and its port isn't published to the host),
# so we serve the widgets from our own :9938 server, which OBS can always
# reach -- it's the same server the widgets already poll for JSON.
_addons_dir = None

# Album-art cache: filename -> base64 jpeg string. SlopSmith serves art
# at /api/song/<encoded-filename>/art; we fetch once per song and cache.
_art_cache = {}
_art_lock = threading.Lock()


def _empty_state():
    """A complete, widget-safe snapshot representing 'nothing playing'.

    current_song.script.js HIDES the popup when songLength, albumYear,
    and numArrangements are all 0, and BAILS SILENTLY if
    memoryReadout.noteData is missing -- so noteData must ALWAYS be
    present even when idle.
    """
    return {
        "success": True,
        "currentState": STATE_MENU,
        "memoryReadout": {
            "songTimer": 0.0,
            "songID": "",
            "arrangementID": "",
            "currentHitStreak": 0,
            "highestHitStreak": 0,
            "totalNotesHit": 0,
            "totalNotesMissed": 0,
            "currentMissStreak": 0,
            "noteData": {
                "Accuracy": 0.0,
                "CurrentHitStreak": 0,
                "HighestHitStreak": 0,
                "TotalNotes": 0,
                "TotalNotesHit": 0,
                "TotalNotesMissed": 0,
                "CurrentMissStreak": 0,
            },
        },
        "albumCoverBase64": "",
        "songDetails": {
            "songID": "",
            "songName": "",
            "artistName": "",
            "albumName": "",
            "songLength": 0.0,
            "albumYear": 0,
            "numArrangements": 0,
            "albumArt": "",
            "arrangements": [],
        },
    }


# ── :9938 stdlib HTTP server ────────────────────────────────────────────

# Explicit MIME map for the widget files we serve. We avoid the stdlib
# `mimetypes` module because its results vary by OS / registry (notably
# .js is sometimes reported as text/plain on Windows, which breaks script
# loading). This covers every extension in the bundled addons/ tree.
_MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
}


def _guess_content_type(path):
    import os
    ext = os.path.splitext(path)[1].lower()
    return _MIME_TYPES.get(ext, "application/octet-stream")


class _SnifferHandler(BaseHTTPRequestHandler):
    """Serves the RockSniffer JSON on GET /. CORS-open for OBS."""

    def _write_json(self, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        # OBS browser sources are a different origin -- without this the
        # fetch is blocked and the widget shows nothing, no error.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # Static widget files live under /addons/... -- serve them from
        # our own server so OBS has a reachable URL on every install type.
        # Everything else (notably bare "/") returns the RockSniffer JSON,
        # preserving drop-in compatibility with existing widgets that poll
        # http://127.0.0.1:9938 with no path.
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        if path.startswith("/addons/") or path == "/addons":
            self._serve_static(path)
            return
        with _state_lock:
            payload = dict(_state) if _state else _empty_state()
        try:
            self._write_json(payload)
        except (BrokenPipeError, ConnectionResetError):
            # OBS closes sockets aggressively between polls -- not an error.
            pass

    def _serve_static(self, url_path):
        """Serve a file from the bundled addons/ dir. Read-only, GET-only,
        with strict path-traversal protection."""
        import os
        import posixpath
        from urllib.parse import unquote

        if not _addons_dir:
            self.send_error(404, "addons not available")
            return

        # url_path looks like "/addons/current_song/current_song.html".
        # Strip the "/addons" prefix to get the path relative to the dir.
        rel = unquote(url_path[len("/addons"):]).lstrip("/")
        # Normalise and reject any traversal that escapes the addons dir.
        # posixpath.normpath collapses ".." segments; we then verify the
        # resolved absolute path is still inside _addons_dir.
        safe_rel = posixpath.normpath(rel)
        if safe_rel.startswith("..") or os.path.isabs(safe_rel):
            self.send_error(403, "forbidden")
            return
        # Translate forward slashes (URL) to OS separators.
        parts = [p for p in safe_rel.split("/") if p not in ("", ".")]
        target = os.path.join(_addons_dir, *parts) if parts else _addons_dir
        target = os.path.abspath(target)
        if os.path.commonpath([target, os.path.abspath(_addons_dir)]) != os.path.abspath(_addons_dir):
            self.send_error(403, "forbidden")
            return
        if not os.path.isfile(target):
            self.send_error(404, "not found")
            return

        ctype = _guess_content_type(target)
        try:
            with open(target, "rb") as fh:
                body = fh.read()
        except OSError:
            self.send_error(404, "not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        # Same-origin as the JSON, but keep CORS open for consistency.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_OPTIONS(self):
        # CORS preflight (some widget stacks send it).
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def log_message(self, *args):
        # Silence the default stderr access log -- it would spam the
        # SlopSmith console at the widget poll rate (~1 Hz per source).
        pass


def _start_sniffer_server():
    global _server, _server_thread
    if _server is not None:
        return
    try:
        _server = ThreadingHTTPServer(("127.0.0.1", SNIFFER_PORT), _SnifferHandler)
    except OSError as exc:
        # Most likely real RockSniffer (or a previous instance) already
        # holds 9938. Log and continue -- the rest of the plugin still
        # works; the user just won't get our JSON until the port frees.
        if _log:
            _log.warning(
                "SlopSniffer: could not bind port %d (%s). "
                "Is RockSniffer still running?", SNIFFER_PORT, exc
            )
        _server = None
        return
    _server_thread = threading.Thread(
        target=_server.serve_forever, name="slopsniffer-9938", daemon=True
    )
    _server_thread.start()
    if _log:
        _log.info("SlopSniffer: serving RockSniffer JSON on 127.0.0.1:%d", SNIFFER_PORT)


# ── Album art ───────────────────────────────────────────────────────────

def _fetch_album_art_b64(filename):
    """Fetch + base64-encode SlopSmith's album art for ``filename``.

    Cached per filename. Returns '' on any failure (the widget tolerates
    an empty cover -- it just shows no image)."""
    if not filename:
        return ""
    with _art_lock:
        if filename in _art_cache:
            return _art_cache[filename]
    b64 = ""
    try:
        import base64
        from urllib.parse import quote
        # SlopSmith's art route is declared as:
        #   @app.get("/api/song/{filename:path}/art")
        # The {filename:path} converter captures slashes as real path
        # separators, so the filename (which can be a RELATIVE PATH under
        # the DLC dir, e.g. "cdlc favorites/ACDC - Back In Black_p.psarc")
        # must keep its "/" LITERAL -- percent-encoding the slash to %2F
        # breaks the route match. We therefore quote everything that needs
        # escaping (spaces, etc.) but keep "/" in the safe set. screen.js
        # sends us the decoded filename, so this single encode is correct.
        enc = quote(filename, safe="/!~*'()")
        url = "http://127.0.0.1:8000/api/song/%s/art" % enc
        with urllib.request.urlopen(url, timeout=5) as resp:
            if resp.status == 200:
                b64 = base64.b64encode(resp.read()).decode("ascii")
    except Exception as exc:  # noqa: BLE001 -- art is best-effort
        if _log:
            _log.debug("SlopSniffer: album art fetch failed for %r: %s", filename, exc)
        b64 = ""
    with _art_lock:
        _art_cache[filename] = b64
    return b64


# Metadata cache: filename -> {"album": str, "year": int}. SlopSmith's
# song_info WebSocket payload (what the browser sees) does NOT include
# album or year, so we fetch them server-side from the song-detail
# endpoint by filename. Cached per filename like art.
_meta_cache = {}
_meta_lock = threading.Lock()


def _fetch_song_meta(filename):
    """Fetch {album, year} for a song from SlopSmith's detail endpoint.

    GET /api/song/{filename:path} returns the cached metadata dict
    (title, artist, album, year, duration, ...). We only need album and
    year -- the browser already gave us the rest. Returns
    {"album": "", "year": 0} on any failure (widget tolerates blanks).
    Runs in-process against :8000, which is reachable here (the
    OBS-can't-reach-8000 issue is browser-side only)."""
    empty = {"album": "", "year": 0}
    if not filename:
        return dict(empty)
    with _meta_lock:
        if filename in _meta_cache:
            return dict(_meta_cache[filename])
    result = dict(empty)
    try:
        import json as _json
        from urllib.parse import quote
        # Same {filename:path} route convention as art: keep "/" literal.
        enc = quote(filename, safe="/!~*'()")
        url = "http://127.0.0.1:8000/api/song/%s" % enc
        with urllib.request.urlopen(url, timeout=5) as resp:
            if resp.status == 200:
                meta = _json.loads(resp.read().decode("utf-8"))
                if isinstance(meta, dict):
                    result["album"] = str(meta.get("album", "") or "")
                    # year is stored as TEXT and may be "", "1980", etc.
                    year_raw = meta.get("year", "") or ""
                    try:
                        result["year"] = int(str(year_raw).strip()) if str(year_raw).strip() else 0
                    except (TypeError, ValueError):
                        result["year"] = 0
    except Exception as exc:  # noqa: BLE001 -- metadata is best-effort
        if _log:
            _log.debug("SlopSniffer: song meta fetch failed for %r: %s", filename, exc)
        result = dict(empty)
    with _meta_lock:
        _meta_cache[filename] = dict(result)
    return result


# ── Text-file output layer ──────────────────────────────────────────────

def _fmt_time(seconds):
    """mm:ss, matching RockSniffer's default format.json timeFormat."""
    try:
        seconds = max(0, int(seconds))
    except (TypeError, ValueError):
        return "0:00"
    return "%d:%02d" % (seconds // 60, seconds % 60)


def _write_text_outputs(state):
    """Write the 6 RockSniffer .txt files + album_cover.jpeg.

    Field mapping mirrors RockSniffer's output.json template strings.
    Best-effort: a write failure is logged but never raised (OBS text
    sources just keep their last value)."""
    if not _output_dir:
        return
    import base64
    import os

    sd = state.get("songDetails", {})
    mr = state.get("memoryReadout", {})
    nd = mr.get("noteData", {})

    def _write(name, text):
        try:
            path = os.path.join(_output_dir, name)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
        except OSError as exc:
            if _log:
                _log.debug("SlopSniffer: could not write %s: %s", name, exc)

    artist = sd.get("artistName", "")
    name = sd.get("songName", "")
    album = sd.get("albumName", "")
    year = sd.get("albumYear", 0)
    timer = mr.get("songTimer", 0)
    length = sd.get("songLength", 0)
    hit = nd.get("TotalNotesHit", 0)
    total = nd.get("TotalNotes", 0)
    accuracy = nd.get("Accuracy", 0.0)
    cur_streak = nd.get("CurrentHitStreak", 0)
    high_streak = nd.get("HighestHitStreak", 0)

    _write("song_details.txt", "%s - %s" % (artist, name))
    _write("album_details.txt", "%s (%s)" % (album, year))
    _write("song_timer.txt", "%s/%s" % (_fmt_time(timer), _fmt_time(length)))
    _write("notes.txt", "%d/%d" % (hit, total))
    _write("accuracy.txt", "%.2f%%" % float(accuracy or 0.0))
    _write("streaks.txt", "%d/%d" % (cur_streak, high_streak))

    # album_cover.jpeg -- decode the cached base64 to a real file so OBS
    # Image sources can point at it.
    cover_b64 = state.get("albumCoverBase64", "")
    if cover_b64:
        try:
            path = os.path.join(_output_dir, "album_cover.jpeg")
            with open(path, "wb") as fh:
                fh.write(base64.b64decode(cover_b64))
        except (OSError, ValueError) as exc:
            if _log:
                _log.debug("SlopSniffer: could not write album_cover.jpeg: %s", exc)


# ── State ingest (from the browser agent) ───────────────────────────────

def _apply_browser_state(incoming):
    """Build a full RockSniffer snapshot from the browser's POST body.

    The browser sends what it already knows from highway.getSongInfo()
    and highway.getTime() -- title, artist, duration, arrangement count,
    timer, and a currentState. We assemble the RockSniffer-shaped dict,
    fetch album art by filename, and (in v0.2.0) merge scoring. v0.1.0
    leaves accuracy/streaks at zero."""
    state = _empty_state()

    cur = incoming.get("currentState")
    if cur in (STATE_MENU, STATE_PLAYING):
        state["currentState"] = cur

    filename = incoming.get("filename", "") or ""

    sd = state["songDetails"]
    sd["songID"] = filename
    sd["songName"] = incoming.get("songName", "") or ""
    sd["artistName"] = incoming.get("artistName", "") or ""
    sd["albumName"] = incoming.get("albumName", "") or ""
    sd["albumYear"] = int(incoming.get("albumYear", 0) or 0)
    try:
        sd["songLength"] = float(incoming.get("songLength", 0) or 0.0)
    except (TypeError, ValueError):
        sd["songLength"] = 0.0
    sd["numArrangements"] = int(incoming.get("numArrangements", 0) or 0)
    sd["arrangements"] = incoming.get("arrangements", []) or []

    mr = state["memoryReadout"]
    mr["songID"] = filename
    mr["arrangementID"] = incoming.get("arrangementID", "") or ""
    try:
        mr["songTimer"] = float(incoming.get("songTimer", 0) or 0.0)
    except (TypeError, ValueError):
        mr["songTimer"] = 0.0

    # Album art -- only meaningful while a song is loaded.
    if filename and state["currentState"] == STATE_PLAYING:
        b64 = _fetch_album_art_b64(filename)
        state["albumCoverBase64"] = b64
        sd["albumArt"] = b64

        # Album name / year aren't in the browser's song_info, so fill
        # them from SlopSmith's song-detail endpoint by filename. A
        # browser-supplied value (if a future build adds it) still wins.
        if not sd["albumName"] or not sd["albumYear"]:
            meta = _fetch_song_meta(filename)
            if not sd["albumName"]:
                sd["albumName"] = meta.get("album", "")
            if not sd["albumYear"]:
                sd["albumYear"] = int(meta.get("year", 0) or 0)

    return state


def setup(app, context):
    """Plugin entry point. Registers the ingest route and starts :9938."""
    global _log, _output_dir, _addons_dir
    _log = context.get("log")
    # Write the RockSniffer text-file outputs under the plugin's config
    # dir so they're easy to find and survive restarts.
    import os
    config_dir = context.get("config_dir")
    if config_dir:
        _output_dir = os.path.join(str(config_dir), "output")
        try:
            os.makedirs(_output_dir, exist_ok=True)
        except OSError as exc:
            if _log:
                _log.warning("SlopSniffer: could not create output dir: %s", exc)
            _output_dir = None

    # Locate the bundled addons/ dir (sits next to this file in the plugin
    # tree) so the :9938 server can serve the widget files itself. This is
    # set BEFORE starting the server so the handler can reference it.
    addons_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "addons")
    if os.path.isdir(addons_dir):
        _addons_dir = addons_dir
    elif _log:
        _log.warning("SlopSniffer: addons dir not found at %s", addons_dir)

    # Seed the idle snapshot so :9938 has something widget-safe to serve
    # before the first browser POST.
    with _state_lock:
        _state.clear()
        _state.update(_empty_state())

    _start_sniffer_server()

    # Lazy FastAPI imports -- the host already has these; importing at
    # module top would couple us to the framework even when this file is
    # read by tooling.
    from fastapi import Request
    from fastapi.responses import JSONResponse

    @app.post("/api/plugins/slopsniffer/state")
    async def ingest_state(request: Request):  # noqa: ANN001 -- FastAPI handler
        try:
            incoming = await request.json()
        except Exception:  # noqa: BLE001 -- malformed body
            return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
        if not isinstance(incoming, dict):
            return JSONResponse({"ok": False, "error": "expected object"}, status_code=400)

        state = _apply_browser_state(incoming)
        with _state_lock:
            _state.clear()
            _state.update(state)
        _write_text_outputs(state)
        return JSONResponse({"ok": True})

    # Convenience GET on the main app so a user can sanity-check the
    # current snapshot from a browser tab without hitting :9938.
    @app.get("/api/plugins/slopsniffer/state")
    async def read_state():
        with _state_lock:
            return JSONResponse(dict(_state) if _state else _empty_state())

    # The bundled widgets are served by our own :9938 server (see
    # _SnifferHandler._serve_static), NOT by the SlopSmith main app on
    # :8000. On the Desktop build, :8000 is an internal Electron subprocess
    # port that isn't reliably reachable from OBS or a browser, whereas
    # :9938 is our own socket and is always reachable -- it's the same
    # server the widgets poll for JSON, so widget + data are same-origin.
    if _addons_dir and _log:
        _log.info(
            "SlopSniffer: widgets served at "
            "http://127.0.0.1:%d/addons/current_song/current_song.html "
            "and http://127.0.0.1:%d/addons/note_streaks/note_streaks.html",
            SNIFFER_PORT, SNIFFER_PORT,
        )

    if _log:
        _log.info("SlopSniffer: ready (state ingest registered, output dir: %s)", _output_dir)
