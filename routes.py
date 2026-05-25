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

# Context primitives captured in setup(), used for DIRECT in-process
# metadata/art access. routes.py runs inside the SlopSmith server process,
# so we can read meta_db, the DLC dir, and the art cache directly off disk --
# no HTTP round-trip to a port we'd have to guess. The Desktop build binds
# the Python server to a DYNAMIC port chosen at launch (confirmed in the
# Electron main process: it spawns python, waits for it to report its port,
# then loads http://127.0.0.1:<that port>), so there is no fixed port to hit
# and the OBS browser source can't reach it either. Reading files directly
# sidesteps all of that.
#   _ctx_meta_db          -- shared MetadataDB (.get(filename, mtime, size))
#   _ctx_get_dlc_dir()    -- DLC folder Path
#   _ctx_get_art_cache_dir() -- SlopSmith's art_cache dir (cached PNGs)
_ctx_meta_db = None
_ctx_get_dlc_dir = None
_ctx_get_art_cache_dir = None


def _art_safe_name(filename):
    """SlopSmith's art-cache key: filename with '/' and ' ' -> '_', .png.

    Mirrors server.py exactly:
        safe_name = filename.replace("/", "_").replace(" ", "_")
        cached = ART_CACHE_DIR / f"{safe_name}.png"
    """
    return filename.replace("/", "_").replace(" ", "_") + ".png"


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
    """Return base64 album art for ``filename`` by reading it off disk.

    SlopSmith caches extracted album art as PNG in its art_cache dir, keyed
    by the filename with '/' and ' ' replaced by '_'. Any song the user has
    seen in the library grid already has its art cached, so we just read
    that PNG -- no HTTP, no server port (which is dynamic on Desktop), no
    PSARC parsing. If the cache file isn't there yet, we fall back to
    extracting from the PSARC the same way the server does (best-effort;
    needs PIL, which the SlopSmith env has). Cached per filename in-process.
    Returns '' on any failure (the widget tolerates an empty cover)."""
    import os
    if not filename:
        return ""
    with _art_lock:
        if filename in _art_cache:
            return _art_cache[filename]
    b64 = ""
    try:
        import base64
        # 1) Fast path: read SlopSmith's already-cached PNG from disk.
        cache_dir = _ctx_get_art_cache_dir() if _ctx_get_art_cache_dir else None
        if cache_dir:
            cached_png = os.path.join(str(cache_dir), _art_safe_name(filename))
            if os.path.isfile(cached_png):
                with open(cached_png, "rb") as fh:
                    b64 = base64.b64encode(fh.read()).decode("ascii")
                if _log:
                    _log.info("SlopSniffer: album art from cache (%d b64 chars)", len(b64))

        # 2) Fallback: extract art from the PSARC directly, like the server
        #    does (unpack -> largest .dds -> PNG). Only if not cached.
        if not b64:
            b64 = _extract_art_from_psarc(filename)

        if not b64 and _log:
            _log.info("SlopSniffer: no album art found for %r yet", filename)
    except Exception as exc:  # noqa: BLE001 -- art is best-effort
        if _log:
            _log.warning("SlopSniffer: album art read error for %r: %s", filename, exc)
        b64 = ""
    with _art_lock:
        _art_cache[filename] = b64
    return b64


def _extract_art_from_psarc(filename):
    """Best-effort: extract album art from the PSARC on disk and return it
    as base64 PNG. Mirrors SlopSmith's own extraction (largest embedded
    .dds -> PNG via PIL). Returns '' if anything is unavailable."""
    import os
    import base64
    import tempfile
    import shutil
    if _ctx_get_dlc_dir is None:
        return ""
    try:
        dlc = _ctx_get_dlc_dir()
        if not dlc:
            return ""
        psarc_path = os.path.join(str(dlc), filename)
        if not os.path.isfile(psarc_path):
            return ""
        # Only PSARCs carry embedded .dds art needing extraction; sloppak /
        # loose-folder songs serve a cover file the cache path already
        # covers, so skip extraction for non-psarc here.
        if not psarc_path.lower().endswith(".psarc"):
            return ""
        # Use SlopSmith's own psarc unpacker + PIL, imported lazily so a
        # missing dep just disables this fallback rather than breaking load.
        try:
            from psarc import unpack_psarc  # SlopSmith lib/ is on sys.path
        except Exception:
            return ""
        try:
            from PIL import Image
        except Exception:
            return ""
        from pathlib import Path
        tmp = tempfile.mkdtemp(prefix="slopsniffer_art_")
        try:
            unpack_psarc(str(psarc_path), tmp)
            dds = sorted(Path(tmp).rglob("*.dds"),
                         key=lambda p: p.stat().st_size, reverse=True)
            if not dds:
                return ""
            img = Image.open(str(dds[0])).convert("RGB")
            out = os.path.join(tmp, "_art.png")
            img.save(out, "PNG")
            with open(out, "rb") as fh:
                b64 = base64.b64encode(fh.read()).decode("ascii")
            if _log:
                _log.info("SlopSniffer: album art extracted from PSARC (%d b64 chars)", len(b64))
            return b64
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001
        if _log:
            _log.debug("SlopSniffer: PSARC art extraction failed for %r: %s", filename, exc)
        return ""


# Metadata cache: filename -> {"album": str, "year": int}. SlopSmith's
# song_info WebSocket payload (what the browser sees) does NOT include
# album or year, so we fetch them server-side from the song-detail
# endpoint by filename. Cached per filename like art.
_meta_cache = {}
_meta_lock = threading.Lock()


def _fetch_song_meta(filename):
    """Get {album, year} for a song by reading meta_db DIRECTLY in-process.

    SlopSmith's song_info WebSocket payload (what the browser sees) does
    NOT include album or year. Rather than HTTP-fetch the song-detail
    endpoint (whose port we can't reliably know on the Desktop build), we
    read the shared MetadataDB the same way the server's own route does:
    resolve the file under the DLC dir, stat it for the cache key, and
    call meta_db.get(cache_key, mtime, size). Returns {"album","year"}.
    """
    import os
    empty = {"album": "", "year": 0}
    if not filename:
        return dict(empty)
    with _meta_lock:
        if filename in _meta_cache:
            return dict(_meta_cache[filename])
    result = dict(empty)
    try:
        if _ctx_meta_db is None or _ctx_get_dlc_dir is None:
            if _log:
                _log.warning("SlopSniffer: meta lookup unavailable (no context db/dlc)")
            raise RuntimeError("no meta context")
        dlc = _ctx_get_dlc_dir()
        if not dlc:
            raise RuntimeError("DLC dir not configured")
        # Resolve the song file under the DLC dir. screen.js sends the
        # filename as the relative path SlopSmith uses (e.g.
        # "cdlc favorites/ACDC - Back In Black_p.psarc").
        song_path = os.path.join(str(dlc), filename)
        if not os.path.isfile(song_path):
            if _log:
                _log.warning("SlopSniffer: song file not found for meta: %s", song_path)
            raise FileNotFoundError(song_path)
        st = os.stat(song_path)
        # The server canonicalises the cache key as the POSIX relative
        # path under the DLC dir -- which is exactly `filename` as sent.
        cache_key = filename.replace("\\", "/")
        meta = _ctx_meta_db.get(cache_key, st.st_mtime, st.st_size)
        if meta is None:
            # mtime/size mismatch or not yet cached. Try the bare key too.
            if _log:
                _log.info("SlopSniffer: meta_db miss for %r (mtime=%s size=%s)",
                          cache_key, st.st_mtime, st.st_size)
        if isinstance(meta, dict):
            result["album"] = str(meta.get("album", "") or "")
            year_raw = meta.get("year", "") or ""
            try:
                result["year"] = int(str(year_raw).strip()) if str(year_raw).strip() else 0
            except (TypeError, ValueError):
                result["year"] = 0
            if _log:
                _log.info("SlopSniffer: meta for %r -> album=%r year=%r",
                          filename, result["album"], result["year"])
    except Exception as exc:  # noqa: BLE001 -- metadata is best-effort
        if _log:
            _log.warning("SlopSniffer: song meta lookup failed for %r: %s", filename, exc)
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
    global _log, _output_dir, _addons_dir, _ctx_meta_db, _ctx_get_dlc_dir, _ctx_get_art_cache_dir
    _log = context.get("log")
    # Capture the metadata DB and DLC-dir accessor for direct in-process
    # album/year lookups (avoids guessing the server's HTTP port).
    _ctx_meta_db = context.get("meta_db")
    _ctx_get_dlc_dir = context.get("get_dlc_dir")
    _ctx_get_art_cache_dir = context.get("get_art_cache_dir")
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
