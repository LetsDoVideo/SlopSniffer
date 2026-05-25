# SlopSniffer

A drop-in [RockSniffer](https://github.com/kokolihapihvi/RockSniffer)
replacement built as a [SlopSmith](https://github.com/byrongamatos/slopsmith)
plugin. It lets your existing Rocksmith-streaming OBS overlays keep
working when you switch from Rocksmith to SlopSmith. No widget changes
needed.

SlopSniffer does three things:

1. **Serves the RockSniffer JSON** on `http://127.0.0.1:9938/` so any
   OBS browser-source widget that polled RockSniffer just works.
2. **Bundles two ready-to-use widgets** (the RockSniffer `current_song`
   and `note_streaks` overlays), so new streamers get a working overlay
   out of the box.
3. **Writes the RockSniffer text-file output layer** (6 `.txt` files +
   `album_cover.jpeg`) for streamers who use OBS Text (GDI+) / Image
   sources instead of a browser overlay.

## Install

SlopSniffer is a plain plugin, no build step. Clone it into SlopSmith's
`plugins/` folder under the directory name `slopsniffer`:

```
cd plugins
git clone https://github.com/LetsDoVideo/SlopSniffer.git slopsniffer
# restart SlopSmith
```

Restart SlopSmith. On startup you should see a log line like
`SlopSniffer: serving RockSniffer JSON on 127.0.0.1:9938`.

> If you still have the real **RockSniffer** running, it already owns
> port 9938 and SlopSniffer will log a bind warning and skip the JSON
> server. Close RockSniffer first.

## Verify it's working

Play any song in SlopSmith, then open `http://127.0.0.1:9938/` in a
browser. You should see a JSON blob with the current song's title,
artist, and a `songTimer` that advances as the song plays.

## Using the bundled widgets in OBS

The widgets live in this plugin's `addons/` folder and load directly
from disk via a `file://` URL — exactly like RockSniffer's own addons.

1. In OBS, add a **Browser** source.
2. Check **Local file**.
3. Browse to the widget HTML inside this plugin's `addons/` folder:
   - Now-playing overlay:
     `…/plugins/slopsniffer/addons/current_song/current_song.html`
   - Note-streak banner:
     `…/plugins/slopsniffer/addons/note_streaks/note_streaks.html`
4. Set the width/height to taste (the `current_song` overlay is designed
   around ~500×150; `note_streaks` is a transient full-width banner).

The widgets read their server address from `addons/config.js`, which is
pre-set to `127.0.0.1:9938` — the same default RockSniffer used, so no
editing is needed.

## Using the text-file output (OBS Text / Image sources)

SlopSniffer writes RockSniffer-style output files into the plugin's
config directory, under an `output/` folder:

| File | Contents |
| --- | --- |
| `song_details.txt` | `Artist - Song Name` |
| `album_details.txt` | `Album (Year)` |
| `song_timer.txt` | `m:ss/m:ss` (elapsed / total) |
| `notes.txt` | `hit/total` |
| `accuracy.txt` | `0.00%` |
| `streaks.txt` | `current/highest` |
| `album_cover.jpeg` | current album art |

In OBS, add a **Text (GDI+)** source, check **Read from file**, and
point it at the relevant `.txt`. For the album art, add an **Image**
source pointing at `album_cover.jpeg`. OBS Text/Image sources re-read
their file automatically, so they update live as SlopSniffer rewrites
them (a few times per second while a song plays).

The exact path of the `output/` folder is logged at startup
(`output dir: …`).

## How it works

- A browser-side agent (`screen.js`) watches SlopSmith playback via the
  `window.slopsmith` event bus and the highway getters, and POSTs a
  small state snapshot to the plugin backend.
- The backend (`routes.py`) holds that snapshot in memory, assembles the
  full RockSniffer JSON, and serves it on port 9938 (a tiny standalone
  HTTP server, separate from SlopSmith's own server). It also writes the
  text-file outputs and fetches/caches album art.

The JSON served on `:9938` matches RockSniffer's contract — top-level
`success`, `currentState`, `memoryReadout` (with both flat and nested
`noteData`), `albumCoverBase64`, and `songDetails` — so widgets written
against RockSniffer need no changes.

## License

SlopSniffer is licensed **AGPL-3.0-only**, matching the SlopSmith stack.
See `LICENSE`.

The bundled widgets under `addons/` are the RockSniffer `current_song`
and `note_streaks` overlays, reused with the RockSniffer author's
permission, and retain their original **MIT** license — see
`addons/LICENSE-RockSniffer.txt`.
