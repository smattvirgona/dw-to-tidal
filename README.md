# DW to TIDAL

Copies your Spotify **Discover Weekly** into a new, dated playlist on **TIDAL** every week,
inside a "Weekly discoveries" folder. Runs on your own computer; nothing is sent anywhere
except to Spotify and TIDAL.

## Using it

1. **Download** `dw-to-tidal.zip`, unzip it, and keep the folder somewhere handy
   (e.g. Documents). Don't move files out of the folder.
2. **Open it:**
   - macOS: double-click **`Start.command`**. The first time, macOS may say it can't be opened:
     right-click it → **Open** → **Open**. (Once.)
   - Windows: double-click **`Start.bat`**. If SmartScreen appears: **More info** → **Run anyway**.

   A black window opens and, the first time only, downloads its own small Python runtime
   (about a minute, ~100 MB, kept inside `~/.dw2tidal`; nothing is installed system-wide).
   Then a browser tab opens at `http://127.0.0.1:8765` — that's the app.
   **Keep the black window open**; closing it quits the app.
3. **Paste your Discover Weekly link.** In Spotify: open Discover Weekly → `⋯` → Share → Copy link.
   Press **Save settings**. The link stays the same every week.
4. **Connect TIDAL.** Press *Connect TIDAL*, open the link that appears, sign in and approve.
   You only do this once.
5. **Tick the schedule.** Pick a day and hour (Discover Weekly refreshes on Monday) and tick
   *Run every week at this time*. Press **Save schedule**.
   Or press **Copy this week's playlist to TIDAL** to do it right now.

Each run creates `Discover Weekly YYYY-MM-DD` in the *Weekly discoveries* folder on TIDAL.
Tracks that can't be found on TIDAL are listed in the log on the page.

Phones (iOS/Android) aren't supported — the app has to run on a computer.

### The schedule only fires while the app is open

Add it to your startup items so it's always running:

- **macOS:** System Settings → General → Login Items → **+** → choose `Start.command`.
- **Windows:** press `Win+R`, type `shell:startup`, Enter, and drop a shortcut to `Start.bat` there.

If the computer was asleep at the scheduled hour, it runs when it wakes up later that day.

### Optional: Spotify developer key

Matching is done by artist + title and is right almost every time. A Spotify developer key
(developer.spotify.com/dashboard → Create app; any redirect URI such as `http://127.0.0.1:8765/callback`)
lets the app match by ISRC code instead. **Note:** Spotify currently returns `403 Forbidden` for
this lookup on newly created apps, in which case the app silently falls back to artist + title.

### Where things are stored

Everything lives in `~/.dw2tidal/` (macOS: `/Users/you/.dw2tidal`, Windows: `C:\Users\you\.dw2tidal`):
`config.json`, `tidal-session.json` (your TIDAL login — keep private), `state.json`, `app.log`,
and the downloaded runtime (`bin/`, `uv/`). Delete the folder to reset / uninstall completely.

## Making the zip (for whoever shares it)

```sh
./make_package.sh    # -> dist/dw-to-tidal.zip
```

Run from source instead: `pip install -r requirements.txt && python3 dw2tidal_app.py`
(Python 3.11+). Tests: `pip install -r requirements-dev.txt && pytest`.

## How it works / caveats

- Spotify's Web API no longer exposes Discover Weekly to third-party apps, so the track list is
  read from the public embed page (`open.spotify.com/embed/playlist/…`). This is unofficial and may
  break if Spotify changes the page — the app will say "Spotify's embed page has changed".
- TIDAL access uses the `tidalapi` package (device-link login, like a TV app).
- The launcher uses [uv](https://docs.astral.sh/uv/) to fetch a private Python runtime; it's confined to `~/.dw2tidal`.
