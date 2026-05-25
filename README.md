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
Plugins**.

> **Already running the real RockSniffer?** It owns port 9938, so
> SlopSniffer won't work until you close RockSniffer.

## Using the bundled widgets in OBS

SlopSniffer comes with two ready-to-use overlays. To add one to your
stream:

1. In OBS, add a **Browser** source.
2. In the **URL** field, paste the address for the overlay you want:
   - Now-playing overlay:
     `http://127.0.0.1:9938/addons/current_song/current_song.html`
   - Note-streak banner:
     `http://127.0.0.1:9938/addons/note_streaks/note_streaks.html`
3. Set the width and height to taste (the now-playing overlay looks good
   around 500×150; the note-streak banner is a brief full-width pop-up).

> If OBS is running on a different computer than SlopSmith, replace
> `127.0.0.1` with the IP address of the machine running SlopSmith.

**Already using RockSniffer overlays?** They'll keep working with no
changes. SlopSniffer answers at the same address RockSniffer used
(`127.0.0.1:9938`), so any overlay you already had pointed at RockSniffer 
will just work once SlopSniffer is running.

## Using the text-file output (OBS Text / Image sources)

SlopSniffer writes RockSniffer-style output files into an output/
folder inside the SlopSniffer plugin folder itself (the same folder this
README is in, alongside addons/):

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
