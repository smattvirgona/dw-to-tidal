# DW to TIDAL

Copies your Spotify **Discover Weekly** into a new, dated playlist on **TIDAL** every week,
inside a "Weekly discoveries" folder. Runs on your own computer; nothing is sent anywhere
except to Spotify and TIDAL.

## Using it

1. **Download** the app for your computer and put it somewhere handy:
   - macOS: `DW to TIDAL.app`
   - Windows: `DW to TIDAL.exe`
2. **Open it.** A browser tab opens at `http://127.0.0.1:8765`. (There is no window of its own —
   the browser tab is the app. Closing the tab doesn't quit it; quit from the Dock / system tray
   or Task Manager.)
   - macOS may say the app is from an unidentified developer: right-click → **Open** → Open.
   - Windows SmartScreen: **More info** → **Run anyway**.
3. **Paste your Discover Weekly link.** In Spotify: open Discover Weekly → `⋯` → Share → Copy link.
   Press **Save settings**. The link stays the same every week.
4. **Connect TIDAL.** Press *Connect TIDAL*, open the link that appears, sign in and approve.
   You only do this once.
5. **Tick the schedule.** Pick a day and hour (Discover Weekly refreshes on Monday) and tick
   *Run every week at this time*. Press **Save schedule**.
   Or press **Copy this week's playlist to TIDAL** to do it right now.

Each run creates `Discover Weekly YYYY-MM-DD` in the *Weekly discoveries* folder on TIDAL.
Tracks that can't be found on TIDAL are listed in the log on the page.

### The schedule only fires while the app is open

Add it to your startup items so it's always running:

- **macOS:** System Settings → General → Login Items → **+** → choose `DW to TIDAL.app`.
- **Windows:** press `Win+R`, type `shell:startup`, Enter, and drop a shortcut to `DW to TIDAL.exe` there.

If the computer was asleep at the scheduled hour, it runs when it wakes up later that day.

### Optional: Spotify developer key

Matching is done by artist + title and is right almost every time. A Spotify developer key
(developer.spotify.com/dashboard → Create app; any redirect URI such as `http://127.0.0.1:8765/callback`)
lets the app match by ISRC code instead. **Note:** Spotify currently returns `403 Forbidden` for
this lookup on newly created apps, in which case the app silently falls back to artist + title.

### Where things are stored

Everything lives in `~/.dw2tidal/` (macOS: `/Users/you/.dw2tidal`, Windows: `C:\Users\you\.dw2tidal`):
`config.json`, `tidal-session.json` (your TIDAL login — keep private), `state.json`, `app.log`.
Delete the folder to reset.

## Building it yourself

Requires Python 3.11+. Build on the platform you're targeting.

```sh
./build.sh      # macOS  -> dist/DW to TIDAL.app
build.bat       # Windows -> dist\DW to TIDAL.exe
```

Run from source: `pip install -r requirements.txt && python3 dw2tidal_app.py`.
Tests: `pip install -r requirements-dev.txt && pytest`.

## How it works / caveats

- Spotify's Web API no longer exposes Discover Weekly to third-party apps, so the track list is
  read from the public embed page (`open.spotify.com/embed/playlist/…`). This is unofficial and may
  break if Spotify changes the page — the app will say "Spotify's embed page has changed".
- TIDAL access uses the `tidalapi` package (device-link login, like a TV app).
