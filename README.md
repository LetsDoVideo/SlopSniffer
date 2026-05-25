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

**Use SlopSmith's built-in Plugin Manager**

1. In SlopSmith, open the **Plugins** menu → **Plugin Manager**.
2. In the **Install Plugin** box, paste this repo's URL:
   `https://github.com/LetsDoVideo/SlopSniffer.git`
3. Click **Install**, then **restart SlopSmith** when prompted.

That's it. The Plugin Manager fetches the plugin into your plugins
folder for you. After restart, SlopSniffer appears under **Installed
Plugins**, and you should see a log line like
`SlopSniffer: serving RockSniffer JSON on 127.0.0.1:9938`.

<details>
<summary>Manual install (advanced — only if you're not using the Plugin Manager)</summary>

SlopSniffer is a plain plugin with no build step, so you can also drop
it into SlopSmith's plugins folder by hand. The plugins directory
location depends on your platform (see the SlopSmith Desktop README);
clone the repo into it, then restart SlopSmith:

```
git clone https://github.com/LetsDoVideo/SlopSniffer.git
# move the cloned folder into SlopSmith's plugins/ directory, then
# restart SlopSmith
```

The folder name doesn't matter — the plugin's `id` (`slopsniffer`) comes
from `plugin.json`, not the directory name.

</details>

> **Already running the real RockSniffer?** It owns port 9938, so
> SlopSniffer will log a bind warning and skip its JSON server until you
> close RockSniffer. You only need one of them.

## Verify it's working

Play any song in SlopSmith, then open `http://127.0.0.1:9938/` in a
browser. You should see a JSON blob with the current song's title,
artist, and a `songTimer` that advances as the song plays.

## Using the bundled widgets in OBS

The widgets live in this plugin's `addons/` folder and load directly
from disk via a `file://` URL — exactly like RockSniffer's own addons.

First, find where the plugin was installed. If you used the Plugin
Manager, the folder lives in SlopSmith's plugins directory (the location
varies by platform — see the SlopSmith Desktop README for your OS; on
Windows it's typically under `%APPDATA%\slopsmith-desktop\plugins\`).
The folder will be named after the repo (e.g. `SlopSniffer`).

1. In OBS, add a **Browser** source.
2. Check **Local file**.
3. Browse to the widget HTML inside the installed plugin's `addons/`
   folder:
   - Now-playing overlay:
     `…/plugins/<SlopSniffer folder>/addons/current_song/current_song.html`
   - Note-streak banner:
     `…/plugins/<SlopSniffer folder>/addons/note_streaks/note_streaks.html`
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

## License

SlopSniffer is licensed **AGPL-3.0-only**, matching the SlopSmith stack.
See `LICENSE`.

The bundled widgets under `addons/` are the RockSniffer `current_song`
and `note_streaks` overlays, reused with the RockSniffer author's
permission, and retain their original **MIT** license — see
`addons/LICENSE-RockSniffer.txt`.
